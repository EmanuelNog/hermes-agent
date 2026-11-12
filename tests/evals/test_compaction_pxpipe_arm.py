"""Invariants of the pxpipe eval arm (evals/compaction/pxpipe_arm.py).

The arm's contract: the collapsed region is rendered by pxpipe's OWN renderer
(never a local reimplementation), the pages are charged into the retained-token
count, and a page cap can only ever shrink what is sent -- never silently claim
full coverage.
"""
from __future__ import annotations

import pytest

from evals.compaction import pxpipe_arm
from evals.compaction.fixtures import synthetic_transcript, total_tokens

pytestmark = pytest.mark.skipif(
    pxpipe_arm.require_renderer() is not None,
    reason=f"pxpipe arm not runnable here: {pxpipe_arm.require_renderer()}",
)


def test_patch_tokens_matches_anthropic_28px_grid():
    # 1568x728 -> ceil(1568/28)=56 cols, ceil(728/28)=26 rows
    assert pxpipe_arm.patch_tokens(1568, 728) == 56 * 26
    assert pxpipe_arm.patch_tokens(28, 28) == 1
    assert pxpipe_arm.patch_tokens(1, 1) == 1


def test_serialize_region_keeps_role_tags_and_tool_calls():
    msgs = [
        {"role": "user", "content": "find the bug"},
        {"role": "assistant", "content": "calling grep",
         "tool_calls": [{"id": "c1", "function": {"name": "search_files", "arguments": '{"pattern":"x"}'}}]},
        {"role": "tool", "content": "no matches", "tool_call_id": "c1"},
    ]
    text = pxpipe_arm.serialize_region(msgs)
    assert "[user] find the bug" in text
    assert "tool_call search_files" in text
    assert "[tool] no matches" in text


def test_render_pages_produces_real_pngs(tmp_path):
    text = "\n".join(f"line {i}: edited evals/compaction/runner.py, error E{i}" for i in range(200))
    out = pxpipe_arm.render_pages(text, tmp_path)
    assert out["pages"], "renderer returned no pages"
    first = out["pages"][0]
    assert first["width"] > 0 and first["height"] > 0
    assert open(first["file"], "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    assert out["charsRendered"] > 0


def test_page_cap_shrinks_coverage_and_still_charges_tokens(tmp_path):
    msgs = synthetic_transcript(120)
    before = total_tokens(msgs)
    compressed, _comp, cost = pxpipe_arm.build_arm(
        {"engine": "pxpipe", "pxpipe": {"max_pages": 1, "provider": "nous", "model": "unused"}},
        msgs, tmp_path,
    )
    assert cost["pages_rendered"] >= cost["pages_sent"] == 1
    assert cost["image_tokens"] > 0, "an image arm must never look free"
    assert cost["coverage_pct"] <= 100.0
    if cost["pages_rendered"] > 1:
        assert cost["coverage_pct"] < 100.0
    # the tail is kept verbatim and the page count is stated in the stub
    assert any("dense image page" in (m.get("content") or "") for m in compressed)
    assert total_tokens(msgs) == before
