"""Bilingual call replies: a spoken line and its gloss, written as marked pairs.

In a call with subtitles (see voice_call.languages) he writes every sentence as

    EN: [softly] Take your time.
    ZH: 慢慢来。

Only the spoken line (EN, JA, RU…) is voiced; the gloss (ZH) is only shown and
never reaches the voice. The two are never mixed in one string for the player
to untangle.

The reply streams in arbitrary pieces, so a cut can land anywhere, even inside
a marker. The parser keeps an unfinished line until its newline arrives and
publishes a pair only once both of its lines are complete; nothing published
is ever revised. Audio tags stay on the spoken line (the voice filters them)
and are stripped from the gloss.

He is asked for this format, not trusted with it. Departures degrade instead
of silencing the call: an unmarked line is spoken as it is, a spoken line
whose gloss never comes is published without one, and a gloss with no spoken
line before it is dropped (Chinese must not reach a voice of another language).
"""
import re

from voice_call.tags import strip_tags


class PairParser:
    def __init__(self, marker, gloss_marker):
        self._line = re.compile(
            r'^\s*(?:\*\*)?(%s|%s)(?:\*\*)?\s*[:：]\s*(.*)$' % (re.escape(marker), re.escape(gloss_marker)),
            re.IGNORECASE,
        )
        self._marker = marker.upper()
        self._buf = ''
        self._spoken = None  # spoken line waiting for its gloss

    def feed(self, delta):
        """Pairs completed by this piece of the reply, as (spoken, gloss or None)."""
        self._buf += delta or ''
        out = []
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            out.extend(self._take(line))
        return out

    def flush(self):
        """End of the reply: the last line (no newline needed) and any spoken line still waiting."""
        rest, self._buf = self._buf, ''
        out = self._take(rest)
        if self._spoken is not None:
            out.append((self._spoken, None))
            self._spoken = None
        return out

    def _take(self, raw):
        line = raw.strip()
        if not line:
            return []
        match = self._line.match(line)
        if match is None:
            # unmarked: spoken as it is; a spoken line still waiting goes out first, without gloss
            return self._release() + [(line, None)]
        mark, value = match.group(1).upper(), match.group(2).lstrip('*').strip()
        if mark == self._marker:
            out = self._release()
            if value:
                self._spoken = value
            return out
        if self._spoken is None:
            return []  # a gloss with nothing to gloss
        gloss = strip_tags(value) or None
        out = [(self._spoken, gloss)]
        self._spoken = None
        return out

    def _release(self):
        if self._spoken is None:
            return []
        out = [(self._spoken, None)]
        self._spoken = None
        return out
