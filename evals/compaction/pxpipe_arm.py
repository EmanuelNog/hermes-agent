"""pxpipe as a compaction eval arm.

https://github.com/teamchong/pxpipe (MIT) is a local proxy that rewrites bulky
request context into dense PNG pages (5x8 Spleen glyph cells) so the model reads
it through the vision channel: an image costs tokens by pixel area, not by how
much text it carries, so dense text buys ~3-5x fewer input tokens per character.

Our study asks a different question than pxpipe's proxy does: what does that cost
in RECALL, against the same transcripts, question bank and judge the summary
arms are scored on? So this arm keeps the structure of the production policy --
same head, same protected tail, verbatim -- and replaces ONLY the middle region
that compaction would summarize with pxpipe-rendered pages of that region. That
isolates one variable: summary of the collapsed region vs pixels of it.

Rendering is not reimplemented here. `pxpipe_render.mjs` imports pxpipe's own
`renderDensePages` from a checkout of the upstream repo (PXPPIPE_DIR, default
~/Projects/spikes/pxpipe), run through `tsx`, so pages are byte-identical to what
the proxy produces. Nothing is vendored: this repo holds no third-party code.

Image token accounting uses pxpipe's own Anthropic metric (28-px patch count:
ceil(w/28)*ceil(h/28) per page), which is what the proxy reports as saved input.
"""
from __future__ import annotations

import base64
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_SCRIPT = Path(__file__).resolve().parent / "pxpipe_render.mjs"
ANTHROPIC_PATCH_PX = 28
# pxpipe's shipping default reader for dense pages (its own best-scoring model).
DEFAULT_ARM_MODEL = ("nous", "google/gemini-4-argon")

# Vision-call usage for the arm's answers, merged into the run's eval_usage.json.
USAGE: dict[str, Any] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}


def patch_tokens(width: int, height: int) -> int:
    return math.ceil(max(1, width) / ANTHROPIC_PATCH_PX) * math.ceil(max(1, height) / ANTHROPIC_PATCH_PX)


@dataclass
class PxpipeOptions:
    """Knobs for the arm. `max_pages` bounds request size, not the finding:
    pages past the cap are NOT sent, and the result records the coverage that
    lost so the reader can see what the arm was actually asked to read."""
    cols: Optional[int] = None
    max_pages: int = 24
    fmt: str = "dense"          # 'dense' = pxpipe's production page style
    provider: str = ""
    model: str = ""
    usage: dict[str, Any] = field(default_factory=lambda: {"calls": 0, "prompt_tokens": 0,
                                                           "completion_tokens": 0})


def serialize_region(messages: list[dict[str, Any]]) -> str:
    """Text handed to the renderer: every row of the collapsed region, role-tagged,
    tool calls and results included. Deliberately NOT summarized or abridged --
    the arm's claim is that nothing is lost but pixels."""
    parts = []
    for m in messages:
        role = m.get("role") or "?"
        body = m.get("content")
        if isinstance(body, list):
            body = json.dumps(body, default=str)
        body = body or ""
        tc = m.get("tool_calls")
        if tc:
            body += "\n" + "\n".join(
                f"tool_call {c.get('function', {}).get('name', '?')}({c.get('function', {}).get('arguments', '')})"
                for c in tc
            )
        parts.append(f"[{role}] {body}")
    return "\n\n".join(parts)


