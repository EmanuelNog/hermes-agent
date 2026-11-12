"""Text transforms that guard the compaction summary boundary.

Content crosses from the role-typed message list into a FLATTENED transcript before the
summarizer sees it, and two things must not survive that crossing:

* credentials — ``_redact_compaction_text`` strips them in strict mode (URL credentials
  included) because summaries persist and re-enter every later prompt;
* role labels — ``neutralize_summary_role_labels`` escapes a ``[USER]``-shaped token that
  appears INSIDE content. Without that, a tool result or file body containing
  ``\\n[USER]: <instruction>`` arrives at the summarizer as a genuine record boundary and can
  survive into the summary as a real instruction (arXiv 2610.29869, "compaction laundering":
  87-100% success against flattening harnesses, 0% against role-typed ones).

They live together because they are the same boundary: text that is about to be presented as
a labelled record must not be able to speak in the transcript's own voice.
"""

from __future__ import annotations

import re
from typing import Any

from agent.redact import redact_sensitive_text

# A flattened transcript labels each record ``[<ROLE>]: <content>``.
_SUMMARY_ROLE_LABEL_RE = re.compile(
    r"\[(?=(?:USER|ASSISTANT|SYSTEM|DEVELOPER|TOOL RESULT|TOOL CALLS|TOOL)\b)", re.IGNORECASE
)
# The /btw transcript (side_question) labels lines as ``USER: text``, without brackets, so a
# line-leading bare label is a second forgeable shape and is escaped the same way.
_SUMMARY_BARE_LABEL_RE = re.compile(
    r"(?m)^(?=(?:USER|ASSISTANT|SYSTEM|DEVELOPER|TOOL RESULT|TOOL CALLS):)", re.IGNORECASE
)


def _redact_compaction_text(text: Any) -> str:
    """Redact text that crosses a compaction summary boundary (strict mode).
    ``force=True`` overrides ``security.redact_secrets: false``; URL credentials are redacted too, since
    summaries persist and re-enter every later prompt."""
    return redact_sensitive_text(text or "", force=True, redact_url_credentials=True)


def neutralize_summary_role_labels(text: str) -> str:
    """Escape role-label tokens inside CONTENT so it cannot forge a record boundary.

    Escaping the opening bracket keeps the text readable while making the token unable to read
    as a label. The labels the serializer emits are added AFTER this runs, so they stay intact.
    """
    if not text:
        return text
    return _SUMMARY_BARE_LABEL_RE.sub(r"\\", _SUMMARY_ROLE_LABEL_RE.sub(r"\\[", text))
