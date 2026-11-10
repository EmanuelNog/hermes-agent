"""E2E tests for gateway slash commands (Telegram, Discord).

Each test drives a message through the full async pipeline:
    adapter.handle_message(event)
        → BasePlatformAdapter._process_message_background()
        → GatewayRunner._handle_message() (command dispatch)
        → adapter.send() (captured for assertions)

No LLM involved — only gateway-level commands are tested.
Tests are parametrized over platforms via the ``platform`` fixture in conftest.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from tests.e2e.conftest import send_and_capture


class TestSlashCommands:
    """Gateway slash commands dispatched through the full adapter pipeline."""

    @pytest.mark.asyncio
    async def test_help_returns_command_list(self, adapter, platform):
        send = await send_and_capture(adapter, "/help", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "/new" in response_text
        assert "/status" in response_text


    @pytest.mark.asyncio
    async def test_new_resets_session(self, adapter, runner, platform):
        send = await send_and_capture(adapter, "/new", platform)

        send.assert_called_once()
        runner.session_store.reset_session.assert_called_once()


    @pytest.mark.asyncio
    async def test_leading_space_stop_is_still_a_command(self, adapter, platform):
        """Slack users type `` /stop`` to avoid native Slack slash interception."""
        send = await send_and_capture(adapter, " /stop", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        response_lower = response_text.lower()
        assert "no" in response_lower or "stop" in response_lower or "not running" in response_lower




    @pytest.mark.asyncio
    async def test_plaintext_restart_gateway_routes_to_safe_restart_command(self, adapter, runner, platform, monkeypatch):
        if platform != Platform.TELEGRAM:
            pytest.skip("Plaintext restart shortcut is intentionally DM/Telegram-focused")

        monkeypatch.setenv("INVOCATION_ID", "e2e-systemd")
        runner.request_restart = MagicMock(return_value=True)

        send = await send_and_capture(adapter, "restart gateway", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "restart" in response_text.lower() or "draining" in response_text.lower()
        runner.request_restart.assert_called_once_with(detached=False, via_service=True)

    @pytest.mark.asyncio
    async def test_plaintext_restart_gateway_in_group_stays_plain_text(self, adapter, runner, platform, monkeypatch):
        if platform != Platform.TELEGRAM:
            pytest.skip("Shortcut scope is only verified for Telegram here")

        monkeypatch.setenv("INVOCATION_ID", "e2e-systemd")
        runner.request_restart = MagicMock(return_value=True)
        runner._handle_message_with_agent = AsyncMock(return_value="agent-handled")

        send = await send_and_capture(adapter, "restart gateway", platform, chat_id="group-chat-1", user_id="u1", chat_type="group")

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert response_text == "agent-handled"
        runner.request_restart.assert_not_called()




    @pytest.mark.asyncio
    async def test_quick_command_alias_targets_builtin_command_with_args(
        self, adapter, runner, platform
    ):
        """Alias targets with args must reach the built-in command handler."""
        # Key must not collide with a registry command or alias: built-ins keep
        # precedence over quick commands (see _hm_resolve_command), so a key like
        # "s" (the /steer alias) would never reach the quick-command expansion.
        runner.config.quick_commands = {
            "x": {"type": "alias", "target": "/status extra-arg"}
        }
        async def _handle_status(event):
            assert event.get_command_args() == "extra-arg"
            return "status via alias"

        runner._handle_status_command = AsyncMock(side_effect=_handle_status)

        send = await send_and_capture(adapter, "/x", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert response_text == "status via alias"
        runner._handle_status_command.assert_awaited_once()
        runner._handle_message_with_agent.assert_not_awaited()



class TestSessionLifecycle:
    """Verify session state changes across command sequences."""

    @pytest.mark.asyncio
    async def test_new_then_status_reflects_reset(self, adapter, runner, session_entry, platform):
        """After /new, /status should report the fresh session."""
        await send_and_capture(adapter, "/new", platform)
        runner.session_store.reset_session.assert_called_once()

        send = await send_and_capture(adapter, "/status", platform)
        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        # Session ID from the entry should appear in the status output
        assert session_entry.session_id[:8] in response_text



class TestAuthorization:
    """Verify the pipeline handles unauthorized users."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_pairing_response(self, adapter, runner, platform):
        """Unauthorized DM should trigger pairing code, not a command response."""
        runner._is_user_authorized = lambda _source: False

        # Poll for the send (send_and_capture) instead of a fixed delay: the ingress gates
        # before the authorization branch do profile-scoped reads and thread hops, so the
        # pairing offer lands well after handle_message returns.
        send = await send_and_capture(adapter, "/help", platform)

        # The adapter.send is called directly by the authorization path
        # (not via _send_with_retry), so check it was called with a pairing message
        send.assert_called()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "recognize" in response_text.lower() or "pair" in response_text.lower() or "ABC123" in response_text

    @pytest.mark.asyncio
    async def test_unauthorized_pairing_survives_slow_ingress(self, adapter, runner, platform):
        """A pairing offer must not be lost when ingress takes longer than a fixed window.

        The ingress gates before the authorization branch do profile-scoped reads and thread
        hops (``_authorization_home_for_source``, plugin hooks), so the offer lands well after
        ``handle_message`` returns. A fixed settle delay makes that a coin flip; polling for the
        send is what this asserts.
        """
        runner._is_user_authorized = lambda _source: False

        async def _slow_hook(event, _source):
            await asyncio.sleep(0.4)
            return event

        runner._hm_pre_gateway_dispatch_hook = _slow_hook

        send = await send_and_capture(adapter, "/help", platform)

        send.assert_called()

    @pytest.mark.asyncio
    async def test_unauthorized_user_does_not_get_help(self, adapter, runner, platform):
        """Unauthorized user should NOT see the help command output."""
        runner._is_user_authorized = lambda _source: False

        send = await send_and_capture(adapter, "/help", platform)

        # Whatever was sent (the pairing offer, or nothing), it must never be the help text.
        for call in send.call_args_list:
            response_text = call.kwargs.get("content") or (call.args[1] if len(call.args) > 1 else "")
            assert "/new" not in response_text


class TestSendFailureResilience:
    """Verify the pipeline handles send failures gracefully."""

    @pytest.mark.asyncio
    async def test_send_failure_does_not_crash_pipeline(self, adapter, platform):
        """If send() returns failure, the pipeline should not raise."""
        adapter.send = AsyncMock(return_value=SendResult(success=False, error="network timeout"))
        adapter.set_message_handler(adapter._message_handler) # re-wire with same handler

        # Poll for the send: a fixed delay races the same ingress thread hops.
        send = await send_and_capture(adapter, "/help", platform)

        send.assert_called()
