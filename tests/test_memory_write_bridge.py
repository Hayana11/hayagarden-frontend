from __future__ import annotations
import sqlite3
import tempfile
import unittest
from pathlib import Path
from tools.capability_manifest import get_capability
from tools.execution_fence import capability_for_tool, evaluate_tool_call
from tools.lease_signer import issue_turn_lease
from tools.memory_write_adapter import MAX_CONTENT_SIZE, write_memory

def make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, content TEXT NOT NULL, author TEXT, layer TEXT, tags TEXT DEFAULT '', importance INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0)")
    conn.commit()
    conn.close()

class MemoryWriteBridgeTests(unittest.TestCase):
    def test_storage_is_fixed_and_single_write(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "memory.db")
            make_db(path)
            result = write_memory(path, content="  记住这条稳定事实  ")
            self.assertEqual(result["status"], "CREATED")
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT type,content,author,layer,tags,importance,pinned FROM posts").fetchall(), [("MEMORY","记住这条稳定事实","fyodor","long-term","",0,0)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)
            conn.close()

    def test_input_boundaries_reject_without_write(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "memory.db")
            make_db(path)
            for content in ("", "   ", "x" * (MAX_CONTENT_SIZE + 1), None, 123, 'emoji 😀\ncore long-term MEMORY {"x":1}'):
                if isinstance(content, str) and content.startswith("emoji"):
                    result = write_memory(path, content=content)
                    self.assertEqual(result["status"], "CREATED")
                else:
                    result = write_memory(path, content=content)
                    self.assertIn(result["status"], {"INVALID_CONTENT","CONTENT_TOO_LONG"})
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)
            conn.close()

    def test_fence_chat_and_wake(self):
        chat = issue_turn_lease(turn_id="chat-1", turn_mode="chat", issued_from="default_policy")
        wake = issue_turn_lease(turn_id="wake-1", turn_mode="wake", issued_from="default_policy")
        for tool_name in ("mcp__internal__write_memory", "memory_write"):
            self.assertEqual(evaluate_tool_call(tool_name, {"content":"x"}, chat)["lease_decision"], "ALLOW")
            self.assertEqual(evaluate_tool_call(tool_name, {"content":"x"}, wake)["lease_decision"], "DENIED_CAPABILITY")

    def test_model_surface_has_no_legacy_save_memory(self):
        root = Path(__file__).resolve().parents[1]
        gateway = (root / "gateway.py").read_text(encoding="utf-8")
        tools_block = gateway[gateway.index("TOOLS = ["):gateway.index("def get_tools")]
        self.assertIn("'name': 'memory_write'", tools_block)
        self.assertNotIn("'name': 'memory.write'", tools_block)
        self.assertIn("'name': 'search_memories'", tools_block)
        self.assertNotIn("'name': 'save_memory'", tools_block)
        drawers = (root / "tool_drawers.py").read_text(encoding="utf-8")
        self.assertIn("'tools': ['memory.write', 'search_memories']", drawers)

    def test_api_relay_binding_uses_anthropic_safe_physical_name(self):
        binding = get_capability("memory.write")["provider_bindings"]["api_relay"]
        self.assertEqual(binding, "memory_write")
        self.assertRegex(binding, r"^[A-Za-z0-9_-]{1,64}$")
        self.assertEqual(capability_for_tool(binding), "memory.write")

    def test_schema_is_content_only(self):
        from tools.cc_tool_surface import _INTERNAL_TOOL_SCHEMAS
        schema = _INTERNAL_TOOL_SCHEMAS["mcp__internal__write_memory"]
        self.assertEqual(set(schema["properties"]), {"content"})
        self.assertEqual(schema["required"], ["content"])
        self.assertEqual(schema["properties"]["content"]["maxLength"], MAX_CONTENT_SIZE)

if __name__ == "__main__":
    unittest.main()
