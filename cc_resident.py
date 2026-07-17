"""Persistent (resident) Claude Code subprocess for Fyodor's solo chat.

Cold start sends persona/history/full state once. Hot turns only send the new
user message, recall, changed state components, and new group-chat rows.
Snapshots commit only after stdin write+flush succeeds.
"""
from __future__ import annotations

import copy
import json
import subprocess
import threading
import time

import config_store

NL = chr(10)
CC_STREAM_TIMEOUT = 360
IDLE_REAP_SECONDS = 3 * 60 * 60


def _cfg_int(key, default):
    try:
        return int(config_store.get_int(key, default))
    except Exception:
        return default


class ResidentError(RuntimeError):
    def __init__(self, message, *, usage=None):
        super().__init__(message)
        self.usage = usage or empty_usage()


def empty_usage(**overrides):
    base = {
        'v': 2,
        'provider': 'claude_code',
        'num_rounds': 0,
        'input_tokens': 0,
        'output_tokens': 0,
        'cache_read': 0,
        'cache_creation': 0,
        'last_round_context': 0,
        'max_round_context': 0,
        'resident_turn_count': 0,
        'respawn_reason': None,
        'rounds': [],
    }
    base.update(overrides)
    return base


def summarize_rounds(rounds, *, resident_turn_count=0, respawn_reason=None, max_round_context=0):
    rounds = list(rounds or [])
    usage = empty_usage(
        num_rounds=len(rounds),
        input_tokens=sum(int(r.get('input_tokens') or 0) for r in rounds),
        output_tokens=sum(int(r.get('output_tokens') or 0) for r in rounds),
        cache_read=sum(int(r.get('cache_read') or 0) for r in rounds),
        cache_creation=sum(int(r.get('cache_creation') or 0) for r in rounds),
        resident_turn_count=resident_turn_count,
        respawn_reason=respawn_reason,
        rounds=rounds,
    )
    if rounds:
        last = rounds[-1]
        usage['last_round_context'] = int(last.get('context_tokens') or 0)
        usage['max_round_context'] = max(
            int(max_round_context or 0),
            max(int(r.get('context_tokens') or 0) for r in rounds),
        )
    else:
        usage['max_round_context'] = int(max_round_context or 0)
    return usage


