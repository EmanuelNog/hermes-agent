"""Local speaker diarization ("who spoke when") for the local STT provider.

Cloud ElevenLabs Scribe is the only provider that answers this today (``stt.elevenlabs.diarize``);
this module gives the free local path the same capability with sherpa-onnx (pyannote segmentation
+ speaker embedding + fast clustering), all CPU ONNX — no torch, no network, no third party.

Opt-in per ``stt.local.diarize: true``. Diarization is ALWAYS best-effort: when sherpa-onnx or the
model files are missing, or ffmpeg cannot decode, transcription still succeeds and the result
carries a ``diarize_error`` string instead of speakers.
"""

from __future__ import annotations

import array
import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from tools.transcription_audio import _find_ffmpeg_binary
from tools.transcription_common import _config_number
from utils import is_truthy_value

# Log-record parity with the origin module.
logger = logging.getLogger("tools.transcription_tools")

DEFAULT_DIARIZE_MODEL_DIRNAME = "sherpa-speaker"
DEFAULT_DIARIZE_THRESHOLD = 0.5
DEFAULT_DIARIZE_NUM_THREADS = 2
_SAMPLE_RATE = 16000
# Segmentation model directories ship as ``<name>/model.onnx``; the embedding is a flat ``.onnx``.
_SEGMENTATION_DIR_HINT = "segmentation"
_EMBEDDING_NAMES = ("eres2net_en.onnx", "3dspeaker_speech_eres2net_base_sv_zh-cn.onnx")

_LOCK = threading.Lock()
_DIARIZER_CACHE: dict[tuple[str, str, int], Any] = {}


class DiarizeUnavailable(RuntimeError):
    """Raised when local diarization cannot run; the message is user-facing."""


def diarize_enabled(local_cfg: Optional[dict[str, Any]]) -> bool:
    """True when ``stt.local.diarize`` asks for speaker labels."""
    if not isinstance(local_cfg, dict):
        return False
    from utils import is_truthy_value  # local import: keeps module import cheap

    return is_truthy_value(local_cfg.get("diarize", False), default=False)


def default_diarize_model_dir() -> Path:
    """``<hermes home>/models/sherpa-speaker`` — where the two ONNX models live."""
    return get_hermes_home() / "models" / DEFAULT_DIARIZE_MODEL_DIRNAME


def resolve_diarize_paths(local_cfg: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    """Return ``(segmentation_model, embedding_model)`` under ``stt.local.diarize_model_dir``."""
    cfg = local_cfg if isinstance(local_cfg, dict) else {}
    raw_dir = cfg.get("diarize_model_dir") or default_diarize_model_dir()
    model_dir = Path(os.path.expanduser(str(raw_dir)))
    if not model_dir.is_dir():
        raise DiarizeUnavailable(
            f"diarization model dir not found: {model_dir} (set stt.local.diarize_model_dir)")

    segmentation: Optional[Path] = None
    embedding: Optional[Path] = None
    for candidate in sorted(model_dir.glob("*/model.onnx")):
        if _SEGMENTATION_DIR_HINT in candidate.parent.name or "pyannote" in candidate.parent.name:
            segmentation = candidate
            break
    if segmentation is None:
        flat = sorted(model_dir.glob("*.onnx"))
        segmentation = flat[0] if flat else None

    for name in _EMBEDDING_NAMES:
        if (model_dir / name).is_file():
            embedding = model_dir / name
            break
    if embedding is None:
        for candidate in sorted(model_dir.glob("*.onnx")):
            if segmentation is not None and candidate == segmentation:
                continue
            embedding = candidate
            break

    if segmentation is None or embedding is None:
        raise DiarizeUnavailable(
            f"diarization needs a segmentation model (*/model.onnx) and a speaker embedding "
            f"(*.onnx) under {model_dir}; found segmentation={segmentation}, embedding={embedding}")
    return str(segmentation), str(embedding)


def _load_sherpa():
    try:
        import sherpa_onnx  # optional dependency, lazy on purpose
    except Exception as exc:  # ImportError, or a broken native wheel
        raise DiarizeUnavailable(
            "sherpa-onnx is not importable in this Hermes environment; install it with "
            "`pm install wake-sherpa` (or `pip install sherpa-onnx`) to enable local diarization"
        ) from exc
    return sherpa_onnx


def build_diarizer(local_cfg: Optional[dict[str, Any]] = None):
    """Create (and cache) an ``sherpa_onnx.OfflineSpeakerDiarization`` for the configured models."""
    sherpa_onnx = _load_sherpa()
    segmentation, embedding = resolve_diarize_paths(local_cfg)
    cfg = local_cfg if isinstance(local_cfg, dict) else {}
    threads = max(1, _config_number(cfg, "diarize_num_threads", DEFAULT_DIARIZE_NUM_THREADS, int))
    key = (segmentation, embedding, threads)
    with _LOCK:
        cached = _DIARIZER_CACHE.get(key)
        if cached is not None:
            return cached
        diarizer_config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=segmentation)),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=embedding, num_threads=threads),
            # num_clusters=-1 = infer the count; threshold separates same vs different speakers.
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=_config_number(cfg, "diarize_num_speakers", -1, int),
                threshold=_config_number(cfg, "diarize_threshold", DEFAULT_DIARIZE_THRESHOLD)),
            min_duration_on=0.3,
            min_duration_off=0.5)
        diarizer = sherpa_onnx.OfflineSpeakerDiarization(diarizer_config)
        _DIARIZER_CACHE[key] = diarizer
        return diarizer


