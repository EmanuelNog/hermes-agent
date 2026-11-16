"""``restart_mcp_servers`` respawns ONE named server even when nothing it launches with changed.

That is the rebuilt-binary case (chk-n5va.155): ``/reload-mcp`` reconciles config and leaves a live,
still-enabled server connected to the pre-rebuild inode, so a swapped executable kept serving for
hours. Real stdio children here: the child records its PID and its tool description comes from the
script's own source, so the new text can only be served by a process started from the new file.
"""

import textwrap

import pytest

SERVER = textwrap.dedent('''\
    import os, pathlib
    from mcp.server.mcpserver import MCPServer
    (pathlib.Path(__file__).parent / "pid.txt").write_text(str(os.getpid()))
    app = MCPServer("swap")

    @app.tool()
    def which() -> str:
        """VERSION"""
        return "VERSION"

    app.run()
''')

LAUNCHER = '#!/bin/sh\nexec "{python}" "$(dirname "$0")/server.py"\n'


def _write_server(folder, version, python):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "server.py").write_text(SERVER.replace("VERSION", version), encoding="utf-8")
    (folder / "run.sh").write_text(LAUNCHER.format(python=python), encoding="utf-8")
    (folder / "run.sh").chmod(0o755)


@pytest.mark.no_isolate
@pytest.mark.platforms("posix")
def test_restart_respawns_the_named_server_from_the_rebuilt_file(monkeypatch, tmp_path):
    pytest.importorskip("mcp.server.mcpserver")
    import sys

    home = tmp_path / "home"
    srv_dir, other = home / "swap", tmp_path / "keep"
    _write_server(srv_dir, "v1", sys.executable)
    _write_server(other, "kept", sys.executable)
    (home / "config.yaml").write_text(
        "mcp_servers:\n"
        "  swap:\n    command: ./run.sh\n"
        f"    cwd: {srv_dir}\n"
        "  keep:\n    command: ./run.sh\n"
        f"    cwd: {other}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from tools import mcp_tool_discovery as disc
    from tools import mcp_tool_loop as _loop
    from tools.mcp_tool_common import _core

    def _pid(where):
        return int((where / "pid.txt").read_text(encoding="utf-8").strip())

    try:
        disc.discover_mcp_tools()
        first_pid, kept_pid = _pid(srv_dir), _pid(other)
        assert _core._servers["swap"]._tools[0].description == "v1"

        # Rebuild the server on disk: same config, same launch inputs, new code.
        _write_server(srv_dir, "v2", sys.executable)

        assert disc.restart_mcp_servers({"swap"}) == ["swap"]
        assert _pid(srv_dir) != first_pid, "the named server must be a new process"
        assert _core._servers["swap"]._tools[0].description == "v2", "the rebuilt file must be what runs"
        assert _pid(other) == kept_pid, "a server the call did not name keeps its process"

        # A name that is not configured is a no-op, not an error.
        assert disc.restart_mcp_servers({"not-configured"}) == []
    finally:
        from tools.mcp_tool_lifecycle import shutdown_mcp_servers
        shutdown_mcp_servers()
        _loop._stop_mcp_loop()
