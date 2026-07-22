import os
import sqlite3
import tempfile
import unittest

from monopoly_engine import EngineClient, EngineReply, EngineUnavailable, EngineValidationError
from monopoly_agents import CCAdapter, MonopolyAgentScheduler, _REFUSAL_RE, _allowed_actions, _parse_output
from monopoly_rooms import MonopolyService, PassthroughTokenCipher, RoomError
import monopoly_store as store


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.current_state = {"turn": "哈娅", "coins": {"哈娅": 10, "CC": 10}}
        self.next_roll = {
            "who": "哈娅",
            "next_turn": "CC",
            "task": {"text": "task one"},
        }

    def new_game(self, payload):
        self.calls.append(("new_game", payload))
        return {
            "game_id": "game-1",
            "player_token": "delete-me",
            "state": dict(self.current_state),
            "active_limits": {"redline": ["blood"]},
            "history_note": "fresh room",
        }

    def state(self, game_id):
        self.calls.append(("state", game_id))
        return dict(self.current_state)

    def roll(self, game_id, body):
        self.calls.append(("roll", game_id, dict(body)))
        payload = self.next_roll
        nested = payload.get("state")
        if isinstance(nested, dict):
            self.current_state = dict(nested)
        elif isinstance(payload.get("next_turn"), str):
            self.current_state = {**self.current_state, "turn": payload["next_turn"]}
        return EngineReply(payload)

    def action(self, action, game_id, **params):
        self.calls.append((action, game_id, params))
        return {"state": dict(self.current_state), "task": {"text": "replacement"}} if action == "swap" else {"state": dict(self.current_state)}

    def delete_game(self, game_id, token):
        self.calls.append(("delete_game", game_id, token))
        return {"ok": True}


class MonopolyBackendTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.lock_dir = tempfile.mkdtemp(prefix="monopoly-locks-")
        self.engine = FakeEngine()
        from monopoly_rooms import RoomLockRegistry
        self.service = MonopolyService(
            db_path=self.db_path,
            engine=self.engine,
            cipher=PassthroughTokenCipher(),
            locks=RoomLockRegistry(self.lock_dir),
        )
        self.room_id = self.service.create_room()["room"]["id"]
        seq = self.service.snapshot(self.room_id)["room"]["event_seq"]
        self.service.setup(
            self.room_id,
            {"flavor": "medium", "p1_name": "哈娅", "p2_name": "CC"},
            expected_seq=seq,
        )

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass
        for name in os.listdir(self.lock_dir):
            os.unlink(os.path.join(self.lock_dir, name))
        os.rmdir(self.lock_dir)

    def _seq(self):
        return self.service.snapshot(self.room_id)["room"]["event_seq"]

    def test_setup_persists_safety_confirmation_and_encrypts_token(self):
        room = store.get_room(self.room_id, self.db_path, include_token=True)
        self.assertEqual(room["player_token_cipher"], "test:delete-me")
        events = store.list_events(self.room_id, db_path=self.db_path)
        confirmation = next(event for event in events if event["type"] == "setup_confirmed")
        self.assertEqual(confirmation["payload"]["active_limits"], {"redline": ["blood"]})
        self.assertEqual(confirmation["payload"]["history_note"], "fresh room")
        started = next(event for event in events if event["type"] == "game_started")
        self.assertNotIn("player_token", started["payload"])
        self.assertNotIn("delete-me", str(self.service.snapshot(self.room_id)["state"]))

    def test_room_database_uses_wal(self):
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            conn.close()

    def test_live_typing_events_do_not_advance_game_event_seq(self):
        before = self._seq()
        live = store.append_live_event(
            self.room_id, "chat_delta", "cc", {"delta": "hello"}, self.db_path
        )
        self.assertEqual(self._seq(), before)
        events, messages, live_events = store.poll_room_stream(
            self.room_id,
            after_seq=before,
            after_message_id=0,
            after_live_id=live["id"] - 1,
            db_path=self.db_path,
        )
        self.assertEqual(events, [])
        self.assertTrue(isinstance(messages, list))
        self.assertEqual([event["payload"]["delta"] for event in live_events], ["hello"])

    def test_lazy_decision_does_not_call_engine_until_next_roll(self):
        self.service.execute(self.room_id, {"action": "roll", "actor": "haya", "expected_seq": self._seq()})
        roll_calls = len([call for call in self.engine.calls if call[0] == "roll"])
        snapshot = self.service.snapshot(self.room_id)
        self.assertEqual(snapshot["room"]["status"], "task_pending")
        self.assertEqual(snapshot["pending"]["default"], {"task": "done"})

        self.service.execute(self.room_id, {
            "action": "decide", "actor": "haya", "args": {"task": "skip"},
            "expected_seq": self._seq(),
        })
        self.assertEqual(len([call for call in self.engine.calls if call[0] == "roll"]), roll_calls)

        self.engine.next_roll = {"who": "CC", "next_turn": "哈娅", "tile_type": "normal"}
        self.service.execute(self.room_id, {"action": "roll", "actor": "cc", "expected_seq": self._seq()})
        self.assertEqual(
            [call for call in self.engine.calls if call[0] == "roll"][-1],
            ("roll", "game-1", {"task": "skip"}),
        )
        events = store.list_events(self.room_id, db_path=self.db_path)
        settled_index = max(index for index, event in enumerate(events) if event["type"] == "settled")
        rolled_index = max(index for index, event in enumerate(events) if event["type"] == "rolled")
        self.assertLess(settled_index, rolled_index)

    def test_duel_pending_rejects_roll_until_winner_chosen(self):
        self.engine.next_roll = {
            "who": "哈娅", "next_turn": "CC",
            "action_needed": "duel",
        }
        self.service.execute(self.room_id, {"action": "roll", "actor": "haya", "expected_seq": self._seq()})
        with self.assertRaises(RoomError) as caught:
            self.service.execute(self.room_id, {"action": "roll", "actor": "cc", "expected_seq": self._seq()})
        self.assertEqual(caught.exception.code, "DUEL_WINNER_REQUIRED")
        self.service.execute(self.room_id, {
            "action": "duel_result", "actor": "haya", "args": {"winner": "haya"},
            "expected_seq": self._seq(),
        })
        self.engine.next_roll = {"who": "CC", "next_turn": "哈娅"}
        self.service.execute(self.room_id, {"action": "roll", "actor": "cc", "expected_seq": self._seq()})
        self.assertEqual(
            [call for call in self.engine.calls if call[0] == "roll"][-1][2],
            {"duel_winner": "哈娅"},
        )

    def test_direct_roll_decision_maps_actor_duel_winner_to_engine_name(self):
        self.engine.next_roll = {"who": "哈娅", "next_turn": "CC", "action_needed": "duel"}
        self.service.execute(self.room_id, {
            "action": "roll", "actor": "haya", "expected_seq": self._seq(),
        })
        self.engine.next_roll = {"who": "CC", "next_turn": "哈娅"}
        self.service.execute(self.room_id, {
            "action": "roll",
            "actor": "cc",
            "args": {"decision": {"duel_winner": "haya"}},
            "expected_seq": self._seq(),
        })
        self.assertEqual(
            [call for call in self.engine.calls if call[0] == "roll"][-1][2],
            {"duel_winner": "哈娅"},
        )

    def test_stale_seq_is_rejected_before_engine_call(self):
        before = len(self.engine.calls)
        with self.assertRaises(RoomError) as caught:
            self.service.execute(self.room_id, {"action": "roll", "actor": "haya", "expected_seq": 0})
        self.assertEqual(caught.exception.code, "STALE_ROOM_STATE")
        self.assertEqual(caught.exception.latest, self._seq())
        self.assertEqual(len(self.engine.calls), before)

    def test_safe_word_pauses_before_agents_can_continue(self):
        ordinary = self.service.post_message(self.room_id, author="haya", content="接口报了 HTTP 404")
        self.assertNotEqual(ordinary["snapshot"]["room"]["status"], "paused")
        result = self.service.post_message(self.room_id, author="haya", content="404")
        self.assertEqual(result["snapshot"]["room"]["status"], "paused")
        self.assertEqual(result["snapshot"]["messages"][-1]["content"], "游戏已停")
        with self.assertRaises(RoomError) as caught:
            self.service.execute(self.room_id, {"action": "roll", "actor": "haya", "expected_seq": self._seq()})
        self.assertEqual(caught.exception.code, "GAME_PAUSED")

    def test_explicit_safe_word_field_pauses_without_magic_substring(self):
        result = self.service.post_message(
            self.room_id, author="haya", content="先停一下", safe_word=True
        )
        self.assertEqual(result["snapshot"]["room"]["status"], "paused")

    def test_resume_always_removes_paused_from_marker(self):
        self.service.execute(self.room_id, {
            "action": "roll", "actor": "haya", "expected_seq": self._seq(),
        })
        self.service.pause(self.room_id, expected_seq=self._seq())
        resumed = self.service.resume(self.room_id, expected_seq=self._seq())
        self.assertEqual(resumed["room"]["status"], "task_pending")
        self.assertNotIn("paused_from", store.get_agent_state(self.room_id, self.db_path))

    def test_real_engine_player_names_map_back_to_fixed_actors(self):
        handle, db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            service = MonopolyService(
                db_path=db_path,
                engine=self.engine,
                cipher=PassthroughTokenCipher(),
                locks=self.service.locks,
            )
            room_id = service.create_room()["room"]["id"]
            seq = service.snapshot(room_id)["room"]["event_seq"]
            self.engine.current_state = {"turn": "哈娅", "coins": {"哈娅": 10, "CC": 10}}
            service.setup(
                room_id,
                {"p1_name": "哈娅", "p2_name": "CC", "first_player": "哈娅"},
                expected_seq=seq,
            )
            self.engine.next_roll = {
                "who": "哈娅", "next_turn": "CC", "tile": "start", "board": "board"
            }
            result = service.execute(room_id, {
                "action": "roll", "actor": "haya",
                "expected_seq": service.snapshot(room_id)["room"]["event_seq"],
            })
            self.assertEqual(result["room"]["active_actor"], "cc")
        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass

    def test_unknown_timeout_result_freezes_instead_of_dropping_pending(self):
        def uncertain_roll(game_id, body):
            return EngineReply(
                {"state": {"turn": "CC", "coins": {"哈娅": 9, "CC": 11}}},
                reconciled=True,
                outcome_unknown=True,
            )

        self.engine.roll = uncertain_roll
        with self.assertRaises(RoomError) as caught:
            self.service.execute(self.room_id, {
                "action": "roll", "actor": "haya", "expected_seq": self._seq(),
            })
        self.assertEqual(caught.exception.code, "ROLL_OUTCOME_UNKNOWN")
        snapshot = self.service.snapshot(self.room_id)
        self.assertEqual(snapshot["room"]["status"], "engine_down")
        self.assertIsNone(snapshot["pending"])
        with self.assertRaises(RoomError) as resume_error:
            self.service.resume(self.room_id, expected_seq=self._seq())
        self.assertEqual(resume_error.exception.code, "ROLL_OUTCOME_UNKNOWN")

    def test_swap_is_the_only_immediate_pending_card_change(self):
        self.service.execute(self.room_id, {"action": "roll", "actor": "haya", "expected_seq": self._seq()})
        original_pending = self.service.snapshot(self.room_id)["pending"]
        self.service.execute(self.room_id, {
            "action": "use_card", "actor": "haya", "args": {"index": 0},
            "expected_seq": self._seq(),
        })
        self.assertEqual(self.service.snapshot(self.room_id)["pending"], original_pending)
        self.service.execute(self.room_id, {"action": "swap", "actor": "haya", "expected_seq": self._seq()})
        self.assertEqual([call for call in self.engine.calls if call[0] == "swap"][-1][0], "swap")
        self.assertEqual(self.service.snapshot(self.room_id)["pending"]["kind"], "task")

    def test_engine_validation_is_persisted_as_room_error(self):
        def invalid_roll(game_id, body):
            raise EngineValidationError(
                "ENGINE_VALIDATION", "bad redline", status=422,
                payload={"detail": "legal values: ..."},
            )
        self.engine.roll = invalid_roll
        with self.assertRaises(RoomError) as caught:
            self.service.execute(self.room_id, {
                "action": "roll", "actor": "haya", "expected_seq": self._seq(),
            })
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(store.list_events(self.room_id, db_path=self.db_path)[-1]["type"], "room_error")

    def test_ai_cannot_roll_while_another_players_pending_card_waits(self):
        self.service.execute(self.room_id, {"action": "roll", "actor": "haya", "expected_seq": self._seq()})
        snapshot = self.service.snapshot(self.room_id)
        self.assertEqual(snapshot["pending"]["actor"], "haya")
        actions = _allowed_actions(snapshot, "cc")
        self.assertNotIn("roll", {action["action"] for action in actions})
        self.service.execute(self.room_id, {
            "action": "decide", "actor": "haya", "args": {"task": "done"},
            "expected_seq": self._seq(),
        })
        actions = _allowed_actions(self.service.snapshot(self.room_id), "cc")
        self.assertIn("roll", {action["action"] for action in actions})

    def test_pending_persists_display_text_for_refresh(self):
        self.service.execute(self.room_id, {"action": "roll", "actor": "haya", "expected_seq": self._seq()})
        pending = self.service.snapshot(self.room_id)["pending"]
        self.assertEqual(pending["display"]["text"], "task one")
        reloaded = self.service.snapshot(self.room_id)["pending"]
        self.assertEqual(reloaded["display"]["text"], "task one")

    def test_pending_event_carries_canonical_status(self):
        self.service.execute(self.room_id, {"action": "roll", "actor": "haya", "expected_seq": self._seq()})
        events = [event for event in store.list_events(self.room_id, db_path=self.db_path) if event["type"] == "pending"]
        last = events[-1]
        self.assertEqual(last["payload"]["status"], "task_pending")
        self.assertEqual(last["payload"]["pending"]["kind"], "task")

    def test_unknown_roll_pending_event_keeps_engine_down_status(self):
        def uncertain_roll(game_id, body):
            return EngineReply(
                {"state": {"turn": "CC", "coins": {"哈娅": 9, "CC": 11}}},
                reconciled=True,
                outcome_unknown=True,
            )

        self.engine.roll = uncertain_roll
        with self.assertRaises(RoomError):
            self.service.execute(self.room_id, {
                "action": "roll", "actor": "haya", "expected_seq": self._seq(),
            })
        events = [event for event in store.list_events(self.room_id, db_path=self.db_path) if event["type"] == "pending"]
        last = events[-1]
        self.assertEqual(last["payload"]["status"], "engine_down")
        self.assertIsNone(last["payload"]["pending"])

    def test_delete_uses_decrypted_engine_token(self):
        self.service.delete(self.room_id)
        self.assertIn(("delete_game", "game-1", "delete-me"), self.engine.calls)
        self.assertIsNone(store.get_room(self.room_id, self.db_path))


