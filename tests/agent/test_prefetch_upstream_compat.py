"""Upstream-drift contract suite for the prefetch compression fork.

The fork is a version-locked patch over `main`: Hermes updates WILL refactor the
host seams (conversation_compression, context_compressor, agent_init, gate
files). These tests exist so that breakage is loud and MECHANICAL to diagnose:
each failure names the seam and the expected contract. They assert API shape
(symbols exist, signatures accept the keywords the fork passes, config plumbing
behaves) — with ONE deliberate source guard (test_agent_attr_wiring_contract)
for the agent_init assignment line, which has no cheap runtime seam.

When an upstream update breaks this file, adjust the fork against the new
contract, not the tests.
"""

from __future__ import annotations

import inspect

import pytest


def _expect_callable(mod_name: str, name: str):
    mod = pytest.importorskip(mod_name)
    obj = getattr(mod, name, None)
    assert callable(obj), (
        f"PREFETCH-FORK DRIFT: {mod_name}.{name} missing/unusable — an upstream "
        f"refactor removed it; adjust agent/turn_prefetch_compaction.py and the gates."
    )
    return obj


def _expect_param(fn, param: str, what: str):
    sig = inspect.signature(fn)
    assert param in sig.parameters, (
        f"PREFETCH-FORK DRIFT: {fn.__module__}.{fn.__name__} lost keyword "
        f"'{param}' ({what}); adjust the prefetch call sites."
    )


def test_conversation_compression_host_symbols():
    """Every host symbol turn_prefetch_compaction imports must exist."""
    for name in (
        "CompressionCommitFence",
        "_try_admit_compression_job",
        "_release_compression_admission",
        "_get_compress_timeout_executor",
        "resolve_context_compression_timeouts",
        "compress_context",
        "conversation_history_after_compression",
    ):
        _expect_callable("agent.conversation_compression", name)


def test_compress_context_keyword_contract():
    """compress_context must keep the kwargs the prefetch worker passes."""
    fn = _expect_callable("agent.conversation_compression", "compress_context")
    _expect_param(fn, "approx_tokens", "prefetch passes the armed token count")
    _expect_param(fn, "task_id", "prefetch passes the effective task id")
    _expect_param(fn, "commit_fence", "prefetch fences its worker for /stop")


def test_fence_api_contract():
    """The commit fence must keep the methods the prefetch launch uses."""
    Fence = pytest.importorskip("agent.conversation_compression").CompressionCommitFence
    for method in ("set_total_ceiling_seconds", "begin_commit", "finish_commit"):
        assert callable(getattr(Fence, method, None)), (
            f"PREFETCH-FORK DRIFT: CompressionCommitFence lost '{method}'; "
            f"revisit agent/turn_prefetch_compaction.launch_prefetch_compression."
        )


def test_timeouts_resolve_to_float_pair():
    resolve = _expect_callable(
        "agent.conversation_compression", "resolve_context_compression_timeouts"
    )
    idle, ceiling = resolve()
    assert isinstance(idle, float) and isinstance(ceiling, float), (
        "PREFETCH-FORK DRIFT: resolve_context_compression_timeouts no longer "
        "returns (idle, ceiling) floats."
    )


def test_compression_executor_submit_contract():
    executor = _expect_callable(
        "agent.conversation_compression", "_get_compress_timeout_executor"
    )
    exec_ = executor()
    assert callable(getattr(exec_, "submit", None)), (
        "PREFETCH-FORK DRIFT: compression executor lost .submit; "
        "the prefetch worker cannot be launched."
    )


def test_admission_gate_pair_roundtrip():
    """_try_admit/_release must be callable as the launch uses them."""
    cc = pytest.importorskip("agent.conversation_compression")
    admit = cc._try_admit_compression_job
    release = cc._release_compression_admission
    if admit():
        release()


