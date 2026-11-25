"""Hand the summarizer's warm prompt prefix to the lemmalog extractor as a second turn.

Why: provider prompt caches key on a byte-identical prefix. The extractor's own request (session
messages, no tools) can never match the agent's (full toolset, different effort) — measured — but
the *summarizer's* request is warm for minutes after it returns. A second turn that replays those
messages, appends the summary and then the extraction instruction reads the whole prefix from cache
and pays full price only for the instruction and the reply.

This module only captures: the compressor calls :func:`maybe_capture_after_summary` with the exact
messages it sent, the summary it got back, and the per-message timestamps; the payload is written
0600 under ``$HERMES_HOME/cache/lemmalog/post-summary/`` and a detached ``lemmalog-sweep.sh
--post-summary <payload>`` consumes it. Extraction, filtering and store writes stay in the sweep
script, never in the compression path.

Off unless ``LEMMALOG_POST_SUMMARY`` is truthy. Nothing here may raise into compaction: a missing
script, an unwritable cache dir or a refused fork is a silent no-op.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

ENV_FLAG = "LEMMALOG_POST_SUMMARY"
_TRUTHY = ("1", "true", "yes", "on")


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in _TRUTHY


def script_path(home: Path | None = None) -> Path:
    """The sweep entry point, resolved against the active HERMES_HOME (never hardcoded)."""
    root = home
    if root is None:
        from hermes_constants import get_hermes_home

        root = get_hermes_home()
    return Path(root) / "scripts" / "lemmalog-sweep.sh"


def payload_path(session_id: str, home: Path | None = None) -> Path:
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    stamp = f"{int(time.time() * 1000)}"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id)[:96]
    return Path(home) / "cache" / "lemmalog" / "post-summary" / f"{safe}-{stamp}.json"


def _write_0600(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def maybe_capture_after_summary(
    messages: Iterable[dict[str, Any]],
    summary: str,
    *,
    session_id: str,
    model: str = "",
    reasoning_effort: Optional[str] = None,
    ts: Optional[list[int]] = None,
) -> Optional[Path]:
    """Write the second-turn payload and spawn the extractor; ``None`` when the switch is off."""
    if not enabled() or not (summary or "").strip() or not session_id:
        return None
    try:
        script = script_path()
        if not script.is_file():
            logger.debug("lemmalog post-summary skipped: %s is not a file", script)
            return None
        payload = {
            "session_id": session_id,
            "messages": list(messages),
            "summary": summary,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "ts": list(ts or []),
            "captured_at": time.time(),
        }
        path = payload_path(session_id)
        _write_0600(path, payload)
        subprocess.Popen(
            [str(script), "--post-summary", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return path
    except Exception:  # health: allow BLE001 -- the compression path must never fail on this
        logger.debug("lemmalog post-summary capture failed", exc_info=True)
        return None
