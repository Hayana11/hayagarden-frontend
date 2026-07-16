import unittest

import moments_intent


class MomentsIntentTests(unittest.TestCase):
    def tearDown(self):
        moments_intent.clear_pending('hayana-chat')
        moments_intent.clear_pending('other')

    def test_set_and_pop_pending(self):
        moments_intent.set_pending('hayana-chat', previous_turns=1, caption='留一段')
        pending = moments_intent.pop_pending('hayana-chat')
        self.assertIsNotNone(pending)
        self.assertEqual(pending.previous_turns, 1)
        self.assertEqual(pending.caption, '留一段')
        self.assertIsNone(moments_intent.pop_pending('hayana-chat'))

    def test_clear_pending(self):
        moments_intent.set_pending('hayana-chat', previous_turns=0, caption='')
        moments_intent.clear_pending('hayana-chat')
        self.assertIsNone(moments_intent.pop_pending('hayana-chat'))

    def test_previous_turns_bounds(self):
        with self.assertRaises(ValueError):
            moments_intent.set_pending('hayana-chat', previous_turns=-1, caption='')
        with self.assertRaises(ValueError):
            moments_intent.set_pending('hayana-chat', previous_turns=3, caption='')

    def test_caption_length_limit(self):
        with self.assertRaises(ValueError):
            moments_intent.set_pending('hayana-chat', previous_turns=0, caption='x' * 501)


if __name__ == '__main__':
    unittest.main()