class AmbiguousRollClientTests(unittest.TestCase):
    def test_timeout_reconciles_without_resend_if_turn_advanced(self):
        class Client(EngineClient):
            def __init__(self):
                super().__init__("http://unused")
                self.calls = []

            def _request(self, method, path, body=None):
                self.calls.append((method, path, body))
                if method == "GET" and len(self.calls) == 1:
                    return {"turn": 1, "current_player": "p1"}
                if method == "POST":
                    raise EngineUnavailable("ENGINE_UNAVAILABLE", "timeout", status=503)
                return {"turn": 2, "current_player": "p2"}

        client = Client()
        reply = client.roll("g", {"task": "done"})
        self.assertTrue(reply.reconciled)
        self.assertTrue(reply.outcome_unknown)
        self.assertEqual(len([call for call in client.calls if call[0] == "POST"]), 1)

    def test_all_documented_action_paths_are_url_encoded(self):
        class Client(EngineClient):
            def __init__(self):
                super().__init__("http://unused")
                self.paths = []

            def _request(self, method, path, body=None):
                self.paths.append(path)
                return {}

        client = Client()
        examples = {
            "skip": {"who": "p1"}, "swap": {"who": "p1"},
            "duel_result": {"winner": "p1"}, "pay_toll": {"who": "p1"},
            "serve_toll": {"who": "p1"},
            "buyout": {"who": "p1"}, "buy_card": {"who": "p1"},
            "use_card": {"who": "p1", "index": 1}, "discard": {"who": "p1", "index": 1},
            "reroll_identity": {"who": "p1"}, "reroll_task": {"who": "p1"},
            "id_event": {"who": "p1", "event": "first_climax"},
            "extra_task": {"who": "p1"}, "guess_mark": {"guesser": "p1", "part": "腰 侧"},
            "declare_persona": {"who": "p1", "persona": "bad girl"},
        }
        for action, params in examples.items():
            client.action(action, "game/one", **params)
        self.assertEqual(len(client.paths), len(examples))
        self.assertTrue(all("game%2Fone" in path for path in client.paths))
        self.assertTrue(any("%E8%85%B0%20%E4%BE%A7" in path for path in client.paths))

    def test_declare_persona_uses_json_body_not_url(self):
        class Client(EngineClient):
            def __init__(self):
                super().__init__("http://unused")
                self.request = None

            def _request(self, method, path, body=None):
                self.request = (method, path, body)
                return {}

        client = Client()
        client.declare_persona("g", "哈娅", "老师")
        self.assertEqual(client.request[1], "/declare_persona/g/%E5%93%88%E5%A8%85")
        self.assertEqual(client.request[2], {"persona": "老师"})


