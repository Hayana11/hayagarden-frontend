"""Runtime contract for legacy [choices] selectors."""
from __future__ import annotations

import re


MAX_CHOICES = 8
MAX_CHOICE_LENGTH = 120
_CHOICES_RE = re.compile(r'\[choices\](.*?)\[/choices\]', re.DOTALL)


def extract_choices(text: str):
    """Inspect only the first selector; extract it if valid and preserve later blocks."""
    if not text or '[choices]' not in text:
        return text, []
    match = _CHOICES_RE.search(text)
    if not match:
        return text, []
    options = [
        option
        for option in (raw.strip() for raw in match.group(1).split('|'))
        if option and len(option) <= MAX_CHOICE_LENGTH
    ][:MAX_CHOICES]
    if not options:
        return text, []
    clean = (text[:match.start()] + text[match.end():]).strip()
    return clean, options
