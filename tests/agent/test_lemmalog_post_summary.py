"""The summarizer's warm prefix is handed to the lemmalog extractor as a second turn.

The extractor cannot reuse the *agent's* cache (no tools, different effort — measured), but the
summarizer's own request is warm for minutes after it returns. These tests pin the bridge: a
payload written 0600 under HERMES_HOME/cache, one detached spawn, and total silence when the
switch is off or the script is absent.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from agent import lemmalog_post_summary as bridge


@pytest.fixture
def house(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "scripts").mkdir(parents=True)
    script = home / "scripts" / "lemmalog-sweep.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("LEMMALOG_POST_SUMMARY", "1")
    return home


def _payload_args(monkeypatch):
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return seen


def test_off_by_default_writes_nothing_and_spawns_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("LEMMALOG_POST_SUMMARY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen = _payload_args(monkeypatch)
    bridge.maybe_capture_after_summary(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], "the summary",
        session_id="s1", model="m",
    )
    assert "argv" not in seen
    assert not list(tmp_path.rglob("*.json"))


def test_enabled_writes_a_0600_payload_and_spawns_the_script(house, monkeypatch):
    seen = _payload_args(monkeypatch)
    messages = [{"role": "system", "content": "instructions"},
                {"role": "user", "content": "TURNS TO SUMMARIZE:\nhi"},
                {"role": "user", "content": "typed turn two"}]
    bridge.maybe_capture_after_summary(
        messages, "the summary", session_id="s1", model="deepseek-v4.1-flash",
        reasoning_effort="none", ts=[10, 20, 20],
    )
    [path] = list((house / "cache" / "lemmalog" / "post-summary").glob("*.json"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    payload = json.loads(path.read_text())
    assert payload["messages"] == messages
    assert payload["summary"] == "the summary"
    assert payload["session_id"] == "s1"
    assert payload["model"] == "deepseek-v4.1-flash"
    assert payload["reasoning_effort"] == "none"
    assert payload["ts"] == [10, 20, 20]
    assert seen["argv"][0].endswith("lemmalog-sweep.sh")
    assert seen["argv"][1] == "--post-summary"
    assert seen["argv"][2] == str(path)
    assert seen["kwargs"]["start_new_session"] is True


def test_missing_script_never_raises_into_compaction(house, monkeypatch):
    (house / "scripts" / "lemmalog-sweep.sh").unlink()
    seen = _payload_args(monkeypatch)
    bridge.maybe_capture_after_summary([{"role": "user", "content": "u"}], "s", session_id="s1", model="m")
    assert "argv" not in seen


def test_a_spawn_failure_never_raises_into_compaction(house, monkeypatch):
    def boom(*a, **k):
        raise OSError("no fork for you")

    monkeypatch.setattr(subprocess, "Popen", boom)
    bridge.maybe_capture_after_summary([{"role": "user", "content": "u"}], "s", session_id="s1", model="m")


def test_empty_summary_or_session_is_a_noop(house, monkeypatch):
    seen = _payload_args(monkeypatch)
    bridge.maybe_capture_after_summary([{"role": "user", "content": "u"}], "  ", session_id="s1", model="m")
    bridge.maybe_capture_after_summary([{"role": "user", "content": "u"}], "s", session_id="", model="m")
    assert "argv" not in seen
