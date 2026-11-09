"""A stale, token-less ``providers.nous`` record must not read as a live sign-in.

Regression for the 2026-10-25 credential revocation: the Nous refresh session was
revoked (``last_auth_error.code == "invalid_grant"``, ``relogin_required: True``)
and the provider record kept its metadata but lost ``access_token`` /
``refresh_token``. Both sign-in gates tested only for the EXISTENCE of that
record, so ``/login`` answered "Already signed in." forever and every web call
fell back to the keyless free tier.
"""

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import anon_auth, anon_sign_in


def _source(*, chat_type="dm", chat_id="chat-1", user_id="user-1"):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat_id, chat_type=chat_type, user_id=user_id)


def _runner(monkeypatch, state):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={
        Platform.TELEGRAM: PlatformConfig(enabled=True, token="token", extra={})})
    runner.adapters = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.session_store = MagicMock()
    runner._async_session_store = MagicMock(set_model_override=AsyncMock())
    runner._async_session_store._store = runner.session_store
    runner._deliver_platform_notice = AsyncMock()
    runner._evict_cached_agent = MagicMock()

    async def inline(func):
        return func()

    runner._run_login_blocking = inline
    monkeypatch.setattr(anon_auth, "current_nous_state", lambda: state)
    return runner


def _stale_state():
    """The exact shape found in auth.json after the revocation: metadata, no tokens."""
    return {
        "client_id": "hermes-cli",
        "portal_base_url": "https://portal.nousresearch.com",
        "inference_base_url": "https://inference-api.nousresearch.com/v1",
        "scope": "inference:invoke",
        "token_type": "bearer",
        "tls": {"insecure": False, "ca_bundle": None},
        "last_auth_error": {
            "provider": "nous",
            "message": "Refresh session has been revoked",
            "reason": "credential_pool_refresh_failure",
            "relogin_required": True,
            "code": "invalid_grant",
        },
    }


def _live_state():
    return {
        "client_id": "hermes-cli",
        "auth_method": "oauth_device_code",
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": "2099-01-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_a_tokenless_state_does_not_answer_already_signed_in(monkeypatch):
    runner = _runner(monkeypatch, _stale_state())
    monkeypatch.setattr(anon_auth, "run_sign_in", lambda **_kwargs: iter([anon_auth.TimedOut()]))

    result = await runner._handle_login_command(SimpleNamespace(source=_source()))

    assert result != anon_auth.UPGRADE_ALREADY_SIGNED_IN
    assert result == anon_auth.UPGRADE_START
    tasks = list(getattr(runner, "_background_tasks", ()))
    for task in tasks:
        await task


@pytest.mark.asyncio
async def test_a_live_state_still_answers_already_signed_in(monkeypatch):
    runner = _runner(monkeypatch, _live_state())

    result = await runner._handle_login_command(SimpleNamespace(source=_source()))

    assert result == anon_auth.UPGRADE_ALREADY_SIGNED_IN


@pytest.mark.asyncio
async def test_relogin_required_wins_over_a_leftover_access_token(monkeypatch):
    state = {
        "auth_method": "oauth_device_code",
        "refresh_token": "rt",
        "last_auth_error": {"relogin_required": True, "code": "invalid_grant"},
    }
    runner = _runner(monkeypatch, state)
    monkeypatch.setattr(anon_auth, "run_sign_in", lambda **_kwargs: iter([anon_auth.TimedOut()]))

    assert await runner._handle_login_command(
        SimpleNamespace(source=_source())) == anon_auth.UPGRADE_START
    for task in list(getattr(runner, "_background_tasks", ())):
        await task


def test_has_nous_identity_reads_the_credential_not_the_record():
    assert anon_auth.has_nous_identity(_live_state()) is True
    assert anon_auth.has_nous_identity(_stale_state()) is False
    assert anon_auth.has_nous_identity(None) is False
    assert anon_auth.has_nous_identity({}) is False
    assert anon_auth.has_nous_identity({"refresh_token": "rt"}) is True
    assert anon_auth.has_nous_identity(
        {"auth_method": anon_auth.ANON_AUTH_METHOD, "access_token": "x"}) is False


def test_the_cli_sign_in_flow_is_not_short_circuited_by_a_stale_record(monkeypatch):
    monkeypatch.setattr(anon_auth, "current_nous_state", lambda: _stale_state())

    def boom(*_args, **_kwargs):
        raise RuntimeError("no network in this test")

    first = next(anon_sign_in.run_sign_in(client_factory=boom))

    assert not isinstance(first, anon_sign_in.AlreadySignedIn)
