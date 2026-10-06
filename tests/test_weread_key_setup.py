import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as frontend_app


VALID_KEY = "wrk-" + "a" * 32
READ_HTML = Path(ROOT) / "static" / "read.html"


class WereadKeySetupRouteTests(unittest.TestCase):
    def setUp(self):
        frontend_app.app.config.update(TESTING=True)
        self.client = frontend_app.app.test_client()
        self.previous_key = os.environ.get("WEREAD_API_KEY")

    def tearDown(self):
        if self.previous_key is None:
            os.environ.pop("WEREAD_API_KEY", None)
        else:
            os.environ["WEREAD_API_KEY"] = self.previous_key

    def test_missing_key_returns_400(self):
        response = self.client.post("/api/config/weread-key", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"ok": False, "code": "WEREAD_KEY_INVALID"})

    def test_valid_shaped_key_is_persisted_and_restarts_frontend_only(self):
        with mock.patch.object(frontend_app, "_env_set") as env_set, mock.patch("subprocess.Popen") as popen:
            response = self.client.post(
                "/api/config/weread-key",
                json={"key": "  " + VALID_KEY + "  "},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})
        env_set.assert_called_once_with("WEREAD_API_KEY", VALID_KEY)
        self.assertEqual(popen.call_args.args[0], ["systemctl", "restart", "frontend"])
        self.assertNotIn("frontend-gw", str(popen.call_args))
        self.assertEqual(os.environ.get("WEREAD_API_KEY"), VALID_KEY)

    def test_response_never_contains_key(self):
        with mock.patch.object(frontend_app, "_env_set"), mock.patch("subprocess.Popen"):
            response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
        self.assertNotIn(VALID_KEY, response.get_data(as_text=True))

    def test_exception_response_never_contains_key(self):
        with mock.patch.object(frontend_app, "_env_set", side_effect=RuntimeError(VALID_KEY)):
            response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"ok": False, "code": "WEREAD_KEY_SAVE_FAILED"})
        self.assertNotIn(VALID_KEY, response.get_data(as_text=True))

    def test_read_empty_state_has_secure_connection_flow(self):
        source = READ_HTML.read_text(encoding="utf-8")
        self.assertIn("微信读书还没有连接", source)
        self.assertIn("连接微信读书", source)
        self.assertIn("POST", source)
        self.assertIn("/api/config/weread-key", source)
        self.assertIn("/api/weread/shelf", source)

    def test_modal_uses_password_input_and_no_key_storage(self):
        source = READ_HTML.read_text(encoding="utf-8")
        self.assertIn('id="weread-key-input" type="password" autocomplete="off"', source)
        self.assertNotIn("sessionStorage", source)
        self.assertNotIn("localStorage.setItem('WEREAD_API_KEY'", source)
        self.assertNotIn('localStorage.setItem("WEREAD_API_KEY"', source)
        self.assertNotIn("WEREAD_API_KEY=", source)

    def test_save_then_shelf_success_is_wired_to_real_book_shelf_state(self):
        source = READ_HTML.read_text(encoding="utf-8")
        self.assertLess(source.index("/api/config/weread-key"), source.index("await this.apiJson('/api/weread/shelf')"))
        self.assertIn("await this.loadLibrary(remote)", source)
        self.assertIn("connectModal: false", source)
        self.assertIn("微信读书 · 已连接", source)

    def test_demo_shelf_data_is_not_reintroduced(self):
        source = READ_HTML.read_text(encoding="utf-8")
        self.assertIn("SHELF = []", source)
        self.assertNotIn("demoShelf", source)
        self.assertNotIn("DEMO_SHELF", source)
        self.assertNotIn("localStorage.setItem('WEREAD_API_KEY'", source)


if __name__ == "__main__":
    unittest.main()
