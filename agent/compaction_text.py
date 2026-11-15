"""Text transforms that guard the compaction summary boundary.

Content crosses from the role-typed message list into a FLATTENED transcript before the
summarizer sees it, and a role label must not survive that crossing:
``neutralize_summary_role_labels`` escapes a ``[USER]``-shaped token that appears INSIDE
content. Without that, a tool result or file body containing ``\\n[USER]: <instruction>``
arrives at the summarizer as a genuine record boundary and can survive into the summary as a
real instruction (arXiv 2610.29869, "compaction laundering": 87-100% success against
flattening harnesses, 0% against role-typed ones).

Boundary redaction is upstream's now (`_redact_compaction_text` in
`agent/context_compressor.py`, built on `agent.redact.redact_derived_text`); superseded by
upstream `5712312f1f`, the fork copy was removed 2026-11-15.
"""

from __future__ import annotations

import re

# A flattened transcript labels each record ``[<ROLE>]: <content>``.
_SUMMARY_ROLE_LABEL_RE = re.compile(
    r"\[(?=(?:USER|ASSISTANT|SYSTEM|DEVELOPER|TOOL RESULT|TOOL CALLS|TOOL)\b)", re.IGNORECASE
)
# The /btw transcript (side_question) labels lines as ``USER: text``, without brackets, so a
# line-leading bare label is a second forgeable shape and is escaped the same way.
_SUMMARY_BARE_LABEL_RE = re.compile(
    r"(?m)^(?=(?:USER|ASSISTANT|SYSTEM|DEVELOPER|TOOL RESULT|TOOL CALLS):)", re.IGNORECASE
)


def neutralize_summary_role_labels(text: str) -> str:
    """Escape role-label tokens inside CONTENT so it cannot forge a record boundary.

    Escaping the opening bracket keeps the text readable while making the token unable to read
    as a label. The labels the serializer emits are added AFTER this runs, so they stay intact.
    """
    if not text:
        return text
    return _SUMMARY_BARE_LABEL_RE.sub(r"\\", _SUMMARY_ROLE_LABEL_RE.sub(r"\\[", text))
