"""Cut a streamed reply into lines that can be synthesized one at a time.

The phone-style prompt asks for one sentence per line, so newlines are the
primary boundary. A line that runs long without a newline is cut after the
last sentence-ending mark, so the first audio does not wait on a paragraph.
"""

SOFT_LIMIT = 60
_ENDINGS = '。！？!?…~～'


class SentenceSplitter:
    def __init__(self, soft_limit=SOFT_LIMIT):
        self._buf = ''
        self._soft_limit = soft_limit

    def feed(self, chunk):
        self._buf += chunk or ''
        out = []
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            out.extend(_clean([line]))
        while len(self._buf) > self._soft_limit:
            cut = max(self._buf.rfind(mark, 0, self._soft_limit + 1) for mark in _ENDINGS)
            if cut <= 0:
                break
            out.extend(_clean([self._buf[:cut + 1]]))
            self._buf = self._buf[cut + 1:]
        return out

    def flush(self):
        rest, self._buf = self._buf, ''
        return _clean([rest])


def _clean(lines):
    return [line.strip() for line in lines if line.strip()]
