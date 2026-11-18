import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from wake_push_routes import create_wake_push_blueprint
from tools.execution_fence import _hook_result, write_current_turn_lease
from tools.lease_signer import issue_turn_lease
from wake.activity import list_activity, parse_response
from wake.executor import execute


class FreeTimeWakeTests(unittest.TestCase):
    def test_real_hook_budgets_are_shared_across_paths_per_phase(self):
        with tempfile.TemporaryDirectory() as d:
            for prefix, count, expected in [('wake-read-obs:', 4, 3), ('uh-a1-main-chat:', 3, 3),
                                             ('uh-a1-cold:', 3, 2)]:
                path = Path(d) / (prefix[:-1] + '.json')
                lease = issue_turn_lease(turn_id=prefix+'one',turn_mode='wake',issued_from='default_policy')
                write_current_turn_lease(path, lease)
                results = [_hook_result({'tool_name':'ToolSearch'}, {'UH_A0_TURN_LEASE_PATH':str(path)})
                           ['hookSpecificOutput'] for _ in range(count)]
                self.assertEqual(sum(r['permissionDecision']=='allow' for r in results), expected)
                if count > expected:
                    self.assertIn('wake_tool_budget_exhausted', results[-1]['permissionDecisionReason'])

    def test_activity_and_message_share_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d)/'wake.db')
            with sqlite3.connect(path) as db:
                db.executescript("""CREATE TABLE wake_log(id INTEGER PRIMARY KEY, thoughts,action,content,
                    consumed,woke_at,cache_info,wake_run_id);
                    CREATE TABLE chat_messages(id INTEGER PRIMARY KEY, author,content,thinking,source_kind,cache_info);""")
            for rid, raw in [('a','[[SILENT]]\n[[ACTIVITY]] 日记｜写了一页。'),
                             ('b','早安。\n[[ACTIVITY]] 不要显示｜摘要')]:
                text,silent,title,summary=parse_response(raw)
                with patch('chat.window_identity.soft_window_enabled',return_value=False):
                    execute('none' if silent else 'message','',text,'morning',
                            get_db_fn=lambda:sqlite3.connect(path), wake_run_id=rid,settlement_required=False,
                            cache_info={'unified_main_chat_proactive':True,'tool_calls':[{
                                'name':'diary.write','success':True,'time':'09:00','result':'private'}]},
                            activity=(title,summary))
            with sqlite3.connect(path) as db:
                rows=list_activity(db,'',5)
                self.assertEqual([r['spoke'] for r in rows],[True,False])
                self.assertEqual(rows[1]['title'],'日记')
                self.assertEqual(rows[1]['tools'],['diary.write'])
                self.assertEqual(db.execute('SELECT content FROM chat_messages').fetchall(),[('早安。',)])
                calls=json.loads(db.execute('SELECT tool_calls_json FROM wake_activity LIMIT 1').fetchone()[0])
                self.assertEqual(set(calls[0]),{'name','time','success'})
            app = Flask(__name__)
            app.register_blueprint(create_wake_push_blueprint(lambda: sqlite3.connect(path), None, None, None))
            response = app.test_client().get('/api/wake/activity?silent=1&limit=1')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['items'], [rows[1]])

    def test_malformed_and_long(self):
        self.assertEqual(parse_response('[[SILENT]]\n[[ACTIVITY]] incomplete'),('',True,'',''))
        response=parse_response('[[SILENT]]\n[[ACTIVITY]] '+'猫'*20+'｜'+'花'*50)
        self.assertEqual([len(s) for s in response[2:]],[16,40])
