"""Nous Portal inference prices, as published in the portal today (2026-11-09).

The portal lists input/output only and carries several stamped DeepSeek variants at
once, so the entry is keyed on the model id we actually record:

    DeepSeek V4.1 Flash 1017   in 0.15  out 0.60
    DeepSeek V4.1 Flash        in 0.11  out 0.51
    DeepSeek V4 Flash          in 0.05  out 0.16
    DeepSeek V4 Flash 0423     in 0.09  out 0.18
    DeepSeek V4 Pro 0423       in 0.94  out 1.89

Cache read is not published by the portal; it follows DeepSeek's own cache-hit ratio
(2% of input) for the same model generation, which is what the provider upstream bills.
"""

from decimal import Decimal

import pytest

from agent.usage_pricing import CanonicalUsage, estimate_usage_cost, get_pricing_entry

NOUS = {"base_url": "https://inference-api.nousresearch.com/v1"}


@pytest.mark.parametrize("model,inp,out,cache", [
    ("deepseek/deepseek-v4.1-flash-1017", "0.15", "0.60", "0.003"),
    ("deepseek/deepseek-v4.1-flash", "0.11", "0.51", "0.0022"),
    ("deepseek/deepseek-v4-flash", "0.05", "0.16", "0.001"),
    ("deepseek/deepseek-v4-flash-0423", "0.09", "0.18", "0.0018"),
    ("deepseek/deepseek-v4-pro-0423", "0.94", "1.89", "0.0188"),
])
def test_nous_portal_prices_resolve(model, inp, out, cache):
    entry = get_pricing_entry(model, provider="nous", base_url=NOUS["base_url"])
    assert entry is not None, f"no pricing entry for nous route {model}"
    assert entry.input_cost_per_million == Decimal(inp)
    assert entry.output_cost_per_million == Decimal(out)
    assert entry.cache_read_cost_per_million == Decimal(cache)


def test_nous_cost_uses_the_portal_rates_not_the_deepseek_list():
    # 1M of each bucket on V4 Flash: 0.05 + 0.001 + 0.16
    result = estimate_usage_cost(
        "deepseek/deepseek-v4-flash",
        CanonicalUsage(input_tokens=1_000_000, cache_read_tokens=1_000_000, output_tokens=1_000_000),
        provider="nous", base_url=NOUS["base_url"])
    assert result.amount_usd is not None
    assert float(result.amount_usd) == pytest.approx(0.211, abs=1e-6)
