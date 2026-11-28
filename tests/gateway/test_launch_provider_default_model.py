"""``--provider <name>`` without ``-m`` must bind that provider's own default model, never the
profile default's.

Measured 2026-11-28: ``hermes chat -q ... --provider pc2-inkling --oneshot`` answered from
opencode-go/deepseek-v4.1-flash with no warning. ``resolve_launch_route`` kept the provider and
left ``model`` unset, so ``build_policy`` froze ``model: {provider: pc2-inkling, default:
<profile default>}``; the runtime then found no such model on that endpoint and quietly routed the
turn to the profile's default provider.
"""
from __future__ import annotations

import pytest
from persistence.core.errors import AuthorityRefusal

LOCAL = {"api": "http://127.0.0.1:2/v1", "default_model": "model-named"}
LOCAL_OTHER = {"api": "http://127.0.0.1:3/v1", "default_model": "model-other"}


def _config(**model):
    return {
        "model": {"provider": "opencode-go", "default": "model-main", **model},
        "providers": {"named-host": dict(LOCAL), "other-host": dict(LOCAL_OTHER)},
    }


def test_provider_pick_without_model_binds_the_entry_default():
    from gateway.session_local_route import resolve_launch_route
    out = resolve_launch_route({"provider": "named-host", "source": "cli"}, _config())
    assert (out["model"], out["provider"]) == ("model-named", "named-host")


def test_provider_pick_targets_its_own_endpoint_through_build_policy():
    import json
    from gateway.session_local_route import resolve_launch_route
    from gateway.session_route_identity import resolved_route
    from gateway.session_policy import build_policy
    config = _config()
    out = resolve_launch_route({"provider": "named-host", "source": "cli", "cwd": "/tmp"}, config)
    policy = build_policy(dict(out), config, private_secrets={})
    assert (policy.model, policy.provider) == ("model-named", "named-host")
    # the frozen route still resolves to the entry's own endpoint, never the profile default's
    assert resolved_route(json.loads(policy.config_json)["model"], config) == (
        "custom:named-host", "http://127.0.0.1:2/v1")


def test_builtin_provider_pick_without_model_binds_its_catalog_default():
    from gateway.session_local_route import resolve_launch_route
    out = resolve_launch_route({"provider": "anthropic", "source": "cli"}, _config())
    from hermes_cli.models import get_default_model_for_provider
    assert out["model"] == get_default_model_for_provider("anthropic") != "model-main"


def test_same_route_pick_keeps_the_profile_model():
    """The profile already runs this provider: its configured default is the pick's model."""
    from gateway.session_local_route import resolve_launch_route
    config = _config()
    config["model"] = {"provider": "named-host", "default": "model-main"}
    assert resolve_launch_route({"provider": "named-host", "source": "cli"}, config) == {
        "provider": "named-host", "source": "cli"}


def test_auto_sentinel_is_not_a_provider_pick():
    """``provider: auto`` is the thin client's "no pick": the profile route stands as configured."""
    from gateway.session_local_route import resolve_launch_route
    params = {"provider": "auto", "source": "cli"}
    assert resolve_launch_route(dict(params), _config()) == params


def test_provider_with_no_default_model_is_refused_not_ignored():
    from gateway.session_local_route import resolve_launch_route
    with pytest.raises(AuthorityRefusal) as exc:
        resolve_launch_route({"provider": "no-such-provider", "source": "cli"}, _config())
    assert exc.value.args[0] == "invalid_params"


def test_explicit_endpoint_and_model_launches_pass_through():
    from gateway.session_local_route import resolve_launch_route
    explicit = {"provider": "named-host", "base_url": "http://127.0.0.1:9/v1", "source": "cli"}
    assert resolve_launch_route(dict(explicit), _config()) == explicit
