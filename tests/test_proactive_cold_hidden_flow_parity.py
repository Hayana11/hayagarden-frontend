"""Regression: proactive cold Wake keeps Chat's stable Hidden Flow carrier."""

from __future__ import annotations

import types
import unittest
from unittest import mock

from chat import daily_runtime as dr
from chat.hidden_flow.runtime import HiddenFlowTurnPlan


class ProactiveColdHiddenFlowParityTests(unittest.TestCase):
    def test_wake_gets_stable_carrier_without_volatile_flow_runtime(self):
        carrier = "<hidden_flow_system_carrier>stable</hidden_flow_system_carrier>"
        prepared = HiddenFlowTurnPlan(
            enabled=True,
            eligible=True,
            chat_id="default",
            user_message_id=0,
            private_request_block="<hidden_flow_runtime>volatile</hidden_flow_runtime>",
            global_guide_digest="digest",
            global_guide_source="runtime_config",
            global_guide_carrier="system_stable",
            global_guide_system_carrier=carrier,
            volatile_runtime_attached=True,
        )
        with (
            mock.patch.object(dr.config_store, "get_bool", return_value=True),
            mock.patch(
                "chat.hidden_flow.runtime.prepare_hidden_flow_turn",
                return_value=prepared,
            ),
        ):
            wake_hidden = dr._prepare_hidden_flow_plan(
                provider="claude_code",
                turn_kind="wake",
                chat_id="default",
                user_message_id=0,
                db_path="unused.db",
            )

        self.assertFalse(wake_hidden.enabled)
        self.assertFalse(wake_hidden.eligible)
        self.assertEqual(wake_hidden.private_request_block, "")
        self.assertFalse(wake_hidden.volatile_runtime_attached)
        self.assertEqual(wake_hidden.global_guide_system_carrier, carrier)

        wake_plan = types.SimpleNamespace(
            hidden_flow_enabled=False,
            hidden_flow_eligible=False,
            hidden_flow_plan=wake_hidden,
            manifest={},
        )
        chat_plan = types.SimpleNamespace(
            hidden_flow_enabled=True,
            hidden_flow_eligible=True,
            hidden_flow_plan=HiddenFlowTurnPlan(
                enabled=True,
                eligible=True,
                global_guide_digest="digest",
                global_guide_source="runtime_config",
                global_guide_carrier="system_stable",
                global_guide_system_carrier=carrier,
            ),
            manifest={},
        )
        wake_system = dr._effective_hidden_flow_static_system(wake_plan, "STATIC")
        chat_system = dr._effective_hidden_flow_static_system(chat_plan, "STATIC")

        self.assertEqual(wake_system, chat_system)
        self.assertEqual(wake_system, "STATIC\n\n" + carrier)
        self.assertFalse(dr._hidden_flow_runtime_enabled(wake_plan))


if __name__ == "__main__":
    unittest.main()
