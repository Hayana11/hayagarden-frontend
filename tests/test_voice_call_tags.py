"""Audio tags: he picks the delivery, only allowed tags reach the voice, none are shown."""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from voice_call import tags  # noqa: E402
from voice_call.brain import PHONE_STYLE  # noqa: E402
from voice_call.sentences import SentenceSplitter  # noqa: E402
from voice_call.tts import ElevenLabsTTS  # noqa: E402


class SpeakableTests(unittest.TestCase):
    def test_allowed_tags_reach_a_tag_capable_voice(self):
        self.assertEqual(tags.speakable('[whispers] 过来一点。', 'eleven_v4_turbo'), '[whispers] 过来一点。')
        self.assertEqual(tags.speakable('[ Laughs  Softly ] 才不是。', 'eleven_v3'), '[laughs softly] 才不是。')

    def test_unknown_tags_and_sound_effects_never_reach_the_voice(self):
        self.assertEqual(tags.speakable('[gunshot] 别怕 [applause]', 'eleven_v4_turbo'), '别怕')
        self.assertEqual(tags.speakable('[深情地] 晚安', 'eleven_v4'), '晚安')

    def test_a_voice_without_tag_support_gets_no_tags(self):
        self.assertEqual(tags.speakable('[whispers] 过来一点。', 'eleven_multilingual_v2'), '过来一点。')
        self.assertEqual(tags.speakable('[sighs] 好吧', ''), '好吧')

    def test_strip_and_tag_only(self):
        self.assertEqual(tags.strip_tags('[sighs] 好吧 [laughs]'), '好吧')
        self.assertTrue(tags.is_tag_only(' [laughs] [sighs] '))
        self.assertFalse(tags.is_tag_only('[laughs] 嗯'))
        self.assertFalse(tags.is_tag_only(''))

    def test_the_tts_request_carries_the_filtered_text(self):
        engine = ElevenLabsTTS(model_id='eleven_v4_turbo', api_key='k')
        _headers, body = engine._request('[whispers] 嗯 [gunshot]', None)
        self.assertEqual(json.loads(body)['text'], '[whispers] 嗯')
        _headers, body = engine._request('[whispers] 嗯', 'eleven_multilingual_v2')
        self.assertEqual(json.loads(body)['text'], '嗯')

    def test_the_prompt_only_suggests_allowed_tags(self):
        suggested = re.findall(r'\[([a-z ]+)\]', PHONE_STYLE)
        self.assertTrue(suggested)
        self.assertTrue(set(suggested) <= tags.ALLOWED, set(suggested) - tags.ALLOWED)


class OpeningToneTests(unittest.TestCase):
    """The first line of a call he places gets the same say in its tone as his replies."""

    def test_both_prompts_carry_the_same_tone_hint(self):
        from chat.behavior_authority_b3 import _CALL_OPENING_NOTE
        self.assertIn(tags.PROMPT_HINT, PHONE_STYLE)
        self.assertIn(tags.PROMPT_HINT, _CALL_OPENING_NOTE)
        self.assertTrue(set(tags.SUGGESTED) <= tags.ALLOWED)
        suggested = re.findall(r'\[([a-z ]+)\]', _CALL_OPENING_NOTE)
        self.assertEqual(tuple(suggested), tags.SUGGESTED)

    def test_a_tagged_opening_is_voiced_with_its_tag_and_shown_without(self):
        import os
        import tempfile
        from voice_call import store as call_store
        from voice_call.service import CallService
        from chat.wake_call import validate_opening

        opening = validate_opening(json.dumps({'rendered_content': '[softly] 在吗？'}))
        self.assertEqual(opening, '[softly] 在吗？', 'the validator keeps tags')
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        sent = []

        class Engine:
            voice_id = 'v'
            model_id = 'eleven_v4_turbo'

        class TTS:
            def warm(self, text, voice_id=None):
                sent.append(text)

        service = CallService(call_store.CallStore(path, lambda: 1_800_000_000.0), TTS(), brain=None,
                              spawn=lambda fn: fn())
        call = service.ring_from_him(opening)
        service.answer(call['id'])
        lines, _ = service.lines_after(call['id'], 0)
        self.assertEqual([(l['speaker'], l['text']) for l in lines], [('him', '[softly] 在吗？')])
        self.assertEqual(sent, ['[softly] 在吗？'], 'answering warms the voice with the tagged opening')
        _headers, body = ElevenLabsTTS(model_id='eleven_v4_turbo', api_key='k')._request(lines[0]['text'], None)
        self.assertEqual(json.loads(body)['text'], '[softly] 在吗？', 'the voice performs it')
        self.assertEqual(tags.strip_tags(lines[0]['text']), '在吗？', 'what the page shows')


class CacheKeyTests(unittest.TestCase):
    def test_lines_differing_only_in_dropped_tags_share_one_synthesis(self):
        import tempfile
        from voice_call.tts import CachedTTS

        class Engine:
            voice_id = 'v'
            model_id = 'eleven_v4_turbo'

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = CachedTTS(Engine(), tmp.name)
        self.assertEqual(cache.key('[gunshot] 晚安'), cache.key('晚安'), 'a dropped tag is not part of what is voiced')
        self.assertNotEqual(cache.key('[whispers] 晚安'), cache.key('晚安'), 'a kept tag changes the voice')
        self.assertEqual(cache.key('[whispers] 晚安', model_id='eleven_multilingual_v2'),
                         cache.key('晚安', model_id='eleven_multilingual_v2'))


class SplitterTagTests(unittest.TestCase):
    def test_a_tag_on_its_own_line_joins_the_next_line(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed('[laughs]\n'), [])
        self.assertEqual(splitter.feed('才不是。\n'), ['[laughs] 才不是。'])

    def test_a_trailing_tag_with_no_words_is_dropped(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed('晚安。\n[sighs]'), ['晚安。'])
        self.assertEqual(splitter.flush(), [])
        self.assertEqual(splitter.feed('早。\n'), ['早。'], 'a dropped tag never leaks into the next reply')

    def test_tags_do_not_count_toward_the_first_line_cut(self):
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed('[whispers] [softly] 嗯，'), [], 'only three words so far')
        splitter = SentenceSplitter()
        self.assertEqual(splitter.feed('[whispers] 嗯嗯嗯嗯，好'), [],
                         'six words: under FIRST_LIMIT even though the raw text is longer')

    def test_the_first_cut_never_lands_inside_a_tag(self):
        splitter = SentenceSplitter()
        out = splitter.feed('[softly, slowly] 宝宝你刚才说的那件事，我想了很久')
        self.assertEqual(out, ['[softly, slowly] 宝宝你刚才说的那件事，'])

    def test_the_soft_limit_cut_never_lands_inside_a_tag(self):
        splitter = SentenceSplitter(soft_limit=20, first_limit=100)
        splitter.feed('引子。\n')
        out = splitter.feed('一二三四五六七八九十。[is it? yes] 再来一句很长很长很长很长')
        self.assertEqual(out, ['一二三四五六七八九十。'])

    def test_tags_do_not_count_toward_the_soft_limit(self):
        splitter = SentenceSplitter(soft_limit=20, first_limit=100)
        splitter.feed('引子。\n')
        self.assertEqual(splitter.feed('[whispers] [softly] [teasing] 一二三四五。六七八九十'), [],
                         'ten words: under the soft limit however many tags')


if __name__ == '__main__':
    unittest.main()
