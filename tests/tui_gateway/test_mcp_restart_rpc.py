"""``mcp.servers.restart``: the gateway's one-server respawn (chk-n5va.155).

The respawn itself is proven against a real stdio child in ``tests/tools/test_mcp_restart_verb.py``;
here the registered handler is driven for its envelope — an unknown name is refused without touching
the runtime, and a named one forwards exactly that server and reports its post-reconnect row.
"""

from __future__ import annotations

import pytest

from tui_gateway import server


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "hermes_home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    from hermes_constants import get_hermes_home_override

    assert get_hermes_home_override() is None
    return root


def _call(method, params=None):
    return server._methods[method](1, params or {})


def test_restart_forwards_the_named_server_and_reports_its_row(home, monkeypatch):
    from hermes_cli import mcp_config
    from tools import mcp_tool_discovery as disc

    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda: {"swap": {"command": "./run.sh"}})
    asked = []

    def fake_restart(names):
        asked.append(set(names))
        return sorted(names)

    monkeypatch.setattr(disc, "restart_mcp_servers", fake_restart)
    monkeypatch.setattr(disc, "get_mcp_status", lambda configured=None, **kw: [
        {"name": "swap", "transport": "stdio", "tools": 3, "connected": True,
         "disabled": False, "status": "connected"}])

    result = _call("mcp.servers.restart", {"name": "swap"})["result"]

    assert asked == [{"swap"}], "exactly the named server, never the whole profile"
    assert result == {"ok": True, "name": "swap", "restarted": True, "status": "connected",
                      "tools": 3, "error": None}


def test_restart_refuses_an_unknown_server(home, monkeypatch):
    from hermes_cli import mcp_config
    from tools import mcp_tool_discovery as disc

    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda: {"swap": {"command": "./run.sh"}})
    monkeypatch.setattr(disc, "restart_mcp_servers",
                        lambda names: pytest.fail("must not touch the runtime for an unknown name"))

    resp = _call("mcp.servers.restart", {"name": "nope"})

    assert "error" in resp
    assert resp["error"]["code"] == 4064


@pytest.mark.no_isolate
@pytest.mark.platforms("posix")
def test_restart_rpc_respawns_a_real_stdio_child(home, monkeypatch):
    """The handler end to end: real config, real child, a rebuilt file on disk. This is the case the
    verb exists for — nothing the server launches with changed, only its code."""
    import sys
    import textwrap

    pytest.importorskip("mcp.server.mcpserver")
    srv = home / "srv"
    srv.mkdir()
    (srv / "server.py").write_text(textwrap.dedent('''\
        import os, pathlib
        from mcp.server.mcpserver import MCPServer
        here = pathlib.Path(__file__).parent
        (here / "pid.txt").write_text(str(os.getpid()))
        version = (here / "ver.txt").read_text().strip()
        app = MCPServer("swap")

        @app.tool()
        def which() -> str:
            """version marker"""
            return version

        app.run()
    '''), encoding="utf-8")
    (srv / "run.sh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/server.py"\n',
                                encoding="utf-8")
    (srv / "run.sh").chmod(0o755)
    (srv / "ver.txt").write_text("v1", encoding="utf-8")
    (home / "config.yaml").write_text(
        f"mcp_servers:\n  swap:\n    command: ./run.sh\n    cwd: {srv}\n", encoding="utf-8")

    from tools import mcp_tool_discovery as disc
    from tools import mcp_tool_loop as loop
    from tools.mcp_tool_common import _core

    try:
        disc.discover_mcp_tools()
        first = int((srv / "pid.txt").read_text(encoding="utf-8").strip())
        assert _core._servers["swap"]._tools, "the child must register its tool"

        (srv / "server.py").write_text(
            (srv / "server.py").read_text(encoding="utf-8").replace("version marker", "rebuilt marker"),
            encoding="utf-8")

        result = _call("mcp.servers.restart", {"name": "swap"})["result"]

        assert result["ok"] is True and result["status"] == "connected" and result["tools"] >= 1
        assert int((srv / "pid.txt").read_text(encoding="utf-8").strip()) != first
        assert _core._servers["swap"]._tools[0].description == "rebuilt marker"
    finally:
        from tools.mcp_tool_lifecycle import shutdown_mcp_servers
        shutdown_mcp_servers()
        loop._stop_mcp_loop()

