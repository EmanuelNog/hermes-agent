"""Conversation-affinity request headers for session-aware relays and proxies.

Two sources, one merge point:

* ``x-opencode-session`` — OpenCode (opencode.ai Zen/Go relay) pins requests that share this
  value to the same upstream backend, which keeps its prompt cache warm across the turns of one
  conversation. Always sent to OpenCode targets.
* ``providers.<name>.session_affinity_header`` — an opt-in header NAME on a custom provider entry
  (default off). Session-aware proxies fronting a stateful backend (LiteLLM's ``x-litellm-session-id``,
  self-hosted Claude/OpenAI gateways) otherwise classify an agent-loop request whose last message is
  a ``tool_result`` as a new conversation and replay the whole history upstream (#86241, #104449).

The value only has to be opaque and consistent per conversation, so it is derived the same way as
the other affinity hints Hermes already sends (OpenRouter's sticky ``session_id``, xAI's
``x-grok-conv-id``): the host-declared routing scope first (a host that names its own conversation,
#96811), then the ambient conversation ROOT (stable across compaction rotation and delegate trees),
then the physical session id — normalized through ``_cache_scope_from_session_id`` so cron fires of
one job share a scope. Auxiliary calls (compression, titles, vision, MoA) have no session handle and
resolve the ambient value, so they stay on the conversation's backend too (#70820).

Every request — main turn on any transport, auxiliary calls — goes through
:func:`merge_session_affinity_headers` so the headers cannot drift per code path.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("run_agent")

OPENCODE_SESSION_HEADER = "x-opencode-session"

# Mirrors ``hermes_cli.models._OPENCODE_FAMILIES``; used only when that module cannot be imported,
# so the fallback can never claim a provider the real matcher would reject.
_OPENCODE_FAMILY_SLUGS = ("opencode-zen", "opencode-go")

# Matcher imports that failed, keyed by import target, valued by "<ExcType>: <message>". Empty in a
# healthy environment; a probe/diagnostic should ERROR on it rather than trust a False answer.
_MATCHER_IMPORT_ERRORS: dict[str, str] = {}


def _note_matcher_breakage(what: str, exc: BaseException) -> None:
    """Record (once per distinct failure) that an OpenCode predicate could not be imported."""
    detail = f"{type(exc).__name__}: {exc}"
    if _MATCHER_IMPORT_ERRORS.get(what) == detail:
        return
    _MATCHER_IMPORT_ERRORS[what] = detail
    logger.warning(
        "opencode affinity: %s unavailable (%s) — resolving targets from provider name / host",
        what,
        detail,
    )


def matcher_breakage() -> dict[str, str]:
    """Import failures seen by :func:`is_opencode_target` (``{}`` when the matchers loaded)."""
    return dict(_MATCHER_IMPORT_ERRORS)


def _provider_name_is_opencode(provider: Optional[str]) -> bool:
    """Name-shape match for the built-in family slugs and custom entries extending one (#85589)."""
    raw = str(provider or "").strip().lower()
    return bool(raw) and raw.startswith(_OPENCODE_FAMILY_SLUGS)


def _host_is_opencode(base_url: Optional[str]) -> bool:
    """Import-free ``opencode.ai`` host check (fallback for the endpoint matcher)."""
    try:
        host = (urlsplit(str(base_url or "")).hostname or "").lower()
    except Exception:
        return False
    return host == "opencode.ai" or host.endswith(".opencode.ai")


def opencode_transport(provider: Optional[str], model: Optional[str], base_url: Optional[str]) -> tuple[Optional[str], str]:
    """``(api_mode, base_url)`` re-derived per model for an OpenCode relay target; ``(None, base_url)`` otherwise.

    OpenCode Zen/Go serve Responses-only (``gpt-*``, ``grok-*``, ``muse-spark``), Anthropic-wire
    (``minimax-*``, ``qwen*``, ``claude-*``) and chat/completions models behind one provider, so a
    provider-level or persisted ``api_mode`` is wrong for every model but the one it was saved for.
    The main runtime (``hermes_cli/runtime_provider.py``) always re-derives from the effective model;
    auxiliary resolution must agree or ``gpt-5.6-luna`` compression 500s on /chat/completions (#98799).
    Built-in families, custom entries named after one (``opencode-go-bridge``, #85589) and opencode.ai
    hosts all count.
    """
    from hermes_cli.models import normalize_opencode_base_url, normalize_opencode_model_id, opencode_model_api_mode
    from hermes_cli.runtime_provider_custom import _get_named_custom_provider, _opencode_family_for_custom

    url = str(base_url or "")
    family = _opencode_family_for_custom(str(provider or ""), url)
    if family is None:
        return None, url
    # A custom entry that declares its own api_mode keeps it, exactly like the main runtime
    # (_resolve_named_custom_runtime only re-derives when the entry has none).
    if (_get_named_custom_provider(str(provider or "")) or {}).get("api_mode"):
        return None, url
    # ``<provider>/<model>`` config ids are stripped against the entry name before the family lookup.
    api_mode = opencode_model_api_mode(family, normalize_opencode_model_id(provider, model))
    return api_mode, normalize_opencode_base_url(provider, api_mode, url)


def is_opencode_target(provider: Optional[str], base_url: Optional[str]) -> bool:
    """True when *provider* or *base_url* addresses the OpenCode relay.

    Matches the built-in opencode-zen/go providers, custom
    ``opencode-<family>-*`` providers, and any base_url hosted on opencode.ai.

    A broken ENVIRONMENT must not read as "not an OpenCode target": when a matcher cannot be
    imported, the name/host is resolved locally and the breakage is recorded
    (:func:`matcher_breakage`) instead of being swallowed into a ``False`` — the silent-False
    path stripped the session header from every OpenCode request, which is OpenCode Go's
    HTTP 400 MissingSessionID (#105841).
    """
    try:
        from hermes_cli.models import opencode_provider_family
    except Exception as exc:  # missing dependency in this interpreter, not a product answer
        _note_matcher_breakage("hermes_cli.models.opencode_provider_family", exc)
        if _provider_name_is_opencode(provider):
            return True
    else:
        try:
            if opencode_provider_family(provider) is not None:
                return True
        except Exception as exc:
            _note_matcher_breakage("hermes_cli.models.opencode_provider_family", exc)
            if _provider_name_is_opencode(provider):
                return True

    try:
        from agent.anthropic_endpoints import _is_opencode_endpoint
    except Exception as exc:
        _note_matcher_breakage("agent.anthropic_endpoints._is_opencode_endpoint", exc)
        return _host_is_opencode(base_url)
    try:
        return bool(_is_opencode_endpoint(str(base_url or "")))
    except Exception as exc:
        _note_matcher_breakage("agent.anthropic_endpoints._is_opencode_endpoint", exc)
        return _host_is_opencode(base_url)


def resolve_affinity_key(session_id: Optional[str] = None) -> str:
    """Return the normalized rotation-stable conversation affinity key ("" when unknown)."""
    try:
        from agent.portal_tags import get_affinity_scope, get_conversation_context
        from agent.transports.codex import _cache_scope_from_session_id

        return _cache_scope_from_session_id(get_affinity_scope() or get_conversation_context() or session_id)
    except Exception:
        return str(session_id or "")


def opencode_session_headers(
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, str]:
    """Return ``{"x-opencode-session": <key>}`` for OpenCode targets, else ``{}``.

    OpenCode targets always get a key: when no conversation/session key resolves, an
    ephemeral ``oneshot-<hex>`` value is generated (OpenCode Go rejects requests without
    the header, #105841)."""
    if not is_opencode_target(provider, base_url):
        return {}
    key = resolve_affinity_key(session_id)
    if not key:
        # Stateless one-shot requests (commit messages, summaries, standalone prompts outside
        # a session) lack an ambient conversation or session id. OpenCode Go strictly requires
        # x-opencode-session on every request (HTTP 400 MissingSessionID if absent, #105841)
        # so generate an ephemeral session id fallback.
        key = f"oneshot-{uuid.uuid4().hex[:16]}"
    return {OPENCODE_SESSION_HEADER: key}


def custom_provider_session_affinity_headers(
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, str]:
    """Return ``{<session_affinity_header>: <key>}`` when the route's provider entry declares one, else ``{}``."""
    try:
        from hermes_cli.config import get_custom_provider_session_affinity_header

        header = get_custom_provider_session_affinity_header(str(base_url or ""))
    except Exception:
        return {}
    if not header:
        return {}
    key = resolve_affinity_key(session_id)
    return {header: key} if key else {}


def merge_session_affinity_headers(
    kwargs: dict[str, Any],
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Merge the affinity header(s) into ``kwargs["extra_headers"]`` (in place).

    Existing per-request headers win, so a caller-pinned value is preserved.
    Targets with neither source configured are left untouched.
    """
    headers = opencode_session_headers(provider, base_url, session_id)
    headers.update(custom_provider_session_affinity_headers(base_url, session_id))
    if headers:
        existing = kwargs.get("extra_headers")
        merged = dict(existing) if isinstance(existing, dict) else {}
        for key, value in headers.items():
            merged.setdefault(key, value)
        kwargs["extra_headers"] = merged
    return kwargs