def normalize_cache_info(raw):
    """Accept legacy v1 and v2 cache_info without raising."""
    if not isinstance(raw, dict):
        return empty_usage()
    version = int(raw.get('v') or 1)
    if version >= 2:
        usage = empty_usage()
        for key in usage:
            if key in raw:
                usage[key] = raw[key]
        usage['v'] = 2
        usage['num_rounds'] = int(raw.get('num_rounds') or len(raw.get('rounds') or []) or 1)
        usage['rounds'] = list(raw.get('rounds') or [])
        return usage
    return empty_usage(
        v=1,
        num_rounds=1,
        cache_read=int(raw.get('cache_read') or 0),
        cache_creation=int(raw.get('cache_creation') or 0),
        input_tokens=int(raw.get('input_tokens') or 0),
        output_tokens=int(raw.get('output_tokens') or 0),
    )


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
        self._reset_session_meta(respawn_reason=None)

    def _reset_session_meta(self, *, respawn_reason):
        self._resident_turn_count = 0
        self._last_round_context = 0
        self._max_round_context = 0
        self._pending_respawn_reason = respawn_reason
        self._turns_since_respawn = 0
        self._last_state_snapshot = {}
        self._last_group_message_id = 0

    def _spawn(self, system_text, env, *, reason='process_dead'):
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
        self._reset_session_meta(respawn_reason=reason)

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

    def _decide_respawn_reason(self, system_text):
        if not self._alive():
            return 'process_dead'
        if (time.time() - self._last_used) > IDLE_REAP_SECONDS:
            return 'idle'
        if system_text != self._system_text:
            return 'system_changed'

        hard = _cfg_int('CC_CONTEXT_HARD_LIMIT', 120_000)
        soft = _cfg_int('CC_CONTEXT_SOFT_LIMIT', 90_000)
        max_turns = _cfg_int('CC_MAX_RESIDENT_TURNS', 30)
        min_between = _cfg_int('CC_MIN_TURNS_BETWEEN_RESPAWNS', 5)

        if self._last_round_context >= hard:
            return 'hard_context'
        if self._resident_turn_count >= max_turns:
            return 'turn_limit'
        if (
            self._last_round_context >= soft
            and self._turns_since_respawn >= min_between
        ):
            return 'soft_context'
        return None

    def ensure_alive(self, system_text, env):
        with self._lock:
            reason = self._decide_respawn_reason(system_text)
            if reason:
                self._spawn(system_text, env, reason=reason)
            return self._cold

    def _commit_sent_context(self, commit_meta):
        if not commit_meta:
            return
        if 'state_snapshot' in commit_meta:
            self._last_state_snapshot = copy.deepcopy(commit_meta['state_snapshot'] or {})
        if commit_meta.get('group_max_id') is not None:
            self._last_group_message_id = int(commit_meta['group_max_id'])

    def send_turn(self, content, commit_meta=None):
        """Yield ('text'/'think'/'tool_use'/'tool_result'/'done', payload).

        done payload is (text, thinking, usage_dict).
        Snapshots commit only after stdin write+flush succeeds.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise ResidentError('resident 进程不存在，需要先 ensure_alive')

        respawn_reason = self._pending_respawn_reason
        payload = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}, ensure_ascii=False)
        try:
            proc.stdin.write(payload + NL)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._kill(quiet=True)
            raise ResidentError('resident 进程管道已断: ' + str(e))

        # 信已塞进门缝：立刻记作已送达（即使后续流中断也不重复塞）
        self._commit_sent_context(commit_meta)

        timed_out = [False]

        def _kill_on_timeout():
            timed_out[0] = True
            self._kill(quiet=True)

        timer = threading.Timer(CC_STREAM_TIMEOUT, _kill_on_timeout)
        timer.daemon = True
        timer.start()

        think_acc, text_acc = [], []
        rounds = []
        current_round = None
        is_err = None
        saw_result = False
        try:
            while True:
                raw_line = proc.stdout.readline()
                if raw_line == '':
                    break
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
                    if ev_type == 'message_start':
                        if current_round is not None:
                            current_round['context_tokens'] = (
                                int(current_round.get('input_tokens') or 0)
                                + int(current_round.get('cache_read') or 0)
                                + int(current_round.get('cache_creation') or 0)
                            )
                            current_round['complete'] = True
                            rounds.append(current_round)
                        current_round = {
                            'index': len(rounds) + 1,
                            'complete': False,
                            'input_tokens': 0,
                            'output_tokens': 0,
                            'cache_read': 0,
                            'cache_creation': 0,
                            'context_tokens': 0,
                        }
                        u = (ev.get('message') or {}).get('usage') or {}
                        if u:
                            current_round['input_tokens'] = max(
                                current_round['input_tokens'], int(u.get('input_tokens') or 0)
                            )
                            current_round['output_tokens'] = max(
                                current_round['output_tokens'], int(u.get('output_tokens') or 0)
                            )
                            current_round['cache_read'] = max(
                                current_round['cache_read'], int(u.get('cache_read_input_tokens') or 0)
                            )
                            current_round['cache_creation'] = max(
                                current_round['cache_creation'], int(u.get('cache_creation_input_tokens') or 0)
                            )
                    elif ev_type == 'message_delta':
                        u = ev.get('usage') or {}
                        if current_round is not None and u:
                            current_round['input_tokens'] = max(
                                current_round['input_tokens'], int(u.get('input_tokens') or 0)
                            )
                            current_round['output_tokens'] = max(
                                current_round['output_tokens'], int(u.get('output_tokens') or 0)
                            )
                            current_round['cache_read'] = max(
                                current_round['cache_read'], int(u.get('cache_read_input_tokens') or 0)
                            )
                            current_round['cache_creation'] = max(
                                current_round['cache_creation'], int(u.get('cache_creation_input_tokens') or 0)
                            )
                    elif ev_type == 'content_block_delta':
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
                    msg = d.get('message') or {}
                    u = msg.get('usage') or {}
                    if current_round is None:
                        current_round = {
                            'index': len(rounds) + 1,
                            'complete': False,
                            'input_tokens': 0,
                            'output_tokens': 0,
                            'cache_read': 0,
                            'cache_creation': 0,
                            'context_tokens': 0,
                        }
                    if u:
                        current_round['input_tokens'] = max(
                            current_round['input_tokens'], int(u.get('input_tokens') or 0)
                        )
                        current_round['output_tokens'] = max(
                            current_round['output_tokens'], int(u.get('output_tokens') or 0)
                        )
                        current_round['cache_read'] = max(
                            current_round['cache_read'], int(u.get('cache_read_input_tokens') or 0)
                        )
                        current_round['cache_creation'] = max(
                            current_round['cache_creation'], int(u.get('cache_creation_input_tokens') or 0)
                        )
                    for b in (msg.get('content') or []):
                        if isinstance(b, dict) and b.get('type') == 'tool_use':
                            if current_round is not None and not current_round.get('complete'):
                                current_round['context_tokens'] = (
                                    int(current_round.get('input_tokens') or 0)
                                    + int(current_round.get('cache_read') or 0)
                                    + int(current_round.get('cache_creation') or 0)
                                )
                                current_round['complete'] = True
                                rounds.append(current_round)
                                current_round = None
                            yield ('tool_use', {
                                'id': b.get('id'),
                                'name': b.get('name', ''),
                                'args': b.get('input') or {},
                            })
                elif t == 'user':
                    for b in ((d.get('message') or {}).get('content') or []):
                        if isinstance(b, dict) and b.get('type') == 'tool_result':
                            rc = b.get('content')
                            if isinstance(rc, list):
                                rc = ''.join(x.get('text', '') for x in rc if isinstance(x, dict))
                            yield ('tool_result', {
                                'tool_use_id': b.get('tool_use_id'),
                                'result': str(rc or ''),
                                'is_error': bool(b.get('is_error')),
                            })
                elif t == 'result':
                    saw_result = True
                    if d.get('is_error'):
                        is_err = str(d.get('result', ''))[:300]
                    # result.usage 只做校验/fallback，不覆盖已解析的 rounds
                    if current_round is not None:
                        current_round['context_tokens'] = (
                            int(current_round.get('input_tokens') or 0)
                            + int(current_round.get('cache_read') or 0)
                            + int(current_round.get('cache_creation') or 0)
                        )
                        current_round['complete'] = not bool(d.get('is_error'))
                        rounds.append(current_round)
                        current_round = None
                    break
        finally:
            timer.cancel()

        if current_round is not None:
            current_round['context_tokens'] = (
                int(current_round.get('input_tokens') or 0)
                + int(current_round.get('cache_read') or 0)
                + int(current_round.get('cache_creation') or 0)
            )
            current_round['complete'] = False
            rounds.append(current_round)

        usage = summarize_rounds(
            rounds,
            resident_turn_count=self._resident_turn_count + 1,
            respawn_reason=respawn_reason,
            max_round_context=self._max_round_context,
        )

        if timed_out[0]:
            raise ResidentError(
                'claude code 调用超时 (%ds)，resident 进程已重启' % CC_STREAM_TIMEOUT,
                usage=usage,
            )
        if not saw_result:
            self._kill(quiet=True)
            raise ResidentError('resident 进程在本轮回复完成前退出', usage=usage)
        if is_err:
            raise ResidentError('claude code 返回错误: ' + is_err, usage=usage)

        self._cold = False
        self._last_used = time.time()
        self._resident_turn_count += 1
        self._turns_since_respawn += 1
        self._pending_respawn_reason = None
        self._last_round_context = int(usage.get('last_round_context') or 0)
        self._max_round_context = max(self._max_round_context, int(usage.get('max_round_context') or 0))
        usage['resident_turn_count'] = self._resident_turn_count
        usage['max_round_context'] = self._max_round_context
        yield ('done', (''.join(text_acc).strip(), ''.join(think_acc), usage))

    def is_cold(self):
        return self._cold

    @property
    def last_state_snapshot(self):
        return self._last_state_snapshot

    @property
    def last_group_message_id(self):
        return self._last_group_message_id

    @property
    def pending_respawn_reason(self):
        return self._pending_respawn_reason

    def shutdown(self):
        with self._lock:
            self._kill()
