"""
draft_check.py
--------------
Signs that a beat-book draft is damaged, whatever model wrote it.

Reasoning models that ignore "think": false (GLM-5.3 on Ollama) produced
drafts that were saved as "ready": no title, three times the word target,
every section twice after continuations restarted the book, and lines of the
model's own deliberation ("Word count check on my draft."). Nothing flagged
them. This check does: it never blocks a book, it labels it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# Far past the requested length: continuations that restarted the book.
LENGTH_FACTOR = 2.5
LENGTH_SLACK_WORDS = 1500
# A few stray phrases are normal prose; this many lines of them are not.
MIN_REASONING_LINES = 3

_REASONING_RE = re.compile(
    r"^\s*(?:let me\b|i should\b|i need to\b|i'll\b|i will now\b|hmm\b|wait,|actually,? let me|"
    r"now,? (?:let me|word count)|word count\b|that's the full document|i must not\b)"
    r"|\b(?:the user (?:wants|asked|says)|the system prompt|word budget|word count check|"
    r"resume mid-sentence|continuation should)\b",
    re.I,
)


def _headings(markdown: str, level: int) -> List[str]:
    prefix = "#" * level + " "
    return [line.strip()[len(prefix):].strip() for line in markdown.split("\n")
            if line.strip().startswith(prefix) and not line.strip().startswith(prefix + "#")]


def check_draft(markdown: str, target_words: int) -> Dict[str, Any]:
    """{"ok": bool, "problems": [plain-language reasons], "details": {...}}."""
    text = markdown or ""
    words = len(text.split())
    problems: List[str] = []
    details: Dict[str, Any] = {"words": words, "target_words": target_words}

    if not _headings(text, 1):
        problems.append("It has no title, so text before the book may not have been removed.")

    limit = max(int(target_words * LENGTH_FACTOR), target_words + LENGTH_SLACK_WORDS)
    if target_words and words > limit:
        problems.append(f"It is {words:,} words against a target of about {target_words:,}, "
                        "which usually means the model rewrote the book when asked to continue it.")

    sections = [h.lower() for h in _headings(text, 2)]
    repeated = sorted({h for h in sections if sections.count(h) > 1})
    details["repeated_sections"] = repeated
    if repeated:
        problems.append("These sections appear more than once: "
                        + ", ".join(h.title() for h in repeated) + ".")

    reasoning = [line.strip()[:160] for line in text.split("\n") if _REASONING_RE.search(line)]
    details["reasoning_lines"] = reasoning[:10]
    if len(reasoning) >= MIN_REASONING_LINES:
        problems.append(f"{len(reasoning)} lines read like the model's own reasoning, not the book "
                        f"(for example: “{reasoning[0][:90]}”).")

    return {"ok": not problems, "problems": problems, "details": details}
