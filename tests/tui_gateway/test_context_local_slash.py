"""``/context`` must be answered IN-PROCESS for a local (non-compute-host) session.

The slash worker is a CLI subprocess whose ``self.agent`` is built lazily by the
first chat prompt and never by a slash command, so its /context answers
"(._.) No active agent -- send a message first." for ANY local session — active
or idle (#93280). The gateway already renders the view from the live session
(``_format_live_context_output``), with the desktop gauge's breakdown engine when
a live agent exists; this pins that routing so the command never falls through
to the worker for a local session.

Supersedes the narrower #81266, which only served the agentless case and pinned
the live-agent fallthrough (the bug) as intended behaviour.
"""

from __future__ import annotations

import contextlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import patch

from tui_gateway import server


def _live_session(agent, messages=2):
    return {
        "agent": agent,
        "session_key": "sk-1",
        "session_id": "sid-1",
        "history": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ][:messages],
        "history_lock": threading.Lock(),
        "running": False,
    }


def _no_db():
    """Render from in-memory history: no state.db needed in the test."""
    return contextlib.nullcontext()


def _payload(model="test-model", context_max=131072, used=90000):
    return {
        "categories": [
            {"id": "system_prompt", "label": "System prompt", "tokens": 8000, "color": "dim"},
            {"id": "conversation", "label": "Conversation", "tokens": 82000, "color": "normal"},
        ],
        "context_max": context_max,
        "context_percent": round(used / context_max * 100),
        "context_used": used,
        "context_source": "provider_usage",
        "context_estimated": False,
        "estimated_total": used,
        "model": model,
    }


def test_context_local_session_with_live_agent(monkeypatch):
    """THE bug: a live local session's /context fell through to the worker CLI (lazy agent,
    always None) and answered 'No active agent'. It now answers in-process with the classic CLI's
    body (``_show_context_breakdown``): the ``🧠 Context Usage`` header and the 5x20 glyph grid,
    not main's conversation counts."""
    from agent.i18n import t

    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_session_db", _no_db)

    with patch("agent.context_breakdown.compute_session_context_breakdown", return_value=_payload()):
        out = server._live_slash_command_output("sid-1", _live_session(object()), "context", "")

    assert out is not None, "/context fell through to the CLI worker for a local session"
    assert "No active agent" not in out
    lines = out.splitlines()
    assert lines[0] == t("cli.context.header", model="test-model"), out
    assert len(lines[2].split()) == 20 and len(lines[6].split()) == 20, out  # 5x20, one cell per percent
    assert "Conversation:" not in out and "Provider: auto" not in out, out


def test_context_local_session_agentless(monkeypatch):
    """Agentless (idle/resumed) local session: same in-process path (#81266 case)."""
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_session_db", _no_db)

    out = server._live_slash_command_output("sid-1", _live_session(None), "context", "")

    assert out is not None
    assert "No active agent" not in out
    assert "Conversation:" in out


def test_context_compute_host_route_unchanged(monkeypatch):
    """Guard: the isolated/compute-host route the gate was built for still works."""
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: True)
    monkeypatch.setattr(server, "_session_db", _no_db)

    out = server._live_slash_command_output("sid-1", _live_session(object()), "context", "")

    assert out is not None
    assert "Conversation:" in out


def test_tools_local_still_falls_through(monkeypatch):
    """Scope guard: /tools consults the agent, so it keeps the old routing."""
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)

    out = server._live_slash_command_output("sid-1", _live_session(None), "tools", "")

    assert out is None  # worker path, unchanged


def test_context_breakdown_engine_used_with_live_agent(monkeypatch):
    """With a live agent, the output carries the gauge engine's 'Context window'
    line (same compute_session_context_breakdown the popover RPC uses)."""
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_session_db", _no_db)

    payload = {
        "categories": [
            {"id": "system_prompt", "label": "System prompt", "tokens": 8000, "color": "dim"},
            {"id": "conversation", "label": "Conversation", "tokens": 82000, "color": "normal"},
        ],
        "context_max": 131072,
        "context_percent": 68,
        "context_used": 90000,
        "context_source": "provider_usage",
        "context_estimated": False,
        "estimated_total": 90000,
        "model": "test-model",
    }
    with patch("agent.context_breakdown.compute_session_context_breakdown", return_value=payload):
        out = server._live_slash_command_output("sid-1", _live_session(object()), "context", "")

    assert out is not None
    assert "Context window:" in out
    assert "90,000" in out  # rendered from the breakdown payload, not the usage mirror


def _classic_cli_body(agent, model):
    """What the classic CLI's /context prints for *agent* (its own 2-space margin removed)."""
    from hermes_cli.cli_info_mixin import CLIInfoMixin

    class _ClassicCLI(CLIInfoMixin):
        def __init__(self):
            self.agent, self.conversation_history, self.model = agent, [], model
            self._session_db, self._gateway_session_open = None, False

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _ClassicCLI()._show_context_breakdown("context")
    lines = [line.removeprefix("  ") for line in buffer.getvalue().splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def test_in_process_context_body_equals_the_classic_cli(monkeypatch):
    """The regression: /context on a TUI session presented different info than `hermes` (conversation
    counts instead of the CLI's grid/table/context files). Same payload, same rendered body."""
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_session_db", _no_db)
    # Both surfaces discover context files through this one function; pin it so the comparison is about
    # the rendered body, not which cwd each side happens to bind.
    monkeypatch.setattr("agent.context_file_sources.context_file_sources_for_agent",
                        lambda a: [{"label": "AGENTS.md", "status": "loaded", "est_tokens": 812}])
    agent = object()
    with patch("agent.context_breakdown.compute_session_context_breakdown", return_value=_payload()):
        out = server._live_slash_command_output("sid-1", _live_session(agent), "context", "")
        expected = _classic_cli_body(agent, "test-model")
    assert out is not None
    assert out.splitlines() == expected, out
    assert "Context files" in out and "AGENTS.md" in out, out


def test_context_all_arg_reaches_details(monkeypatch):
    """``/context all`` expands per-skill / per-toolset costs (compute_context_details)."""
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: False)
    monkeypatch.setattr(server, "_session_db", _no_db)

    payload = {
        "categories": [{"id": "conversation", "label": "Conversation", "tokens": 1000, "color": "normal"}],
        "context_max": 10000,
        "context_percent": 10,
        "context_used": 1000,
        "context_source": "provider_usage",
        "context_estimated": False,
        "estimated_total": 1000,
        "model": "test-model",
    }
    details = {"skills": [], "toolsets": []}
    with (
        patch("agent.context_breakdown.compute_session_context_breakdown", return_value=payload) as m_breakdown,
        patch("agent.context_breakdown.compute_context_details", return_value=details) as m_details,
    ):
        out = server._live_slash_command_output("sid-1", _live_session(object()), "context", "all")

    assert out is not None
    m_breakdown.assert_called_once()
    m_details.assert_called_once()