class AgentOutputTests(unittest.TestCase):
    def test_structured_intent_is_removed_from_chat_text(self):
        text, intent = _parse_output(
            '我来掷。\n[game_intent]{"action":"roll","args":{"guess":"大"}}[/game_intent]'
        )
        self.assertEqual(text, "我来掷。")
        self.assertEqual(intent["args"]["guess"], "大")

    def test_refusal_detection_is_anchored_and_does_not_match_roleplay(self):
        self.assertIsNone(_REFUSAL_RE.search("我无法完成这个任务，换一张吧。"))
        self.assertIsNone(_REFUSAL_RE.search("这个政策限制很奇怪。"))
        self.assertIsNotNone(_REFUSAL_RE.search("抱歉，我不能协助这个请求。"))


class MonopolyRouteAuthTests(unittest.TestCase):
    def test_entire_blueprint_requires_owner_auth(self):
        try:
            from flask import Flask
            from moments_auth import OwnerAuthError
            from monopoly_routes import create_monopoly_blueprint
        except ModuleNotFoundError as exc:
            self.skipTest(f"Flask runtime unavailable: {exc}")
        handle, db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        lock_dir = tempfile.mkdtemp(prefix="monopoly-auth-locks-")
        try:
            from monopoly_rooms import RoomLockRegistry

            service = MonopolyService(
                db_path=db_path,
                engine=FakeEngine(),
                cipher=PassthroughTokenCipher(),
                locks=RoomLockRegistry(lock_dir),
            )
            app = Flask(__name__)

            def reject(_request):
                raise OwnerAuthError("unauthorized", 401)

            app.register_blueprint(create_monopoly_blueprint(service, owner_guard=reject))
            response = app.test_client().post("/api/monopoly/rooms", json={})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json()["code"], "OWNER_AUTH_REQUIRED")
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass
            if os.path.isdir(lock_dir):
                for name in os.listdir(lock_dir):
                    os.unlink(os.path.join(lock_dir, name))
                os.rmdir(lock_dir)


