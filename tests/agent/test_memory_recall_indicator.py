"""MemoryManager.describe_recall — the deterministic recall indicator.

When auto-recall injects memory, Hermes surfaces a model-independent
"🧠 <provider> — recalled N memories" status line so the user SEES memory
working regardless of whether the model chooses to mention it. These tests
lock the formatting (singular/plural/generic) and the aggregation across
providers, all deterministically (no LLM, no network).
"""
from typing import Optional

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider, RecallStatus


class _FakeProvider(MemoryProvider):
    """Provider with a settable recall_status for indicator tests."""

    def __init__(self, name: str, status: Optional[RecallStatus], *, raises: bool = False):
        self._name = name
        self._status = status
        self._raises = raises

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs) -> str:
        return ""

    def recall_status(self) -> Optional[RecallStatus]:
        if self._raises:
            raise RuntimeError("boom")
        return self._status


def test_no_status_returns_empty_string():
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("hindsight", None))
    assert mgr.describe_recall() == ""


def test_aggregates_multiple_providers():
    # builtin is always accepted first; a second external is rejected, so use
    # builtin + one external to exercise the join path.
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("builtin", RecallStatus("Notes", 2)))
    mgr.add_provider(_FakeProvider("hindsight", RecallStatus("Hindsight", 5)))
    result = mgr.describe_recall()
    assert "Notes" in result and "2" in result
    assert "Hindsight" in result and "5" in result


def test_failing_provider_is_skipped_not_fatal():
    mgr = MemoryManager()
    mgr.add_provider(_FakeProvider("builtin", None, raises=True))
    mgr.add_provider(_FakeProvider("hindsight", RecallStatus("Hindsight", 1)))
    # The raising provider is swallowed; the healthy one still surfaces.
    assert "Hindsight" in mgr.describe_recall()


class _PrefetchManager:
    """MemoryManager stub: prefetch returns a block, so the indicator is emitted."""

    def on_turn_start(self, *args, **kwargs) -> None:
        pass

    def prefetch_all(self, query: str, session_id: str = "") -> str:
        return "Prior Context Hint(automatic): ..."

    def describe_recall(self) -> str:
        return "🧠 Prior Context Hint · 1 shown"


class _EmitRecorder:
    """Agent stub that records what reached the status rail, kind included."""

    _memory_manager = _PrefetchManager()
    session_id = "s-test"
    _user_turn_count = 1

    def __init__(self) -> None:
        self.emitted: list[tuple[str, str]] = []

    def _emit_status(self, message: str) -> None:
        self.emitted.append(("lifecycle", message))

    def _emit_status_kind(self, kind: str, message: str, *, origin: str = "") -> None:
        self.emitted.append((kind, message))


def test_recall_indicator_rides_its_own_status_kind():
    """The indicator goes out as ``recall``, not as a generic ``lifecycle`` line.

    A surface renders kinds it knows: the TUI keeps ``lifecycle`` ambient (it defaults the
    activity section to hidden), so a hint on that rail never reached the user. The dedicated
    kind is what lets the hint be printed as a durable transcript line, like ``compacted``.
    """
    from agent.turn_context import _memory_turn_start_and_prefetch

    agent = _EmitRecorder()
    injected = _memory_turn_start_and_prefetch(agent, "how does the hint system work", None, "")

    assert injected == "Prior Context Hint(automatic): ..."
    assert agent.emitted == [("recall", "🧠 Prior Context Hint · 1 shown")]


def test_tui_providers_get_the_cli_status_sink_on_the_recall_kind():
    """The TUI hands providers the same one-argument status sink the CLI does, tagged ``recall``.

    A provider that owns its own rendering (header plus one row per hint) can only paint that
    block through the sink. Without it the hint collapses to a RecallStatus one-liner, and the
    crops — the part the user reads — never appear.
    """
    from types import SimpleNamespace

    from agent.agent_init_memory_provider import _memory_provider_init_kwargs

    emitted: list[tuple[str, str]] = []

    class _Agent:
        session_id = "s-test"
        _session_db = None
        session_cwd = None

        def __init__(self) -> None:
            self.emitted = emitted

        def __getattr__(self, _name: str):
            # Gateway identity attrs (_user_id, _chat_id, ...) read unconditionally: absent here.
            return None

        def _emit_status(self, message: str) -> None:
            self.emitted.append(("lifecycle", message))

        def _emit_status_kind(self, kind: str, message: str, origin: str = "") -> None:
            self.emitted.append((kind, message))

    agent = _Agent()

    kwargs = _memory_provider_init_kwargs(agent, "tui")

    assert callable(kwargs.get("status_callback")), "TUI providers must receive a status sink"
    kwargs["status_callback"]("🧠 Prior Context Hint · 2 shown")
    assert emitted == [("recall", "🧠 Prior Context Hint · 2 shown")]
