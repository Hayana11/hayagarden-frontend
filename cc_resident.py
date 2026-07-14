"""Persistent (resident) Claude Code subprocess for Fyodor's solo chat.

The old approach (see gateway.py git history: _cc_prepare/_cc_stream_gen)
spawned a fresh `claude -p <full flattened day's conversation>` process on
every single turn. That meant the entire day's history got re-sent and
re-billed as fresh input tokens every turn — verified live (two consecutive
calls, same system prompt, only the "conversation so far" text grew: second
call showed cache_creation again, cache_read=0 — zero reuse).

This module keeps ONE `claude` process alive for the life of the gateway
worker, fed via `--input-format stream-json` over stdin (the officially
documented "streaming input mode" / preferred way for resident use). Only
the *new* turn is sent each time; the process's own in-memory context is the
conversation history, so nothing needs to be re-flattened and resent. The
static system prompt (persona) is passed once at spawn via --system-prompt
and stays untouched for the process's lifetime, so it's cacheable normally.

Respawn happens when: the process died, the model changed, or the static
system prompt text changed (e.g. persona.md was edited). On a fresh spawn
with no prior in-process history, the first turn carries a one-time recap
of today's conversation (same content the old code always resent) so
continuity isn't lost across a deploy-triggered restart — every turn after
that first one is cheap.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

NL = chr(10)
CC_STREAM_TIMEOUT = 360  # seconds; kills a hung turn (and the whole resident process with it)
IDLE_REAP_SECONDS = 3 * 60 * 60  # respawn instead of keeping a multi-hour-idle process around


class ResidentError(RuntimeError):
    pass


class ResidentSession:
    """One persistent `claude` subprocess. Not safe for concurrent turns —
    caller must serialize (gateway.py already does via _gen_acquire_or_wait)."""

    def __init__(self, cwd, allowed_tools, mcp_config_path):
        self._cwd = cwd
        self._allowed_tools = allowed_tools
        self._mcp_config_path = mcp_config_path
        self._proc = None
        self._system_text = None
        self._session_id = None
        self._cold = True
        self._last_used = 0.0
        self._lock = threading.Lock()

    def _spawn(self, system_text, env):
        # No --model flag: matches the one-shot code this replaces, which never
        # passed one either (CLI just uses the logged-in account's own default).
        self._kill(quiet=True)
        args = [
            'claude', '-p',
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--verbose',
            '--include-partial-messages',
            '--system-prompt', system_text,
            '--max-turns', '5',
            '--tools', '',
            '--thinking-display', 'summarized',
            '--mcp-config', self._mcp_config_path,
            '--strict-mcp-config',
            '--allowedTools', self._allowed_tools,
            '--exclude-dynamic-system-prompt-sections',
        ]
        self._proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=self._cwd, env=env,
        )
        self._system_text = system_text
        self._session_id = None
        self._cold = True

    def _kill(self, quiet=False):
        proc, self._proc = self._proc, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=8)
            except Exception:
                pass
        if not quiet:
            try:
                proc.stdout.close()
            except Exception:
                pass
        try:
            proc.stderr.close()
        except Exception:
            pass

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def ensure_alive(self, system_text, env):
        with self._lock:
            idle_too_long = self._alive() and (time.time() - self._last_used) > IDLE_REAP_SECONDS
            needs_respawn = (
                not self._alive()
                or idle_too_long
                or system_text != self._system_text
            )
            if needs_respawn:
                self._spawn(system_text, env)
            return self._cold

    def send_turn(self, content):
        """content: str or list of Anthropic content blocks for the new user turn.
        Yields the same event tuples _cc_stream_gen used to:
        ('text', str) / ('think', str) / ('tool_use', dict) / ('tool_result', dict)
        / ('done', (text, thinking, cache_read_total, cache_create_total))."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise ResidentError('resident 进程不存在，需要先 ensure_alive')

        payload = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}, ensure_ascii=False)
        try:
            proc.stdin.write(payload + NL)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._kill(quiet=True)
            raise ResidentError('resident 进程管道已断: ' + str(e))

        timed_out = [False]

        def _kill_on_timeout():
            timed_out[0] = True
            self._kill(quiet=True)

        timer = threading.Timer(CC_STREAM_TIMEOUT, _kill_on_timeout)
        timer.daemon = True
        timer.start()

        think_acc, text_acc = [], []
        cache_read_total, cache_create_total = 0, 0
        is_err = None
        saw_result = False
        try:
            while True:
                raw_line = proc.stdout.readline()
                if raw_line == '':
                    break  # EOF: process died mid-turn
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get('type')
                if t == 'system' and d.get('subtype') == 'init':
                    self._session_id = d.get('session_id') or self._session_id
                elif t == 'stream_event':
                    ev = d.get('event') or {}
                    ev_type = ev.get('type')
                    if ev_type == 'content_block_delta':
                        delta = ev.get('delta') or {}
                        if delta.get('type') == 'text_delta':
                            chunk = delta.get('text', '')
                            if chunk:
                                text_acc.append(chunk)
                                yield ('text', chunk)
                        elif delta.get('type') == 'thinking_delta':
                            chunk = delta.get('thinking', '')
                            if chunk:
                                think_acc.append(chunk)
                                yield ('think', chunk)
                elif t == 'assistant':
                    for b in ((d.get('message') or {}).get('content') or []):
                        if isinstance(b, dict) and b.get('type') == 'tool_use':
                            yield ('tool_use', {'id': b.get('id'), 'name': b.get('name', ''), 'args': b.get('input') or {}})
                elif t == 'user':
                    for b in ((d.get('message') or {}).get('content') or []):
                        if isinstance(b, dict) and b.get('type') == 'tool_result':
                            rc = b.get('content')
                            if isinstance(rc, list):
                                rc = ''.join(x.get('text', '') for x in rc if isinstance(x, dict))
                            yield ('tool_result', {'tool_use_id': b.get('tool_use_id'), 'result': str(rc or ''), 'is_error': bool(b.get('is_error'))})
                elif t == 'result':
                    saw_result = True
                    if d.get('is_error'):
                        is_err = str(d.get('result', ''))[:300]
                    u = d.get('usage') or {}
                    if u:
                        cache_read_total = u.get('cache_read_input_tokens', cache_read_total) or cache_read_total
                        cache_create_total = u.get('cache_creation_input_tokens', cache_create_total) or cache_create_total
                    break  # end of THIS turn only — process/pipe stays open for the next one
        finally:
            timer.cancel()

        if timed_out[0]:
            raise ResidentError('claude code 调用超时 (%ds)，resident 进程已重启' % CC_STREAM_TIMEOUT)
        if not saw_result:
            self._kill(quiet=True)
            raise ResidentError('resident 进程在本轮回复完成前退出')
        if is_err:
            raise ResidentError('claude code 返回错误: ' + is_err)

        self._cold = False
        self._last_used = time.time()
        yield ('done', (''.join(text_acc).strip(), ''.join(think_acc), cache_read_total, cache_create_total))

    def is_cold(self):
        return self._cold

    def shutdown(self):
        with self._lock:
            self._kill()
