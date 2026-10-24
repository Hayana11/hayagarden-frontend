"""Cut a streamed reply into lines that can be synthesized one at a time.

The phone-style prompt asks for one sentence per line, so newlines are the
primary boundary. A line that runs long without a newline is cut after the
last sentence-ending mark, so the first audio does not wait on a paragraph.
The very first line of a reply is cut sooner, at its first pause once it runs
past FIRST_LIMIT, because it alone decides how long she waits for his voice.

Audio tags ("[whispers]") ride with the words they shape: lengths count words
only, no cut lands inside a tag, and a tag on a line of its own waits for the
next line instead of being synthesized alone.
"""
from voice_call.tags import is_tag_only, outside_tags, strip_tags

SOFT_LIMIT = 60
FIRST_LIMIT = 12
FIRST_MIN = 4  # never cut the first line shorter than this
_ENDINGS = '。！？!?…~～'
_PAUSES = '，,、；;：:' + _ENDINGS


class SentenceSplitter:
    def __init__(self, soft_limit=SOFT_LIMIT, first_limit=FIRST_LIMIT):
        self._buf = ''
        self._soft_limit = soft_limit
        self._first_limit = first_limit
        self._started = False  # a line has been handed out
        self._tags = ''  # tag-only lines waiting for the line they belong to

    def feed(self, chunk):
        self._buf += chunk or ''
        out = []
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            out.extend(self._take([line]))
        if not out and not self._started and len(strip_tags(self._buf)) > self._first_limit:
            cut = next((i for i, ch in enumerate(self._buf)
                        if ch in _PAUSES and len(strip_tags(self._buf[:i])) >= FIRST_MIN
                        and outside_tags(self._buf, i)), -1)
            if cut > 0:
                out.extend(self._take([self._buf[:cut + 1]]))
                self._buf = self._buf[cut + 1:]
        self._started = self._started or bool(out)
        while len(self._buf) > self._soft_limit:
            cut = max((i for i, ch in enumerate(self._buf[:self._soft_limit + 1])
                       if ch in _ENDINGS and outside_tags(self._buf, i)), default=-1)
            if cut <= 0:
                break
            out.extend(self._take([self._buf[:cut + 1]]))
            self._buf = self._buf[cut + 1:]
        return out

    def flush(self):
        rest, self._buf = self._buf, ''
        self._started = False
        out = self._take([rest])
        self._tags = ''  # a tag with no words after it is dropped
        return out

    def _take(self, lines):
        out = []
        for line in _clean(lines):
            if is_tag_only(line):
                self._tags += line + ' '
                continue
            out.append(self._tags + line)
            self._tags = ''
        return out


def _clean(lines):
    return [line.strip() for line in lines if line.strip()]