def test_persisted_marker_symbol():
    """The marker the ballot requires on every row must keep existing."""
    marker = getattr(pytest.importorskip("agent.context_compressor"), "_DB_PERSISTED_MARKER", None)
    assert isinstance(marker, str) and marker, (
        "PREFETCH-FORK DRIFT: _DB_PERSISTED_MARKER missing; the arming "
        "persistence invariant (region_fully_persisted) is broken."
    )


def test_gate_imports_resolve():
    """The gate call sites must keep importing the renamed module/api."""
    mod = pytest.importorskip("agent.turn_prefetch_compaction")
    for name in (
        "prefetch_trigger_tokens",
        "can_prefetch",
        "adopt_prefetch_result",
        "launch_prefetch_compression",
        "run_prefetch_compaction_step",
    ):
        assert callable(getattr(mod, name, None)), (
            f"PREFETCH-FORK DRIFT: agent.turn_prefetch_compaction lost '{name}'."
        )


def test_step_signature_contract():
    """run_prefetch_compaction_step must keep its keyword interface."""
    step = getattr(
        pytest.importorskip("agent.turn_prefetch_compaction"),
        "run_prefetch_compaction_step",
    )
    sig = inspect.signature(step)
    assert "tokens" in sig.parameters and "system_message" in sig.parameters, (
        "PREFETCH-FORK DRIFT: run_prefetch_compaction_step signature changed; "
        "update all gate call sites."
    )


def test_gate_function_names_contract():
    """The gate functions the fork patches must keep being importable."""
    mod = pytest.importorskip("agent.turn_context_compaction")
    for fn in ("_preflight_compression", "_run_preflight_passes"):
        assert callable(getattr(mod, fn, None)), (
            f"PREFETCH-FORK DRIFT: turn_context_compaction.{fn} renamed/moved; "
            f"re-wire the prefetch ballot."
        )
    pre = pytest.importorskip("agent.turn_preflight")
    for fn in ("compress_after_tool_results",):
        assert callable(getattr(pre, fn, None)), (
            f"PREFETCH-FORK DRIFT: turn_preflight.{fn} renamed/moved; "
            f"re-wire the prefetch ballot."
        )


def test_prefetch_margin_config_contract():
    """The config->settings plumbing for prefetch_margin must survive rebases.

    This is the seam most likely to silently vanish when upstream rewrites
    _parse_compression_config: the key would be ignored, the feature silently
    off. Values: parsed as float, negatives clamped to 0, garbage -> 0.
    """
    from types import SimpleNamespace

    ai = pytest.importorskip("agent.agent_init")
    agent = SimpleNamespace(api_mode=None, model="test/model")

    out = ai._parse_compression_config(agent, {"compression": {"prefetch_margin": 0.05}})
    assert out.prefetch_margin == 0.05, (
        "PREFETCH-FORK DRIFT: compression.prefetch_margin no longer reaches "
        "CompressionSettings — re-apply the parse hook in agent_init.py."
    )
    assert ai._parse_compression_config(agent, {}).prefetch_margin == 0.0
    assert ai._parse_compression_config(
        agent, {"compression": {"prefetch_margin": -1.5}}
    ).prefetch_margin == 0.0
    assert ai._parse_compression_config(
        agent, {"compression": {"prefetch_margin": "garbage"}}
    ).prefetch_margin == 0.0

    # The key must exist in DEFAULT_CONFIG so `hermes config set` treats it as
    # a known setting instead of a custom-key warning path.
    cd = pytest.importorskip("hermes_cli.config_defaults")
    assert "prefetch_margin" in cd.DEFAULT_CONFIG.get("compression", {}), (
        "PREFETCH-FORK DRIFT: DEFAULT_CONFIG lost compression.prefetch_margin."
    )


def test_agent_attr_wiring_contract():
    """agent_init must keep assigning the attribute the module reads."""
    ai = pytest.importorskip("agent.agent_init")
    src = inspect.getsource(ai)
    assert "agent.compression_prefetch_margin" in src, (
        "PREFETCH-FORK DRIFT: init_agent no longer assigns "
        "agent.compression_prefetch_margin — the module would silently see the "
        "0.0 default and never arm."
    )