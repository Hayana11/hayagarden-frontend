import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as frontend_app
from weread_client import WereadError


VALID_KEY = "wrk-" + "a" * 32
OLD_KEY = "wrk-" + "o" * 32
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

    def _allow_owner_same_origin(self):
        return mock.patch.object(frontend_app, "require_owner", return_value=None), mock.patch.object(
            frontend_app, "same_origin_mutation_ok", return_value=True
        )

    def test_unauthenticated_request_is_rejected(self):
        denied = frontend_app.OwnerAuthError("unauthorized", 401)
        with mock.patch.object(frontend_app, "require_owner", side_effect=denied), mock.patch.object(
            frontend_app, "WereadClient"
        ) as weread:
            response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"ok": False, "error": "unauthorized"})
        weread.assert_not_called()

    def test_owner_auth_not_configured_is_rejected(self):
        denied = frontend_app.OwnerAuthError("owner auth is not configured", 503)
        with mock.patch.object(frontend_app, "require_owner", side_effect=denied):
            response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"ok": False, "error": "owner auth is not configured"})

    def test_cross_origin_request_is_rejected(self):
        with mock.patch.object(frontend_app, "require_owner", return_value=None), mock.patch.object(
            frontend_app, "same_origin_mutation_ok", return_value=False
        ), mock.patch.object(frontend_app, "WereadClient") as weread:
            response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"ok": False, "error": "cross-origin mutation rejected"})
        weread.assert_not_called()

    def test_missing_key_returns_400_without_live_request(self):
        owner_patch, origin_patch = self._allow_owner_same_origin()
        with owner_patch, origin_patch, mock.patch.object(frontend_app, "WereadClient") as weread:
            response = self.client.post("/api/config/weread-key", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"ok": False, "code": "WEREAD_KEY_INVALID"})
        weread.assert_not_called()

    def test_malformed_key_does_not_call_weread(self):
        owner_patch, origin_patch = self._allow_owner_same_origin()
        with owner_patch, origin_patch, mock.patch.object(frontend_app, "WereadClient") as weread:
            response = self.client.post("/api/config/weread-key", json={"key": "not-a-weread-key"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"ok": False, "code": "WEREAD_KEY_INVALID"})
        weread.assert_not_called()

    def test_unauthorized_candidate_preserves_old_secret_and_skips_restart(self):
        os.environ["WEREAD_API_KEY"] = OLD_KEY
        candidate = mock.Mock()
        candidate.call.side_effect = WereadError("WEREAD_UNAUTHORIZED", 502)
        owner_patch, origin_patch = self._allow_owner_same_origin()
        with owner_patch, origin_patch, mock.patch.object(frontend_app, "WereadClient", return_value=candidate), mock.patch.object(
            frontend_app, "_env_set"
        ) as env_set, mock.patch("subprocess.Popen") as popen:
            response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"ok": False, "code": "WEREAD_KEY_INVALID"})
        candidate.call.assert_called_once_with("/shelf/sync")
        env_set.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(os.environ.get("WEREAD_API_KEY"), OLD_KEY)
        body = response.get_data(as_text=True)
        self.assertNotIn(OLD_KEY, body)
        self.assertNotIn(VALID_KEY, body)

    def test_upgrade_and_rate_limit_are_safe_structured_errors(self):
        for error, code, status in (
            (WereadError("WEREAD_UPGRADE_REQUIRED", 502), "WEREAD_UPGRADE_REQUIRED", 502),
            (WereadError("WEREAD_RATE_LIMITED", 429), "WEREAD_RATE_LIMITED", 429),
        ):
            candidate = mock.Mock()
            candidate.call.side_effect = error
            owner_patch, origin_patch = self._allow_owner_same_origin()
            with owner_patch, origin_patch, mock.patch.object(frontend_app, "WereadClient", return_value=candidate), mock.patch.object(
                frontend_app, "_env_set"
            ) as env_set, mock.patch("subprocess.Popen") as popen:
                response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.get_json(), {"ok": False, "code": code})
            env_set.assert_not_called()
            popen.assert_not_called()

    def test_success_validates_before_persisting_and_restarts_frontend_only(self):
        candidate = mock.Mock()
        candidate.call.return_value = {"books": []}
        owner_patch, origin_patch = self._allow_owner_same_origin()
        with owner_patch, origin_patch, mock.patch.object(frontend_app, "WereadClient", return_value=candidate), mock.patch.object(
            frontend_app, "_env_set"
        ) as env_set, mock.patch("subprocess.Popen") as popen:
            response = self.client.post("/api/config/weread-key", json={"key": "  " + VALID_KEY + "  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})
        candidate.call.assert_called_once_with("/shelf/sync")
        env_set.assert_called_once_with("WEREAD_API_KEY", VALID_KEY)
        self.assertEqual(popen.call_args.args[0], ["systemctl", "restart", "frontend"])
        self.assertNotIn("frontend-gw", str(popen.call_args))
        self.assertEqual(os.environ.get("WEREAD_API_KEY"), VALID_KEY)

    def test_business_error_payload_is_not_persisted(self):
        candidate = mock.Mock()
        candidate.call.return_value = {"errcode": 1001}
        owner_patch, origin_patch = self._allow_owner_same_origin()
        with owner_patch, origin_patch, mock.patch.object(frontend_app, "WereadClient", return_value=candidate), mock.patch.object(
            frontend_app, "_env_set"
        ) as env_set, mock.patch("subprocess.Popen") as popen:
            response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json(), {"ok": False, "code": "WEREAD_UNAVAILABLE"})
        env_set.assert_not_called()
        popen.assert_not_called()

    def test_response_and_save_exception_never_contain_candidate_key(self):
        candidate = mock.Mock()
        candidate.call.return_value = {"books": []}
        owner_patch, origin_patch = self._allow_owner_same_origin()
        with owner_patch, origin_patch, mock.patch.object(frontend_app, "WereadClient", return_value=candidate), mock.patch.object(
            frontend_app, "_env_set", side_effect=RuntimeError(VALID_KEY)
        ), mock.patch("subprocess.Popen") as popen:
            response = self.client.post("/api/config/weread-key", json={"key": VALID_KEY})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"ok": False, "code": "WEREAD_KEY_SAVE_FAILED"})
        self.assertNotIn(VALID_KEY, response.get_data(as_text=True))
        popen.assert_not_called()

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
        self.assertNotIn("MOMENTS_OWNER_TOKEN", source)

    def test_frontend_maps_auth_failures_to_safe_copy(self):
        source = READ_HTML.read_text(encoding="utf-8")
        self.assertIn("OWNER_AUTH_REQUIRED", source)
        self.assertIn("当前页面没有修改权限", source)
        self.assertIn("error.status = response.status", source)

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
