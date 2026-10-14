"""Focused provider-budget accounting replay tests.

The three redacted usage rows are deterministic projections of the existing
production Claude JSONL assistant rows. They intentionally contain no prompt
content or credentials; prompt assembly is tested through the shared
cc_usage_observability pure functions.
"""
from __future__ import annotations

import json
import unittest

from cc_resident import summarize_rounds
from tools import cc_usage_observability as obs
from tools.cc_jsonl_usage import replay_jsonl_lines


_GENERATION_ROWS = (
    (35, 41338, 41608, 82948),
    (46, 0, 101487, 101489),
    (48, 43104, 66412, 109518),
)


def _assistant_jsonl(request_id: str, cache_read: int, cache_creation: int) -> str:
    return json.dumps({
        "type": "assistant",
        "requestId": request_id,
        "timestamp": "2026-10-14T00:00:00.000Z",
        "message": {
            "model": "claude-opus-5-5",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 1,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": cache_creation,
                },
            },
        },
    })


class ProviderBudgetReplayTests(unittest.TestCase):
    def test_gen35_gen46_gen48_first_round_context_is_deterministic(self):
        for generation, cache_read, cache_creation, expected_context in _GENERATION_ROWS:
            result = replay_jsonl_lines([
                _assistant_jsonl(
                    "req-generation-%s" % generation,
                    cache_read,
                    cache_creation,
                ),
            ])
            self.assertEqual(result["request_count"], 1)
            record = result["records"][0]
            context = (
                record["input_tokens"]
                + record["cache_read"]
                + record["cache_creation"]
            )
            self.assertEqual(context, expected_context)
            self.assertEqual(
                record["cache_read"] + record["cache_creation"],
                expected_context - 2,
            )

    def test_visible_payload_and_components_are_not_double_counted(self):
        system = "STATIC_SYSTEM"
        canonical = "CANONICAL_HISTORY"
        user = "CURRENT_USER"
        content = canonical + "\n" + user
        tool_schema = '{"name":"tool","input_schema":{}}'
        breakdown = obs.build_context_breakdown(
            full_system=system,
            history_bootstrap_text=canonical,
            user_text=user,
            final_content=content,
            tool_schema_text=tool_schema,
            tool_schema_source="static_registry",
            is_cold=True,
        )
        usage = summarize_rounds([{
            "index": 1,
            "complete": True,
            "input_tokens": 2,
            "output_tokens": 1,
            "cache_read": 0,
            "cache_creation": 1000,
            "context_tokens": 1002,
        }])
        finalized = obs.finalize_breakdown_with_usage(
            breakdown,
            usage,
            is_cold=True,
        )
        expected_known = (
            obs.estimate_tokens_heuristic_cjk1_ascii4_v1(system)
            + obs.estimate_tokens_heuristic_cjk1_ascii4_v1(content)
        )
        self.assertEqual(
            finalized["known_visible_context_tokens_estimate"],
            expected_known,
        )
        self.assertEqual(
            finalized["unattributed_bootstrap_tokens_estimate"],
            max(
                0,
                1002
                - expected_known
                - finalized["tool_schema_tokens_estimate"],
            ),
        )
        self.assertLess(
            finalized["known_visible_context_tokens_estimate"],
            expected_known
            + obs.estimate_tokens_heuristic_cjk1_ascii4_v1(user),
        )
        self.assertEqual(
            finalized["provider_first_round_context_tokens"],
            1002,
        )
        self.assertEqual(
            finalized["tool_schema_measurement_status"],
            "available",
        )


if __name__ == "__main__":
    unittest.main()