def render_pages(text: str, out_dir: Path, cols: Optional[int] = None) -> dict[str, Any]:
    """Render `text` with pxpipe's own renderer. Returns page geometry + dropped glyph stats."""
    out_dir.mkdir(parents=True, exist_ok=True)
    src = out_dir / "region.txt"
    src.write_text(text, encoding="utf-8")
    cmd = ["npx", "--yes", "tsx", str(RENDER_SCRIPT), str(src), str(out_dir / "pages")]
    if cols:
        cmd.append(str(cols))
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=900,
                          check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"pxpipe render failed ({proc.returncode}): {proc.stderr[-800:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def build_arm(spec: dict, messages: list[dict[str, Any]], work_dir: Path,
             model: str = "anthropic/claude-fable-5") -> tuple[list[dict[str, Any]], Any, dict]:
    """Return (compressed messages, stub compressor, compaction-cost dict with `media`)."""
    from agent.context_compressor import ContextCompressor

    opt = PxpipeOptions(**(spec.get("pxpipe") or {}))
    if not opt.provider or not opt.model:
        opt.provider, opt.model = DEFAULT_ARM_MODEL
    opt.provider = os.environ.get("PXPIPE_ARM_PROVIDER", opt.provider)
    opt.model = os.environ.get("PXPIPE_ARM_MODEL", opt.model)

    # Same structural cut as the shipping policy: protected head, protected tail,
    # the middle is what compaction collapses.
    comp = ContextCompressor(model=model, quiet_mode=True)
    head_end = comp.protect_first_n
    tail_start = comp._find_tail_cut_by_tokens(messages, head_end)
    region = messages[head_end:tail_start]
    region_text = serialize_region(region)

    rendered = render_pages(region_text, work_dir, cols=opt.cols)
    pages = rendered["pages"]
    sent = pages[: opt.max_pages]
    image_tokens = sum(patch_tokens(p["width"], p["height"]) for p in sent)
    rendered_chars = sum(p["chars"] for p in sent)

    stub = (
        f"[Compacted context: the earlier part of this session was converted to {len(sent)} dense "
        f"image page(s), attached below in chronological order. Read them as your own prior "
        f"conversation. {rendered_chars:,} characters were rendered"
        + (f"; {len(pages) - len(sent)} further page(s) are omitted." if len(pages) > len(sent) else ".")
        + "]"
    )
    import copy as _copy
    compressed = _copy.deepcopy(messages[:head_end]) + [{"role": "user", "content": stub}] \
        + _copy.deepcopy(messages[tail_start:])

    cost = {
        "compaction_calls": 0,
        "compaction_cost_usd": 0.0,
        "compaction_model": f"pxpipe render ({opt.fmt})",
        "media": [p["file"] for p in sent],
        "image_tokens": image_tokens,
        "pages_rendered": len(pages),
        "pages_sent": len(sent),
        "rendered_chars": rendered_chars,
        "region_chars": len(region_text),
        "region_rows": len(region),
        "coverage_pct": round(min(100.0, 100 * rendered_chars / max(1, len(region_text))), 1),
        "dropped_glyphs": rendered.get("dropped", 0),
        "arm_reader": f"{opt.provider}/{opt.model}",
    }
    # `after_tokens` must charge the pages, or the arm looks free.
    cost["_image_tokens"] = image_tokens
    return compressed, comp, cost


ANSWER_NOTE = (
    "Your earlier conversation has been replaced by dense image pages of its raw text, attached "
    "below in chronological order (oldest first). Read the pages as your own prior work: role tags "
    "mark [user]/[assistant]/[tool] rows. "
)


def image_content(paths: list[str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for p in paths:
        data = base64.b64encode(Path(p).read_bytes()).decode()
        blocks.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}})
    return blocks


def call_vision(prompt: str, paths: list[str], provider: str, model: str,
                usage_sink: dict[str, Any] | None = None, max_tokens: int = 500) -> str:
    from agent.auxiliary_client import call_llm

    msgs = [{"role": "user", "content": [{"type": "text", "text": prompt}, *image_content(paths)]}]
    resp = call_llm(task="compression", provider=provider, model=model, messages=msgs,
                    max_tokens=max_tokens, timeout=600)
    u = getattr(resp, "usage", None)
    if u is not None and usage_sink is not None:
        usage_sink["calls"] += 1
        usage_sink["prompt_tokens"] += int(getattr(u, "prompt_tokens", 0) or 0)
        usage_sink["completion_tokens"] += int(getattr(u, "completion_tokens", 0) or 0)
    if hasattr(resp, "choices"):
        return resp.choices[0].message.content or ""
    return str(resp)


def temp_work_dir() -> Path:
    """Scratch inside the eval run's own directory tree (never the system temp dir)."""
    return Path(tempfile.mkdtemp(prefix="pxpipe-arm-"))


def cleanup(dir_path: Path) -> None:
    shutil.rmtree(dir_path, ignore_errors=True)


def require_renderer() -> Optional[str]:
    """None when the arm can run, else the reason it cannot."""
    if shutil.which("npx") is None:
        return "npx not found (needed to run pxpipe's TypeScript renderer)"
    px = Path(os.environ.get("PXPPIPE_DIR", str(Path.home() / "Projects" / "spikes" / "pxpipe")))
    if not (px / "src" / "core" / "render.ts").exists():
        return f"no pxpipe checkout at {px} (set PXPPIPE_DIR; upstream is never vendored here)"
    return None