def read_audio_16k(file_path: str) -> list[float]:
    """Decode any supported container to 16 kHz mono float samples via ffmpeg."""
    ffmpeg = _find_ffmpeg_binary()
    if not ffmpeg:
        raise DiarizeUnavailable("ffmpeg is required to decode audio for diarization")
    try:
        raw = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-i", file_path, "-ar", str(_SAMPLE_RATE),
             "-ac", "1", "-f", "s16le", "-"],
            capture_output=True, check=True, timeout=300).stdout
    except subprocess.TimeoutExpired as exc:
        raise DiarizeUnavailable("ffmpeg timed out decoding audio for diarization") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise DiarizeUnavailable(f"ffmpeg failed to decode audio: {detail}") from exc
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        raise DiarizeUnavailable("decoded audio is empty")
    return [value / 32768.0 for value in samples]


def diarize_file(file_path: str, local_cfg: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Return ``[{start, end, speaker}]`` sorted by start time."""
    diarizer = build_diarizer(local_cfg)
    samples = read_audio_16k(file_path)
    result = diarizer.process(samples)
    segments = sorted(result.sort_by_start_time(), key=lambda s: float(s.start))
    return [{"start": round(float(s.start), 3), "end": round(float(s.end), 3),
             "speaker": int(s.speaker)} for s in segments]


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers(
    whisper_segments: Any, diarization: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach a speaker to each whisper segment by greatest time overlap.

    Segments with no overlap keep ``speaker: None`` rather than guessing. ``speaker`` is the
    sherpa cluster index, rendered as ``SPEAKER_00``.
    """
    labeled: list[dict[str, Any]] = []
    for segment in whisper_segments:
        try:
            start, end = float(segment.start), float(segment.end)
        except (AttributeError, TypeError, ValueError):
            continue
        best_speaker: Optional[int] = None
        best_overlap = 0.0
        for turn in diarization:
            overlap = _overlap(start, end, turn["start"], turn["end"])
            if overlap > best_overlap:
                best_overlap, best_speaker = overlap, turn["speaker"]
        text = getattr(segment, "text", "") or ""
        labeled.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "speaker": None if best_speaker is None else f"SPEAKER_{best_speaker:02d}",
            "text": text.strip()})
    return labeled


def format_labeled_transcript(labeled: list[dict[str, Any]]) -> str:
    """Render labeled segments as ``SPEAKER_00: text`` blocks, one per speaker turn."""
    lines: list[str] = []
    for row in labeled:
        if not row["text"]:
            continue
        label = row["speaker"] or "SPEAKER_?"
        body = f"{label}: {row['text']}"
        if lines and lines[-1].startswith(f"{label}: "):
            lines[-1] = f"{lines[-1]} {row['text']}"
        else:
            lines.append(body)
    return "\n".join(lines).strip()


def diarize_transcript(
    file_path: str, whisper_segments: Any, local_cfg: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Diarize + label an already-transcribed local result. Raises ``DiarizeUnavailable``."""
    diarization = diarize_file(file_path, local_cfg)
    labeled = assign_speakers(whisper_segments, diarization)
    speakers = sorted({row["speaker"] for row in labeled if row["speaker"]})
    return {
        "transcript": format_labeled_transcript(labeled),
        "segments": labeled,
        "speakers": speakers,
        "turns": diarization}


def diarize_status(local_cfg: Optional[dict[str, Any]] = None) -> str:
    """Human/agent-readable readiness line, used by diagnostics and the CLI helper."""
    try:
        segmentation, embedding = resolve_diarize_paths(local_cfg)
        _load_sherpa()
    except DiarizeUnavailable as exc:
        return f"local diarization unavailable: {exc}"
    return json.dumps({"ready": True, "segmentation": segmentation, "embedding": embedding})
