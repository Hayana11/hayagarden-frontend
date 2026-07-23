"""Persistent (resident) Claude Code subprocess for Fyodor's solo chat.

Cold start sends persona/history/full state once. Hot turns only send the new
user message, recall, changed state components, and new group-chat rows.
State/group cursors commit after stdin write+flush. Feedback/dream claims are
returned on done and consumed only after assistant DB persistence.
Client disconnect (GeneratorExit) kills the resident to avoid stdout pollution.
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
        'cache_creation_5m': 0,
        'cache_creation_1h': 0,
        'request_ids': [],
        'request_count': 0,
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
        cache_creation_5m=sum(int(r.get('cache_creation_5m') or 0) for r in rounds),
        cache_creation_1h=sum(int(r.get('cache_creation_1h') or 0) for r in rounds),
        request_ids=[r.get('request_id') for r in rounds if r.get('request_id')],
        request_count=len([r for r in rounds if r.get('request_id')]),
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
        # 阶段 1A：成功回复可选观测字段（缺省保持缺失，不写成 0/false）
        for key in ('observation_version', 'context_breakdown', 'runtime', 'jsonl_usage'):
            if key in raw:
                usage[key] = raw[key]
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
        self._generation = 0
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
        self._group_cursor_initialized = False
        self._last_rel_fingerprint = None
        self._turns_since_rel_sent = 0
        self._last_rel_mood = None
        self._keepwarm_lease_expires_at = None
        self._tool_surface_snapshot = {}

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
        self._generation += 1
        self._reset_session_meta(respawn_reason=reason)
        try:
            from tools.cc_tool_surface import capture_tool_surface_snapshot
            self._tool_surface_snapshot = capture_tool_surface_snapshot(
                self._allowed_tools,
                mcp_config_path=self._mcp_config_path,
            )
        except Exception:
            self._tool_surface_snapshot = {}

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
        """flush 后只提交仍存活 resident 内的游标；one-shot 不在这里消费。"""
        if not commit_meta:
            return
        if commit_meta.get('rel_fingerprint'):
            self._last_rel_fingerprint = commit_meta['rel_fingerprint']
            self._turns_since_rel_sent = 0
            if 'rel_mood' in commit_meta:
                self._last_rel_mood = commit_meta.get('rel_mood')
        elif commit_meta.get('rel_tick'):
            # Only successful user turns that skipped relationship advance the cadence.
            self._turns_since_rel_sent += 1
        if 'state_snapshot' in commit_meta:
            self._last_state_snapshot = copy.deepcopy(commit_meta['state_snapshot'] or {})
        if commit_meta.get('group_cursor_initialized'):
            self._group_cursor_initialized = True
            if commit_meta.get('group_max_id') is not None:
                self._last_group_message_id = int(commit_meta['group_max_id'])
        elif (
            self._group_cursor_initialized
            and commit_meta.get('group_max_id') is not None
        ):
            self._last_group_message_id = int(commit_meta['group_max_id'])

    @staticmethod
    def _extract_one_shot_claims(commit_meta):
        meta = commit_meta or {}
        return {
            'feedback_ids': list(meta.get('feedback_ids') or []),
            'dream_id': meta.get('dream_id'),
            'wake_ids': list(meta.get('wake_ids') or []),
        }

    def peek_idle_seconds(self):
        """在更新 _last_used 前计算 idle；从未成功用过时返回 None。"""
        if not self._last_used:
            return None
        return max(0.0, time.time() - float(self._last_used))

    def _maybe_set_session_id(self, data):
        if not isinstance(data, dict):
            return
        sid = data.get('session_id') or data.get('sessionId')
        if sid:
            self._session_id = str(sid)

    def _jsonl_replay_cursor(self, jsonl_cursor=None):
        from tools.cc_jsonl_usage import snapshot_session_jsonl

        replay_cursor = jsonl_cursor
        if self._session_id and replay_cursor is None:
            replay_cursor = snapshot_session_jsonl(self._cwd, self._session_id)
            # 冷启动首轮：session_id 在流结束后才出现，需从文件头回放。
            if replay_cursor is not None and self._cold:
                replay_cursor = dict(replay_cursor)
                replay_cursor['offset'] = 0
        return replay_cursor

    def _attach_jsonl_usage_with_retry(self, usage, jsonl_cursor=None):
        """JSONL 落盘可能略晚于 stdout；短退避重试，避免 request_ids 恒空。"""
        from tools.cc_jsonl_usage import attach_jsonl_usage, replay_session_jsonl

        if not self._session_id:
            return usage
        replay_cursor = self._jsonl_replay_cursor(jsonl_cursor)
        delays = (0.0, 0.05, 0.15, 0.35)
        last_replay = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            last_replay = replay_session_jsonl(
                self._cwd, self._session_id, cursor=replay_cursor,
            )
            merged = attach_jsonl_usage(usage, last_replay)
            if int(merged.get('request_count') or 0) > 0:
                return merged
            has_usage = any(
                int(usage.get(key) or 0) > 0
                for key in ('cache_creation', 'input_tokens', 'output_tokens')
            )
            if not has_usage:
                break
        return attach_jsonl_usage(usage, last_replay)

    def send_turn(self, content, commit_meta=None):
        """Yield ('text'/'think'/'tool_use'/'tool_result'/'done', payload).

        done payload is (text, thinking, usage_dict, one_shot_claims).
        Flush 后只提交 state/group cursor；feedback/dream claims 经 done 带回，
        由 gateway 在 assistant 落库成功后再 consume。
        result.is_error / EOF / GeneratorExit（客户端断开）后 kill resident。
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise ResidentError('resident 进程不存在，需要先 ensure_alive')

        # 热 resident 从当前 EOF 开始；冷启动拿到 session_id 后从文件头回放。
        jsonl_cursor = None
        try:
            from tools.cc_jsonl_usage import snapshot_session_jsonl
            jsonl_cursor = snapshot_session_jsonl(self._cwd, self._session_id)
        except Exception:
            pass

        # 必须在更新 _last_used 前计算（成功路径末尾才写 _last_used）
        idle_seconds_before_turn = self.peek_idle_seconds()
        respawn_reason = self._pending_respawn_reason
        one_shot_claims = self._extract_one_shot_claims(commit_meta)
        payload = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}, ensure_ascii=False)
        try:
            proc.stdin.write(payload + NL)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self._kill(quiet=True)
            raise ResidentError('resident 进程管道已断: ' + str(e))

        # 信已塞进门缝：立刻提交 resident 游标（即使后续流中断也不重复塞）
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
                    self._maybe_set_session_id(d)
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
            except GeneratorExit:
                # 客户端断开 SSE：必须 kill，否则残留 stdout 会污染下一轮
                self._kill(quiet=True)
                raise
            except BaseException:
                if self._proc is not None:
                    self._kill(quiet=True)
                raise
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
        # JSONL 只补 request identity / TTL bucket / model；stream totals 保持权威。
        try:
            usage = self._attach_jsonl_usage_with_retry(usage, jsonl_cursor)
        except Exception:
            pass
        # 观测辅助字段：不改变既有 Usage v2 公开语义，供 gateway 组装 runtime
        usage['_obs_idle_seconds_before_turn'] = idle_seconds_before_turn
        usage['_obs_resident_generation'] = self._generation
        usage['_obs_resident_pid'] = getattr(proc, 'pid', None)
        usage['_obs_claude_session_id'] = self._session_id
        usage['_obs_keepwarm_lease_expires_at'] = self._keepwarm_lease_expires_at
        surface = self._tool_surface_snapshot or {}
        usage['_obs_tool_schema_sha256'] = surface.get('tool_schema_sha256')
        usage['_obs_tool_schema_text'] = surface.get('tool_schema_text')
        usage['_obs_tool_schema_source'] = surface.get('tool_schema_source')
        usage['_obs_tool_schema_measurement_status'] = surface.get(
            'tool_schema_measurement_status'
        )
        usage['_obs_tool_count'] = surface.get('tool_count')

        if timed_out[0]:
            raise ResidentError(
                'claude code 调用超时 (%ds)，resident 进程已重启' % CC_STREAM_TIMEOUT,
                usage=usage,
            )
        if not saw_result:
            self._kill(quiet=True)
            raise ResidentError('resident 进程在本轮回复完成前退出', usage=usage)
        if is_err:
            # payload 已写入 resident：kill 强制下一轮冷启动，避免脏会话继续热轮
            self._kill(quiet=True)
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
        # claims 只经 done 内部回传，不得写入公开 cache_info
        yield ('done', (''.join(text_acc).strip(), ''.join(think_acc), usage, one_shot_claims))

    def is_cold(self):
        return self._cold

    @property
    def last_state_snapshot(self):
        return self._last_state_snapshot

    @property
    def last_group_message_id(self):
        return self._last_group_message_id

    @property
    def group_cursor_initialized(self):
        return self._group_cursor_initialized

    @property
    def last_rel_fingerprint(self):
        return self._last_rel_fingerprint

    @property
    def turns_since_rel_sent(self):
        return self._turns_since_rel_sent

    @property
    def last_rel_mood(self):
        return self._last_rel_mood

    @property
    def pending_respawn_reason(self):
        return self._pending_respawn_reason

    @property
    def generation(self):
        return self._generation

    @property
    def session_id(self):
        return self._session_id

    @property
    def system_text(self):
        return self._system_text

    @property
    def allowed_tools(self):
        return self._allowed_tools

    @property
    def mcp_config_path(self):
        return self._mcp_config_path

    @property
    def tool_surface_snapshot(self):
        return self._tool_surface_snapshot or {}

    @property
    def keepwarm_lease_expires_at(self):
        return self._keepwarm_lease_expires_at

    def set_keepwarm_lease_expires_at(self, value):
        """UH-A will write authoritative keepwarm lease expiry (ISO-8601)."""
        self._keepwarm_lease_expires_at = value

    @property
    def resident_pid(self):
        proc = self._proc
        return getattr(proc, 'pid', None) if proc is not None else None

    def shutdown(self):
        with self._lock:
            self._kill()
