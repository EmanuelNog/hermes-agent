"""Tests for local speaker diarization (``stt.local.diarize``).

Contract under test:

1. ``stt.local.diarize`` is off by default and truthy strings enable it.
2. Model discovery needs both a segmentation model and a speaker embedding; a
   missing dir/files raises ``DiarizeUnavailable`` with an actionable message.
3. Whisper segments get the speaker of their greatest-overlap diarization turn;
   segments with no overlap get ``None`` instead of a guess.
4. Consecutive same-speaker segments merge into one ``SPEAKER_00: …`` turn.
5. ``_transcribe_local`` adds ``speakers``/``segments`` when diarization runs, and
   degrades to ``diarize_error`` (still ``success: True``) when it cannot.
"""

import struct
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import tools.transcription_tools as tt
from tools.transcription_diarize import (
    DiarizeUnavailable,
    assign_speakers,
    diarize_enabled,
    format_labeled_transcript,
    resolve_diarize_paths,
)


class TestDiarizeEnabled:
    def test_off_by_default(self):
        assert diarize_enabled({}) is False
        assert diarize_enabled(None) is False

    def test_truthy_values(self):
        assert diarize_enabled({"diarize": True}) is True
        assert diarize_enabled({"diarize": "true"}) is True

    def test_falsy_values(self):
        assert diarize_enabled({"diarize": False}) is False
        assert diarize_enabled({"diarize": "no"}) is False


class TestResolveDiarizePaths:
    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(DiarizeUnavailable, match="model dir not found"):
            resolve_diarize_paths({"diarize_model_dir": str(tmp_path / "nope")})

    def test_missing_models_raises(self, tmp_path):
        with pytest.raises(DiarizeUnavailable, match="segmentation"):
            resolve_diarize_paths({"diarize_model_dir": str(tmp_path)})

    def test_finds_pyannote_dir_and_embedding(self, tmp_path):
        seg_dir = tmp_path / "sherpa-onnx-pyannote-segmentation-3-0"
        seg_dir.mkdir()
        (seg_dir / "model.onnx").write_bytes(b"seg")
        (tmp_path / "eres2net_en.onnx").write_bytes(b"emb")

        segmentation, embedding = resolve_diarize_paths({"diarize_model_dir": str(tmp_path)})

        assert segmentation.endswith("sherpa-onnx-pyannote-segmentation-3-0/model.onnx")
        assert embedding.endswith("eres2net_en.onnx")


def _whisper_seg(start, end, text):
    return SimpleNamespace(start=start, end=end, text=text,
                           no_speech_prob=0.0, avg_logprob=-0.2)


class TestAssignSpeakers:
    TURNS = [
        {"start": 0.0, "end": 2.0, "speaker": 0},
        {"start": 2.0, "end": 4.0, "speaker": 1},
    ]

    def test_picks_greatest_overlap(self):
        labeled = assign_speakers(
            [_whisper_seg(0.5, 1.5, " hi"), _whisper_seg(2.4, 3.6, " there")], self.TURNS)
        assert [row["speaker"] for row in labeled] == ["SPEAKER_00", "SPEAKER_01"]

    def test_no_overlap_is_none_not_a_guess(self):
        labeled = assign_speakers([_whisper_seg(10.0, 11.0, " later")], self.TURNS)
        assert labeled[0]["speaker"] is None
        assert format_labeled_transcript(labeled) == "SPEAKER_?: later"

    def test_format_merges_consecutive_same_speaker(self):
        labeled = [
            {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "hello"},
            {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "again"},
            {"start": 2.0, "end": 3.0, "speaker": "SPEAKER_01", "text": "bye"},
        ]
        assert format_labeled_transcript(labeled) == (
            "SPEAKER_00: hello again\nSPEAKER_01: bye")


def _empty_wav(path):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<16000h", *([0] * 16000)))


def _fake_model():
    segment = MagicMock()
    segment.text = " hello"
    segment.start = 0.0
    segment.end = 1.0
    segment.no_speech_prob = 0.0
    segment.avg_logprob = -0.2
    info = MagicMock()
    info.language = "en"
    info.duration = 1.0
    model = MagicMock(name="whisper_model")
    model.transcribe.return_value = (iter([segment]), info)
    return model


class TestTranscribeLocalDiarize:
    def test_adds_speakers_to_result(self, tmp_path):
        wav_path = tmp_path / "a.wav"
        _empty_wav(wav_path)
        fake = {
            "transcript": "SPEAKER_00: hello",
            "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "hello"}],
            "speakers": ["SPEAKER_00"],
            "turns": [{"start": 0.0, "end": 1.0, "speaker": 0}]}

        with patch.object(tt, "_HAS_FASTER_WHISPER", True), \
             patch.object(tt, "_load_stt_config",
                          return_value={"local": {"diarize": True}}), \
             patch.object(tt, "build_local_transcribe_kwargs", return_value={}), \
             patch.object(tt, "_local_model", _fake_model()), \
             patch.object(tt, "_local_model_name", "base"), \
             patch("tools.transcription_diarize.diarize_transcript", return_value=fake):
            result = tt._transcribe_local(str(wav_path), "base")

        assert result["success"] is True
        assert result["speakers"] == ["SPEAKER_00"]
        assert result["transcript"] == "SPEAKER_00: hello"
        assert result["provider"] == "local"

    def test_missing_diarizer_keeps_transcript(self, tmp_path):
        wav_path = tmp_path / "a.wav"
        _empty_wav(wav_path)

        with patch.object(tt, "_HAS_FASTER_WHISPER", True), \
             patch.object(tt, "_load_stt_config",
                          return_value={"local": {"diarize": True}}), \
             patch.object(tt, "build_local_transcribe_kwargs", return_value={}), \
             patch.object(tt, "_local_model", _fake_model()), \
             patch.object(tt, "_local_model_name", "base"), \
             patch("tools.transcription_diarize.diarize_transcript",
                   side_effect=DiarizeUnavailable("no models")):
            result = tt._transcribe_local(str(wav_path), "base")

        assert result["success"] is True
        assert result["transcript"] == "hello"
        assert "no models" in result["diarize_error"]
        assert "speakers" not in result

    def test_diarize_off_returns_plain_result(self, tmp_path):
        wav_path = tmp_path / "a.wav"
        _empty_wav(wav_path)

        with patch.object(tt, "_HAS_FASTER_WHISPER", True), \
             patch.object(tt, "_load_stt_config", return_value={"local": {}}), \
             patch.object(tt, "build_local_transcribe_kwargs", return_value={}), \
             patch.object(tt, "_local_model", _fake_model()), \
             patch.object(tt, "_local_model_name", "base"), \
             patch("tools.transcription_diarize.diarize_transcript") as mocked:
            result = tt._transcribe_local(str(wav_path), "base")

        assert result == {"success": True, "transcript": "hello", "provider": "local"}
        mocked.assert_not_called()
