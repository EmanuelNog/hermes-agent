"""``-m <alias | provider:model | provider/model>`` at ``session.create``, resolved by the owner."""
from __future__ import annotations


def resolve_launch_route(params: dict, config: dict) -> dict:
    """Return ``params`` with an explicit launch model decoded the way every other startup path does.

    The client sends the raw ``-m`` string; the authority owns the profile whose ``model_aliases:``
    / ``providers:`` define it. Unresolved, the alias name becomes the model sent to the profile
    DEFAULT provider, and the alias's own endpoint/key are never used (#103933/#107191/#109440).
    Same owner as HermesCLI / oneshot / the TUI factory: ``resolve_startup_model_route``. A plain
    model id (no alias, no qualifier) is returned untouched; explicit ``--base-url``/``--api-key``
    launches already carry their route.

    A ``--provider`` pick with no ``-m`` is the mirror case, decided here too: the pick supplies its
    own model instead of inheriting the profile default's (:func:`_bind_provider_default`).
    """
    model = params.get('model')
    if not isinstance(model, str) or not model.strip():
        return _bind_provider_default(params, config)
    if 'base_url' in params or 'api_key' in params:
        return params
    from hermes_cli.model_switch import resolve_startup_model_route
    model_cfg = config.get('model')
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    explicit_provider = str(params.get('provider') or '').strip()
    route = resolve_startup_model_route(
        model, explicit_provider=explicit_provider,
        current_provider=(explicit_provider or str(model_cfg.get('provider') or '')).strip().lower(),
        user_providers=config.get('providers'), custom_providers=config.get('custom_providers'),
        alias_config=config)  # this snapshot's aliases, not a cache keyed on config.yaml alone
    if route is None:
        return params
    resolved = dict(params, model=route.model)
    if route.provider:
        resolved['provider'] = route.provider
    if route.base_url:
        resolved['base_url'] = route.base_url
    if route.api_key:
        # An alias key is a launch key: authority memory only, revoked by restart (bind_launch_key).
        resolved['api_key'] = route.api_key
    return resolved


def _picked_provider(params: dict) -> str:
    """The provider a create picked, or ``''`` for none / ``auto`` — the thin client's "no pick"."""
    provider = params.get('provider')
    if not isinstance(provider, str):
        return ''
    provider = provider.strip()
    return '' if provider.lower() == 'auto' else provider


def _provider_default_model(provider: str, config: dict) -> str:
    """The model *provider* serves by default: its own entry's ``default_model``, else its catalog's."""
    from hermes_cli.runtime_provider_custom import _get_named_custom_provider
    entry = _get_named_custom_provider(provider, config=config, resolve_credentials=False)
    if entry is not None:  # a configured named endpoint: its ``default_model`` arrives as ``model``
        return str(entry.get('model') or '').strip()
    from hermes_cli.models import get_default_model_for_provider
    return str(get_default_model_for_provider(provider) or '').strip()


def _bind_provider_default(params: dict, config: dict) -> dict:
    """A ``--provider`` pick with no ``-m`` takes that provider's own default model.

    Left unset, the create froze the PROFILE's default model against the picked provider: the runtime
    then found no such model on the new endpoint and quietly sent the turn to the profile's own
    provider (measured 2026-11-28 — ``--provider pc2-inkling`` answered from the cloud while the local
    box slept). A pick that resolves to the profile's own route keeps the profile's model, and a pick
    nothing can supply a default for is refused here rather than silently ignored.
    """
    provider = _picked_provider(params)
    if not provider or 'base_url' in params or 'api_key' in params:
        return params
    from gateway.session_policy import _keeps_profile_route
    if _keeps_profile_route(provider, config):
        return params
    model = _provider_default_model(provider, config)
    if not model:
        from persistence.core.errors import AuthorityRefusal
        raise AuthorityRefusal('invalid_params', field='provider',
                               detail=f'{provider} serves no default model; name one with -m/--model')
    return dict(params, model=model)
