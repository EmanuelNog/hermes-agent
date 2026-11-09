"""C8 chaos: SIGTERM to a one-shot CLI pid is a REAL shutdown, not a turn interrupt.

Class (chk-n5va.89): a spawner that bounds ``hermes chat -q`` with a one-pid signal —
``timeout(1)``, a shell script, ``KillMode=process`` — must not leave the turn's own work
running. The audit that opened this class asked whether SIGTERM to the CLI "stops the turn's
own work, or only interrupts it", because a lane worker's in-flight tool reported
``[Command interrupted]`` and the session went on to commit and close a bead for six more
minutes. Answer, pinned here: ``hermes_cli/cli_single_query.py::
_install_single_query_signal_handlers`` turns the signal into ``agent.interrupt()`` plus a
KeyboardInterrupt, so the CLI kills the in-flight tool and exits 130 inside the grace window
— the turn cannot take another step.

Three assertions fail together if that ever degrades back to "interrupt the tool and carry
on": the process ends 130 within INTERRUPT_DEADLINE_S, the in-flight command never reaches
its second stage (the shell is gone, so the marker file is absent), and no provider request
follows the signal.

This contract is what a one-pid bound may rely on. It does NOT cover the tree: the terminal
tool executes in the profile's environment server (a gateway the CLI either spawns or
attaches to, verified 2026-11-09), so a spawner that signals an arbitrary pid — not the CLI —
still needs a full tree walk with identity checks (pc1: ``scripts/lib/run_group.py``).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.e2e.core.chaos._helpers import (
    INTERRUPT_DEADLINE_S,
    describe_pids,
    hermetic_env,
    kill_tagged,
    new_tag,
    python_exe,
    tagged_pids,
    write_chaos_home,
)
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.waiting import wait_until

# Reading /proc for the tagged tool process, and the env server is POSIX-shaped.
pytestmark = [
    pytest.mark.platforms("linux"),
    pytest.mark.spawns_gateway_lookalike,
]

TOOL_START_TIMEOUT = 120.0
# Long enough that the probe can observe the tool in flight and signal the CLI, short enough
# that a tool which SURVIVED the signal would still write its second marker inside
# PROCESS_REAP_S — the difference between "killed" and "kept going" has to be observable.
TOOL_SLEEP_S = 8
PROCESS_REAP_S = 20.0


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _tool_pids(tag: str) -> list[int]:
    """Tagged processes still running the probe's ``sleep`` — the in-flight tool, however deep."""
    return [pid for pid in tagged_pids(tag) if "sleep" in _cmdline(pid)]


def test_sigterm_to_one_shot_cli_ends_the_turn_and_its_tool(tmp_path: Path) -> None:
    root = tmp_path / "sigterm"
    tag = new_tag()
    started = root / "tool-started"
    finished = root / "tool-finished"
    command = f"touch {started} && sleep {TOOL_SLEEP_S} && touch {finished}"

    srv = FakeLLMServer([ToolCall("terminal", {"command": command}),
                         Text("TURN CONTINUED AFTER THE TERM")])
    srv.start()
    proc: subprocess.Popen | None = None
    cli_log = root / "cli.log"
    try:
        # No PM dependency selection: the child provisions its own tools inside the fake home
        # (tests/e2e/core/_pm_dependencies reads the REAL ~/.hermes/tools, which the home guard
        # refuses). TOOL_START_TIMEOUT covers that first-run bootstrap.
        home, hermes_home = write_chaos_home(root, srv.base_url, extra="mcp_servers: {}\n")
        log = cli_log.open("wb")
        proc = subprocess.Popen(
            [python_exe(), "-m", "hermes_cli.main", "chat", "-q", "run the probe", "--oneshot", "-Q"],
            cwd=str(root), env=hermetic_env(home, hermes_home, tag), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
        )

        # The turn has to be INSIDE a tool when the signal lands, or the test proves nothing.
        wait_until(started.exists, timeout=TOOL_START_TIMEOUT,
                   what="the scripted terminal tool started (provider request #1, tool launched)")
        assert _tool_pids(tag), f"no tagged tool process; alive: {describe_pids(tagged_pids(tag))}"
        assert len(srv.main_requests()) == 1, "the turn had already moved past its tool call"

        os.kill(proc.pid, signal.SIGTERM)

        try:
            rc = proc.wait(timeout=INTERRUPT_DEADLINE_S)
        except subprocess.TimeoutExpired:
            survivors = describe_pids(tagged_pids(tag))
            tail = cli_log.read_text(errors="replace")[-2000:]
            raise AssertionError(
                f"the CLI ignored SIGTERM for {INTERRUPT_DEADLINE_S}s; still alive: {survivors}\n{tail}")
        assert rc == 130, f"SIGTERM is the one-shot interrupt exit (130), got {rc}: {cli_log.read_text(errors='replace')[-2000:]}"

        # The tool must be reaped with it — same process, an env server, or a setsid child.
        wait_until(lambda: not _tool_pids(tag), timeout=PROCESS_REAP_S,
                   what=f"the in-flight tool to die with the CLI; alive: {describe_pids(_tool_pids(tag))}")
        assert not finished.exists(), (
            "the in-flight tool kept running after SIGTERM and reached its second command — "
            "SIGTERM degraded to an interrupt"
        )
        assert len(srv.main_requests()) == 1, (
            f"a provider request followed the SIGTERM ({len(srv.main_requests())} main requests): "
            "the turn kept going after the signal"
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        kill_tagged(tag)
        srv.stop()
