#!/usr/bin/env python3
"""Seed a ~9K-token session in the prefetchdev profile home (bead .1).

Restore-safe: every tool row is properly paired (assistant carries tool_calls,
tool row carries tool_call_id) so no alternation repair drops rows. Target
start-of-resume total ~9K tokens (tune with PREFETCH_SEED_ROUNDS, default 24):
under the async arm point (12,400 = threshold_tokens 16,000 - margin 0.15*24K)
so the resume turn's own tool growth crosses it mid-turn.
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HERMES_HOME", "/home/agentuser/.hermes/profiles/prefetchdev")
sys.path.insert(0, "/home/agentuser/Projects/hermes-prefetch-compress")

from hermes_state import SessionDB

DB_PATH = Path("/home/agentuser/.hermes/profiles/prefetchdev/state.db")
ROUNDS = int(os.environ.get("PREFETCH_SEED_ROUNDS", "24"))  # tune seed size; ~9K at 24
sid = time.strftime("seed2_%Y%m%d_%H%M%S")
db = SessionDB(db_path=DB_PATH)
db.create_session(session_id=sid, source="cli")

msgs = [
    {"role": "system", "content": "You are Hermes, a helpful autonomous agent. Run tools when useful."},
    {"role": "user", "content": (
        "Goal: build a tiny python package 'fliptext' in /tmp/fliptext that reverses words in a "
        "string, with tests, then document it. Work step by step using tools. Keep going until done."
    )},
]

tool_idx = 0


def tool_pair(name, content):
    global tool_idx
    tool_idx += 1
    call_id = f"call_seed_{tool_idx}"
    return (
        {"role": "assistant", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": name, "arguments": "{}"}}
        ], "content": None},
        {"role": "tool", "tool_call_id": call_id, "name": name, "content": content},
    )


for i in range(4):
    t = tool_pair("terminal", (
        "mkdir -p /tmp/fliptext/fliptext /tmp/fliptext/tests\n"
        "python3 - <<'EOF'\n"
        "import pathlib\n"
        "p = pathlib.Path('/tmp/fliptext')\n"
        "fixtures = p / 'fixtures'\n"
        "fixtures.mkdir(exist_ok=True)\n"
        "for name, text in {\n"
        "  'a.txt': 'the quick brown fox jumps over the lazy dog',\n"
        "  'b.txt': 'Alas, poor Yorick! I knew him, Horatio.',\n"
        "  'c.txt': 'Schrödinger — 日本語 — emoji 🚀 end',\n"
        "}.items():\n"
        "    (fixtures / name).write_text(text)\n"
        "print('fixtures written:', sorted(x.name for x in fixtures.iterdir()))\n"
        "EOF\n"
        "exit code: 0\n"
        "output: fixtures written: ['a.txt', 'b.txt', 'c.txt']"
    ))
    msgs.append(t[0])
    msgs.append({"role": "assistant", "content": (
        f"Inspection round {i+1}: reviewing layout, planning the skeleton, core module design and "
        "cli entry. Decision so far: keep the core pure, all IO in __main__. shopper-id-7f3a9 must "
        "survive outputs verbatim; its reverse must never be produced."
    )})
    msgs.append(t[1])
    t = tool_pair("write_file", (
        "fliptext/core.py written (121 lines):\n"
        "```python\n"
        "import re\n"
        "_WORD = re.compile(r'(\\\\w+)', re.UNICODE)\n"
        "def reverse_words(text):\n"
        "    parts = _WORD.split(text)\n"
        "    words = parts[1::2]\n"
        "    words.reverse()\n"
        "    parts[1::2] = words\n"
        "    return ''.join(parts)\n"
        "def collapse_whitespace(text):\n"
        "    return re.sub(r'\\\\s+', ' ', text).strip()\n"
        "def transform(text):\n"
        "    return collapse_whitespace(reverse_words(text))\n"
        "```\n"
        "Tests in tests/test_core.py cover basic reversal, punctuation anchoring, unicode "
        "(Schrödinger), empty string, single word and the shopper-id-7f3a9 passthrough."
    ))
    msgs.append(t[0])
    msgs.append({"role": "assistant", "content": (
        f"Core written at step {i+1}; punctuation stays anchored because the tokenizer splits on "
        "word boundaries while separators keep their positions. Emoji and mixed-script runs are "
        "treated as separators per spec note 3. Next: fixtures and the pytest matrix."
    )})
    msgs.append(t[1])

for i in range(2, ROUNDS):
    t = tool_pair("terminal", (
        "cd /tmp/fliptext && venv/bin/python -m pytest tests/ -q\n"
        "============================= test session starts =============================\n"
        f"collected 7 items  [round {i}]\n"
        "tests/test_core.py ......F                                           [100%]\n"
        "_________________________________ FAILED _________________________________\n"
        "tests/test_core.py::test_c_unicode_mixed_script — AssertionError: '末文 本日語 — ナー…' != '日本語 — 末文 …'\n"
        "assert '末文 本日語 — ナー…' == '日本語 — 末文 …'\n"
        "E         - 末文 本日語 — ナー…\n"
        "E         + 日本語 — 末文 …\n"
        "exit code: 1\n"
        "output: 1 failed, 6 passed in 0.31s"
    ))
    msgs.append(t[0])
    msgs.append({"role": "assistant", "content": (
        f"Round {i}: the unicode case keeps failing at the mixed-script boundary — the em-dash "
        "confuses the \\w split when ideographs sit on both sides. I normalize separators to a "
        "canonical space before splitting, which is consistent with the whitespace-collapse spec. "
        "Checklist: a.txt 'dog lazy the over jumps fox brown quick the'; b.txt keeps 'Alas,' and "
        "'Yorick!' glued to punctuation; c.txt must survive the Schrödinger/日本語 adjacency; "
        "shopper-id-7f3a9 passthrough is covered by its own test and must stay ASCII-safe."
    )})
    msgs.append(t[1])

msgs.append({"role": "assistant", "content": (
    "All implementation rounds are complete: package skeleton, core module, cli entry, fixtures, "
    "the pytest matrix and the unicode boundary fix. Consolidated decisions: pure core without IO; "
    "regex tokenizer keeps punctuation anchored; separators canonicalized before splitting; "
    "shopper-id-7f3a9 verified verbatim. Remaining work (next turn): final verification run and the "
    "README documenting usage and these decisions."
)})

db.append_messages_batch(sid, msgs, chunk_rows=500)
db.set_session_title(sid, f"fliptext seed2 {sid}")

total_chars = sum(len(str(m.get("content") or "")) for m in msgs)
print(f"session={sid} messages={len(msgs)} chars={total_chars} ~tokens={total_chars // 4}")