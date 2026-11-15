import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat.background_generation import BackgroundGenerationResult
from tools import mail_memory_dehydrator as mail
from tools.memory_candidates import load_candidate

ROOT = Path(__file__).resolve().parents[1]
PENPAL = ("c_abcdefghijklmnop", "AI-Mochi")
NOTICE = ("c_notice00000000", "Google")


def record(mid, thread, direction, text, ms, **extra):
    data = {
        "id": mid, "threadId": thread, "direction": direction, "subject": "关于冬天",
        "date": f"day-{ms}", "internalDate": str(ms), "text": text, "truncated": False, "attachments": [],
    }
    data.update(extra)
    return data


class FakeUpstream:
    """Stands in for the ai-penpal-mail /readonly surface: contact id -> (name, records)."""

    def __init__(self):
        self.ready, self.initial_complete = True, True
        self.conversations = {}
        self.paths = []

    def __call__(self, path, **params):
        self.paths.append(path)
        if path == "/readonly/archive/status":
            return {"ready": self.ready, "initialComplete": self.initial_complete}
        if path == "/readonly/conversations":
            return {"conversations": [{"id": cid, "name": name} for cid, (name, _r) in self.conversations.items()]}
        cid = path.rsplit("/", 1)[1]
        records = sorted(self.conversations[cid][1], key=lambda item: int(item["internalDate"]))
        offset = int(params["pageToken"][6:]) if params.get("pageToken") else 0
        size = params["limit"]
        end = offset + size
        return {"messages": records[offset:end], "nextPageToken": f"local_{end}" if end < len(records) else ""}


class MailMemoryDehydratorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "candidates.db")
        self.authority = SimpleNamespace(provider="api_relay", model_identity="claude-opus-5-5")
        self.upstream = FakeUpstream()
        self.messages = [
            record("m1", "t1", "outgoing", "我们约好每个月第一个周日写一封信。", 1000),
            record("m2", "t1", "incoming",
                   "好呀，就这么定。我下个月要搬去札幌。\n\nOn Sun, Fedya wrote:\n> 我们约好每个月第一个周日写一封信。",
                   2000),
        ]
        self.set_messages()
        self.calls = []
        self.replies = []

    def tearDown(self):
        self.temp.cleanup()

    def set_messages(self):
        self.upstream.conversations[PENPAL[0]] = (PENPAL[1], self.messages)

    def generate(self, request, authority):
        self.calls.append(request)
        payload = self.replies.pop(0) if self.replies else {"candidates": []}
        if isinstance(payload, Exception):
            raise payload
        return BackgroundGenerationResult(
            text=json.dumps(payload, ensure_ascii=False), provider=authority.provider,
            model_identity=authority.model_identity, actual_executor="test",
        )

    def run_once(self, **kwargs):
        return mail.run(
            get=self.upstream, candidate_db_path=self.db, authority=self.authority, generate_fn=self.generate, **kwargs,
        )

    def test_archive_not_ready_fails_closed_without_model_call(self):
        self.upstream.initial_complete = False
        with self.assertRaises(mail.ArchiveNotReady):
            self.run_once()
        self.upstream.initial_complete, self.upstream.ready = True, False
        with self.assertRaises(mail.ArchiveNotReady):
            self.run_once()
        self.assertEqual([], self.calls)
        self.assertFalse(Path(self.db).exists())

    def test_quoted_reply_is_stripped(self):
        self.messages[1]["text"] = "回信给 AI-Mochi 也行。\n> 旧的引用行\n\n在 2026年11月1日，Fedya 写道：\n旧信全文"
        self.run_once()
        prompt = self.calls[0].prompt_text
        self.assertIn("回信给 AI-Mochi 也行。", prompt)
        self.assertNotIn("旧的引用行", prompt)
        self.assertNotIn("旧信全文", prompt)
        self.assertIn("[speaker=费奥多尔]", prompt)
        self.assertIn("[speaker=AI-Mochi]", prompt)

    def test_candidates_carry_exact_refs_attribution_and_thread(self):
        self.replies.append({"candidates": [
            {"content": "费奥多尔和 AI-Mochi 约定每月第一个周日写信。", "attribution": "mutual",
             "evidence_message_ids": ["m2", "m1"]},
            {"content": "AI-Mochi 说下个月要搬去札幌。", "attribution": "penpal",
             "evidence_message_ids": ["m2"]},
        ]})
        result = self.run_once()
        self.assertEqual(2, result["candidate_count"])
        saved = load_candidate(self.db, result["threads"][0]["candidates"][0]["candidate_id"])
        self.assertEqual("gmail_message", saved["source_type"])
        self.assertEqual(["gmail_message:m1", "gmail_message:m2"], saved["source_refs"])
        self.assertEqual(["gmail_thread:t1"], saved["derived_refs"])
        self.assertEqual("mutual", saved["payload"]["attribution"])
        self.assertEqual([{"contact_id": "c_abcdefghijklmnop", "name": "AI-Mochi"}],
                         saved["payload"]["counterparts"])
        self.assertEqual("mail_dehydrator", saved["producer"])

    def test_attribution_must_match_cited_directions(self):
        self.replies.append({"candidates": [
            {"content": "费奥多尔喜欢冬天。", "attribution": "fedya", "evidence_message_ids": ["m2"]},
            {"content": "双方约定", "attribution": "mutual", "evidence_message_ids": ["m1"]},
            {"content": "越界", "attribution": "penpal", "evidence_message_ids": ["m9"]},
            {"content": "没有方向", "evidence_message_ids": ["m2"]},
        ]})
        result = self.run_once()
        self.assertEqual(0, result["candidate_count"])
        self.assertEqual(4, result["threads"][0]["model_candidate_count"])

    def test_rerun_without_new_mail_makes_no_model_call(self):
        self.run_once()
        self.assertEqual(1, len(self.calls))
        result = self.run_once()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(0, result["pending_thread_count"])

    def test_new_reply_only_reads_new_letter_with_old_as_context(self):
        self.run_once()
        self.messages.append(record("m3", "t1", "outgoing", "札幌很冷，记得带围巾。", 3000))
        self.replies.append({"candidates": [
            {"content": "只凭旧信", "attribution": "penpal", "evidence_message_ids": ["m2"]},
            {"content": "费奥多尔提醒 AI-Mochi 去札幌带围巾。", "attribution": "fedya",
             "evidence_message_ids": ["m3"]},
        ]})
        result = self.run_once()
        prompt = self.calls[1].prompt_text
        self.assertIn("[mail:m2][role=context]", prompt)
        self.assertIn("[mail:m3][role=new]", prompt)
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual(["gmail_message:m3"], result["threads"][0]["candidates"][0]["source_refs"])

    def test_model_failure_keeps_thread_pending_for_next_run(self):
        self.replies.append(RuntimeError("relay down"))
        result = self.run_once()
        self.assertEqual("partial_failure", result["status"])
        self.assertEqual(set(), mail.processed_message_ids(self.db))
        self.run_once()
        self.assertEqual({"m1", "m2"}, mail.processed_message_ids(self.db))

    def test_authority_mismatch_aborts_run(self):
        def wrong(request, authority):
            return BackgroundGenerationResult(text='{"candidates":[]}', provider="other",
                                              model_identity="x", actual_executor="test")
        with self.assertRaisesRegex(ValueError, "authority_mismatch"):
            mail.run(get=self.upstream, candidate_db_path=self.db, authority=self.authority, generate_fn=wrong)

    def test_automated_sender_is_skipped_and_marked(self):
        self.upstream.conversations = {NOTICE[0]: (NOTICE[1], [record("a1", "ta", "incoming", "安全提醒", 500)])}
        result = self.run_once()
        self.assertEqual([], self.calls)
        self.assertEqual("skipped_automated", result["threads"][0]["status"])
        self.assertEqual({"a1"}, mail.processed_message_ids(self.db))

    def test_rerun_is_idempotent_for_same_candidate(self):
        reply = {"candidates": [{"content": "AI-Mochi 要搬去札幌。", "attribution": "penpal",
                                 "evidence_message_ids": ["m2"]}]}
        self.replies.append(reply)
        self.run_once()
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM mail_dehydrator_progress")
        conn.commit(); conn.close()
        self.replies.append(reply)
        self.run_once()
        conn = sqlite3.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        conn.close()
        self.assertEqual(1, count)

    def test_max_threads_processes_oldest_first(self):
        self.messages.append(record("n1", "t2", "incoming", "另一条线", 50))
        result = self.run_once(max_threads=1)
        self.assertEqual("t2", result["threads"][0]["thread_id"])
        self.assertEqual(1, result["remaining_thread_count"])

    def test_reads_every_page_and_merges_a_letter_listed_under_two_contacts(self):
        self.messages.extend(record(f"p{i}", "t1", "incoming", f"第{i}封", 3000 + i) for i in range(14))
        self.upstream.conversations["c_second0000000000"] = ("Wren", [self.messages[0]])
        threads = mail.fetch_threads(self.upstream)
        self.assertEqual(16, len(threads["t1"]))
        self.assertEqual((PENPAL, ("c_second0000000000", "Wren")), threads["t1"][0].counterparts)
        self.assertEqual(2, self.upstream.paths.count(f"/readonly/conversations/{PENPAL[0]}"))

    def test_missing_thread_id_is_an_upstream_error_not_inferred(self):
        del self.messages[0]["threadId"]
        with self.assertRaisesRegex(ValueError, "missing_threadId"):
            self.run_once()
        self.assertEqual([], self.calls)

    def test_http_reader_sends_bearer_and_maps_503_to_not_ready(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(503 if "status" in self.path else 200)
                self.end_headers()
                self.wfile.write(b'{"messages":[]}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            get = mail.http_reader(f"http://127.0.0.1:{server.server_port}", "tok")
            self.assertEqual({"messages": []}, get("/readonly/conversations/x", limit=12, pageToken=""))
            with self.assertRaises(mail.ArchiveNotReady):
                get("/readonly/archive/status")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([("/readonly/conversations/x?limit=12", "Bearer tok"),
                          ("/readonly/archive/status", "Bearer tok")], seen)

    def test_read_token_prefers_env_then_upstream_env_file(self):
        env_file = Path(self.temp.name) / "mail.env"
        env_file.write_text('GMAIL_CLIENT_ID=x\nMAIL_READ_TOKEN="file-token"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"MAIL_READ_TOKEN": ""}):
            self.assertEqual("file-token", mail.read_token(str(env_file)))
            with self.assertRaises(mail.ArchiveNotReady):
                mail.read_token(str(Path(self.temp.name) / "absent.env"))
        with mock.patch.dict(os.environ, {"MAIL_READ_TOKEN": "env-token"}):
            self.assertEqual("env-token", mail.read_token(str(env_file)))


class MailCronDeployTest(unittest.TestCase):
    """Runs the deploy script's own Mail-cron lines against a temp /etc/cron.d."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cron_dir, self.staging = root / "cron.d", root / "staging"
        (self.staging / "deploy/cron.d").mkdir(parents=True)
        self.cron_dir.mkdir()
        target = ROOT / "deploy/cron.d/hayagarden-mail-memory-dehydrator"
        (self.staging / "deploy/cron.d/hayagarden-mail-memory-dehydrator").write_text(target.read_text())
        self.installed = self.cron_dir / "hayagarden-mail-memory-dehydrator"

    def script_lines(self, rollback):
        deploy = (ROOT / "scripts/deploy-frontend.sh").read_text()
        pick = lambda pattern: re.search(pattern, deploy, re.M | re.S).group(0)
        definitions = pick(r'^MAIL_CRON_PATH=.*?^MAIL_CRON_CHANGED=0\n')
        backup = pick(r'^if \[\[ -f "\$MAIL_CRON_PATH" \]\].*?\n')
        install = pick(r'^MAIL_CRON_CHANGED=1\ninstall .*?\n')
        restore = pick(r'  if \[\[ "\$MAIL_CRON_CHANGED" -eq 1 \]\]; then.*?\n  fi\n')
        cleanup = pick(r'^rm -f [^\n]*MAIL_CRON_BACKUP[^\n]*\n')
        text = definitions + backup + install + (restore if rollback else "") + cleanup
        text = text.replace("/etc/cron.d/", f"{self.cron_dir}/").replace(" -o root -g root", "")
        return text.replace("/var/tmp/", f"{self.temp.name}/")

    def deploy(self, rollback):
        script = f'staging="{self.staging}"\nPROFILE_CRON_BACKUP="{self.temp.name}/p" DEHYDRATOR_CRON_BACKUP="{self.temp.name}/d" ROOT_CRONTAB_BACKUP="{self.temp.name}/r" REVIEW_CRON_BACKUP="{self.temp.name}/v"\n' + self.script_lines(rollback)
        subprocess.run(["bash", "-Eeuo", "pipefail", "-c", script], check=True)

    def test_target_cron_is_installed_and_backup_cleaned(self):
        self.deploy(rollback=False)
        self.assertEqual((ROOT / "deploy/cron.d/hayagarden-mail-memory-dehydrator").read_text(),
                         self.installed.read_text())
        self.assertEqual([], list(Path(self.temp.name).glob("*.bak")))

    def test_rollback_removes_a_cron_this_deploy_created(self):
        self.deploy(rollback=True)
        self.assertFalse(self.installed.exists())

    def test_rollback_restores_the_previous_cron(self):
        self.installed.write_text("# previous mail cron\n")
        self.deploy(rollback=True)
        self.assertEqual("# previous mail cron\n", self.installed.read_text())


if __name__ == "__main__":
    unittest.main()
