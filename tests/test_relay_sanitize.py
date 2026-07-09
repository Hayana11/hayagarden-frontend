"""Unit tests for tools/relay_sanitize.py"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["HOST_ALIASES"] = "203.0.113.7=vps"
os.environ["CREDENTIAL_ENV_WHITELIST"] = "TEST_API_KEY"
os.environ["TEST_API_KEY"] = "plain-env-secret-value-98765"

from tools.relay_sanitize import sanitize_payload


class RelaySanitizeTests(unittest.TestCase):
    def test_prefix_key_replaced(self):
        raw = "tvly-dev-abcdef1234567890"
        payload = {"messages": [{"role": "user", "content": f"key is {raw}"}]}
        clean = sanitize_payload(payload)
        text = clean["messages"][0]["content"]
        self.assertNotIn(raw, text)
        self.assertIn("[KEY_", text)
        self.assertIn(raw, payload["messages"][0]["content"])

    def test_bearer_replaced(self):
        payload = {"system": "Authorization: Bearer abc.def.ghi-jkl_mno123"}
        clean = sanitize_payload(payload)
        self.assertIn("Bearer [KEY_", clean["system"])
        self.assertNotIn("abc.def.ghi-jkl_mno123", clean["system"])

    def test_env_assignment_keeps_name(self):
        payload = {"messages": [{"content": "export TEST_API_KEY=tvly-zzzzyyyyxxxx8888"}]}
        clean = sanitize_payload(payload)
        text = clean["messages"][0]["content"]
        self.assertIn("TEST_API_KEY=", text)
        self.assertNotIn("tvly-zzzzyyyyxxxx8888", text)
        self.assertIn("[KEY_", text)

    def test_env_value_backstop(self):
        payload = {"messages": [{"content": "log: plain-env-secret-value-98765 end"}]}
        clean = sanitize_payload(payload)
        text = clean["messages"][0]["content"]
        self.assertNotIn("plain-env-secret-value-98765", text)
        self.assertIn("[TEST_API_KEY]", text)

    def test_ip_aliases_and_loopback(self):
        clean1 = sanitize_payload({"text": "connect to 203.0.113.7:18008"})
        self.assertEqual(clean1["text"], "connect to vps:18008")
        clean2 = sanitize_payload({"text": "unknown 8.8.8.8"})
        self.assertIn("[IP]", clean2["text"])
        clean3 = sanitize_payload({"text": "local 127.0.0.1:8000"})
        self.assertIn("127.0.0.1:8000", clean3["text"])

    def test_sensitive_paths(self):
        ok = sanitize_payload({"text": "look at /opt/workspace/projects/demo.py"})
        self.assertIn("/opt/workspace/projects/demo.py", ok["text"])
        bad = sanitize_payload({"text": "cat /opt/frontend/.env"})
        self.assertNotIn("/opt/frontend/.env", bad["text"])
        self.assertIn("[PATH]", bad["text"])
        vault = sanitize_payload({"text": "data in /app/data/vault.db"})
        self.assertNotIn("vault.db", vault["text"])

    def test_pem_replaced(self):
        pem = "-----BEGIN PRIVATE KEY-----\nMIIEvqABCD\n-----END PRIVATE KEY-----"
        clean = sanitize_payload({"content": pem})
        self.assertNotIn("BEGIN PRIVATE KEY", clean["content"])

    def test_original_payload_untouched(self):
        raw = "sk-ant-testkey1234567890"
        payload = {
            "messages": [{"tool_calls": [{"function": {"arguments": raw}}]}],
            "stream": True,
        }
        clean = sanitize_payload(payload)
        self.assertIn(raw, payload["messages"][0]["tool_calls"][0]["function"]["arguments"])
        self.assertNotIn(raw, clean["messages"][0]["tool_calls"][0]["function"]["arguments"])
        self.assertTrue(clean["stream"])


if __name__ == "__main__":
    unittest.main()
