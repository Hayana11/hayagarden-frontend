"""Regression coverage for Hidden Flow private-content recovery boundaries."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import chat.daily_runtime as daily_runtime
from chat.hidden_flow.stream_filter import HiddenFlowStreamFilter


_PRIVATE_MARKERS = (
    "HF_PRIVATE_SENTINEL_7F31A9",
    "HF_POOL_SECRET_83C2D1",
    "HF_GUIDANCE_SECRET_B91E44",
    "<hidden_flow_",
    "<available_hidden_flows",
)


def assert_no_hidden_flow_private_content(testcase: unittest.TestCase, payload) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, default=str)
    for marker in _PRIVATE_MARKERS:
        testcase.assertNotIn(marker, rendered)


class HiddenFlowPrivateLeakageRecoveryTests(unittest.TestCase):
    def test_interrupted_recovery_persists_only_filtered_visible_projection(self) -> None:
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            plan = SimpleNamespace(
                chat_id="default",
                context_id=1,
                context_epoch=1,
                resident_generation=1,
                lease_owner="owner",
                db_path=db_path,
                manifest={},
            )
            raw_chunks = (
                "visible response ",
                "<hidden_flow_guidance>HF_PRIVATE_SENTINEL_7F31A9 "
                "HF_POOL_SECRET_83C2D1 HF_GUIDANCE_SECRET_B91E44"
                "</hidden_flow_guidance>tail ",
                '<hidden_flow_control flow="demo-flow" action="advance"',
            )
            stream_filter = HiddenFlowStreamFilter()
            rescued = "".join(stream_filter.feed(chunk) for chunk in raw_chunks)
            rescued += stream_filter.finish()

            captured = {}

            def fake_persist(**kwargs):
                captured.update(kwargs)
                return 101

            with mock.patch.object(
                daily_runtime.dc,
                "persist_daily_assistant_if_current",
                side_effect=fake_persist,
            ):
                assistant_id = daily_runtime.persist_partial_daily_stream_rescue(
                    plan,
                    content=rescued,
                    thinking=rescued,
                )

            self.assertEqual(assistant_id, 101)
            self.assertEqual(captured["content"], "visible response tail ")
            self.assertEqual(captured["thinking"], "visible response tail ")
            assert_no_hidden_flow_private_content(self, captured)
        finally:
            os.unlink(db_path)
