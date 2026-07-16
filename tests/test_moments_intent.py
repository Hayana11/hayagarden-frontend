import os
import sqlite3
import tempfile
import unittest

import moments_intent
import moments_turn


class MomentsIntentTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        moments_turn.ensure_turn_schema(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_set_and_pop_pending(self):
        moments_intent.set_pending(self.db_path, 'turn-a', previous_turns=1, caption='留一段')
        pending = moments_intent.pop_pending(self.db_path, 'turn-a')
        self.assertIsNotNone(pending)
        self.assertEqual(pending.previous_turns, 1)
        self.assertEqual(pending.caption, '留一段')
        self.assertIsNone(moments_intent.pop_pending(self.db_path, 'turn-a'))

    def test_clear_pending(self):
        moments_intent.set_pending(self.db_path, 'turn-a', previous_turns=0, caption='')
        moments_intent.clear_pending(self.db_path, 'turn-a')
        self.assertIsNone(moments_intent.pop_pending(self.db_path, 'turn-a'))

    def test_previous_turns_bounds(self):
        with self.assertRaises(ValueError):
            moments_intent.set_pending(self.db_path, 'turn-a', previous_turns=-1, caption='')
        with self.assertRaises(ValueError):
            moments_intent.set_pending(self.db_path, 'turn-a', previous_turns=3, caption='')

    def test_caption_length_limit(self):
        with self.assertRaises(ValueError):
            moments_intent.set_pending(self.db_path, 'turn-a', previous_turns=0, caption='x' * 501)


if __name__ == '__main__':
    unittest.main()
