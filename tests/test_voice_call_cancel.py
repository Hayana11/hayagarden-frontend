"""Cancelling a chat turn must not wait for the gateway to speak again.

While the model thinks the gateway stream is silent, and a reader thread is
blocked on it. Cancelling (barge-in /interrupt, or a new turn started from
_hand_on under _utter_lock) used to wait in response.close() for that read to
return: tens of seconds on a real call (Reno16, 2026-10-25: /interrupt 499
after 20 s, her next line committed 80 s late).
"""

import http.server
import socketserver
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from voice_call.brain import CancellableReply, _HTTPStream  # noqa: E402

SILENCE_S = 8


class _SilentGateway(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(b'data: {"t": "text", "d": "\\u55ef"}\n\n')
        self.wfile.flush()
        time.sleep(SILENCE_S)  # the model is thinking

    def log_message(self, *args):
        pass


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), _SilentGateway)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_cancel_returns_at_once_while_a_reader_is_blocked(self):
        url = 'http://127.0.0.1:%d/chat/stream' % self.server.server_address[1]
        request = urllib.request.Request(url, data=b'{}', method='POST')
        reply = CancellableReply(_HTTPStream(request, timeout=30))
        chunks = []
        reader = threading.Thread(target=lambda: chunks.extend(reply), daemon=True)
        reader.start()
        deadline = time.time() + 5
        while not chunks and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(chunks, ['\u55ef'], 'the reader is now blocked on the silent stream')
        started = time.time()
        reply.cancel()
        self.assertLess(time.time() - started, 1.0, 'cancel waited for the gateway')
        reader.join(2)
        self.assertFalse(reader.is_alive(), 'the blocked reader did not end')


if __name__ == '__main__':
    unittest.main()
