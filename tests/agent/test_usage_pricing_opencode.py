"""OpenCode Console pricing: Zen is pay-as-you-go, Go is the subscription.

Zen and Go share a host (opencode.ai) and a model catalogue, but they bill
differently:

  opencode-zen  pay-as-you-go, charged per request against console credits --
                published rates on opencode.ai/docs/console/models (#pricing)
  opencode-go   a monthly plan with included usage; a metered price would be a
                counterfactual, not an invoice

Before this file both resolved to no pricing entry at all, so every Zen session
recorded ``cost_status=unknown`` and ``$0`` -- real money looking free.
"""

from decimal import Decimal

import pytest

from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    get_pricing_entry,
    resolve_billing_route,
)

ZEN_URL = "https://opencode.ai/zen/v1"
GO_URL = "https://opencode.ai/zen/go/v1"


def test_zen_list_prices_are_known():
    entry = get_pricing_entry("deepseek-v4.1-flash", provider="opencode-zen", base_url=ZEN_URL)
    assert entry is not None, "no pricing entry for a pay-as-you-go Zen route"
    assert entry.input_cost_per_million == Decimal("0.30")
    assert entry.output_cost_per_million == Decimal("1.20")
    assert entry.cache_read_cost_per_million == Decimal("0.006")


def test_the_legacy_opencode_id_resolves_to_zen():
    entry = get_pricing_entry("deepseek-v4.1-flash", provider="opencode", base_url=ZEN_URL)
    assert entry is not None
    assert entry.input_cost_per_million == Decimal("0.30")


def test_zen_usage_carries_real_money():
    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    result = estimate_usage_cost("deepseek-v4.1-flash", usage, provider="opencode-zen", base_url=ZEN_URL)
    assert result.amount_usd is not None
    # 0.30 + 1.20 + 0.006
    assert float(result.amount_usd) == pytest.approx(1.506, abs=1e-6)
    assert result.status != "unknown"


@pytest.mark.parametrize("provider", ["opencode-go", "opencode-go-2", "opencode-go-3"])
def test_go_routes_are_subscription_included_not_unknown(provider):
    route = resolve_billing_route("deepseek-v4.1-flash", provider=provider, base_url=GO_URL)
    assert route.billing_mode == "subscription_included"
    result = estimate_usage_cost(
        "deepseek-v4.1-flash",
        CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000),
        provider=provider, base_url=GO_URL,
    )
    assert result.status == "included"
