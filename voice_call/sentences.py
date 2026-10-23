"""Cut a streamed reply into lines that can be synthesized one at a time.

The phone-style prompt asks for one sentence per line, so newlines are the
primary boundary. A line that runs long without a newline is cut after the
last sentence-ending mark, so the first audio does not wait on a paragraph.
The very first line of a reply is cut sooner, at its first pause once it runs
past FIRST_LIMIT, because it alone decides how long she waits for his voice.
"""

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

    def feed(self, chunk):
        self._buf += chunk or ''
        out = []
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            out.extend(_clean([line]))
        if not out and not self._started and len(self._buf.strip()) > self._first_limit:
            cut = next((i for i, ch in enumerate(self._buf) if ch in _PAUSES and i >= FIRST_MIN), -1)
            if cut > 0:
                out.extend(_clean([self._buf[:cut + 1]]))
                self._buf = self._buf[cut + 1:]
        self._started = self._started or bool(out)
        while len(self._buf) > self._soft_limit:
            cut = max(self._buf.rfind(mark, 0, self._soft_limit + 1) for mark in _ENDINGS)
            if cut <= 0:
                break
            out.extend(_clean([self._buf[:cut + 1]]))
            self._buf = self._buf[cut + 1:]
        return out

    def flush(self):
        rest, self._buf = self._buf, ''
        self._started = False
        return _clean([rest])


def _clean(lines):
    return [line.strip() for line in lines if line.strip()]