class CCAdapterSnapshotTests(unittest.TestCase):
    @staticmethod
    def _adapter(provider, relay, label=""):
        return CCAdapter(
            provider_getter=lambda: provider,
            model_getter=lambda: "fallback-model",
            relay_factory=lambda: relay,
            token_getter=lambda: "token",
            cwd=".",
            allowed_tools="",
            relay_label_getter=lambda: label,
        )

    def test_snapshot_distinguishes_official_relay_and_claude_code(self):
        official_relay = type("Relay", (), {"api_url": "https://api.anthropic.com/v1/messages", "model": "opus"})()
        snapshot = self._adapter("api_relay", official_relay).snapshot()
        self.assertEqual(snapshot, {"kind": "official", "label": "Anthropic Official", "model": "opus"})

        guagua = type("Relay", (), {"api_url": "https://relay.example/v1/messages", "model": "sonnet"})()
        snapshot = self._adapter("api_relay", guagua, "guagua").snapshot()
        self.assertEqual(snapshot, {"kind": "relay", "label": "guagua", "model": "sonnet"})

        snapshot = self._adapter("claude_code", guagua).snapshot()
        self.assertEqual(snapshot["kind"], "claude_code")


class MonopolyAgentSchedulerTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.lock_dir = tempfile.mkdtemp(prefix="monopoly-agent-locks-")
        self.engine = FakeEngine()
        from monopoly_rooms import RoomLockRegistry

        self.service = MonopolyService(
            db_path=self.db_path,
            engine=self.engine,
            cipher=PassthroughTokenCipher(),
            locks=RoomLockRegistry(self.lock_dir),
        )
        self.room_id = self.service.create_room()["room"]["id"]
        self.service.setup(
            self.room_id,
            {"p1_name": "哈娅", "p2_name": "CC"},
            expected_seq=self.service.snapshot(self.room_id)["room"]["event_seq"],
        )
        self.engine.next_roll = {"who": "哈娅", "next_turn": "CC", "tile_type": "normal"}
        self.service.execute(self.room_id, {
            "actor": "haya", "action": "roll",
            "expected_seq": self.service.snapshot(self.room_id)["room"]["event_seq"],
        })

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass
        for name in os.listdir(self.lock_dir):
            os.unlink(os.path.join(self.lock_dir, name))
        os.rmdir(self.lock_dir)

    def _scheduler(self, cc):
        codex = type("Codex", (), {"snapshot": lambda self: {}, "stream": lambda *args: iter(())})()
        return MonopolyAgentScheduler(
            self.service,
            persona_builder=lambda: "",
            cc=cc,
            codex=codex,
        )

    def test_network_failure_never_swaps_skips_or_rolls(self):
        class FailingCC:
            @staticmethod
            def snapshot():
                return {"kind": "relay", "label": "guagua", "model": "sonnet"}

            @staticmethod
            def stream(*_args):
                raise RuntimeError("temporary network failure")

        before_calls = list(self.engine.calls)
        self._scheduler(FailingCC())._generate_one(self.room_id, "cc", "turn", None)
        self.assertEqual(self.engine.calls, before_calls)
        live = store.poll_room_stream(
            self.room_id,
            after_seq=self.service.snapshot(self.room_id)["room"]["event_seq"],
            after_message_id=0,
            after_live_id=0,
            db_path=self.db_path,
        )[2]
        self.assertTrue(any(event["type"] == "agent_status" for event in live))

    def test_cc_roll_that_draws_own_task_gets_one_followup_turn(self):
        outputs = iter([
            '我来掷。\n[game_intent]{"action":"roll","args":{}}[/game_intent]',
            '这张我做完。\n[game_intent]{"action":"decide","args":{"task":"done"}}[/game_intent]',
        ])

        class ScriptedCC:
            @staticmethod
            def snapshot():
                return {"kind": "relay", "label": "guagua", "model": "sonnet"}

            @staticmethod
            def stream(*_args):
                yield next(outputs)

        self.engine.next_roll = {
            "who": "CC", "next_turn": "哈娅", "task": {"text": "task for CC"}
        }
        self._scheduler(ScriptedCC())._generate_one(self.room_id, "cc", "turn", None)
        snapshot = self.service.snapshot(self.room_id)
        self.assertEqual(snapshot["pending"]["actor"], "cc")
        self.assertEqual(snapshot["pending"]["chosen"], {"task": "done"})
        cc_messages = [message for message in snapshot["messages"] if message["author"] == "cc"]
        self.assertEqual(len(cc_messages), 2)


class MonopolyRoomStateScriptTests(unittest.TestCase):
    def test_room_state_reducer_contract(self):
        import shutil
        import subprocess
        from pathlib import Path

        node = shutil.which('node')
        if not node:
            self.skipTest('node not available')
        script = Path(__file__).with_name('test_monopoly_room_state.mjs')
        completed = subprocess.run([node, str(script)], check=False, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()

