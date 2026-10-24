"""Bilingual calls: he speaks one language, she reads a Chinese gloss."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.test_voice_call import Clock, FakeEngine, FakeSTT, run_now  # noqa: E402
from voice_call import brain  # noqa: E402
from voice_call import languages as call_languages  # noqa: E402
from voice_call import store as call_store  # noqa: E402
from voice_call.pairs import PairParser  # noqa: E402
from voice_call.service import CallService  # noqa: E402
from voice_call.stt import ElevenLabsSTT  # noqa: E402
from voice_call.tts import CachedTTS  # noqa: E402

REPLY = (
    'EN: [softly] Wake up, little cat.\n'
    'ZH: [softly] 醒醒，小猫。\n'
    'EN: It is half past seven.\n'
    'ZH: 七点半了。\n'
)
PAIRS = [('[softly] Wake up, little cat.', '醒醒，小猫。'), ('It is half past seven.', '七点半了。')]


def parse(chunks, marker='EN'):
    parser = PairParser(marker, 'ZH')
    out = []
    for chunk in chunks:
        out.extend(parser.feed(chunk))
    return out + parser.flush()


class PairParserTests(unittest.TestCase):
    def test_a_cut_anywhere_gives_the_same_pairs(self):
        for cut in range(1, len(REPLY)):
            self.assertEqual(parse([REPLY[:cut], REPLY[cut:]]), PAIRS, 'cut at %d: %r' % (cut, REPLY[:cut]))
        self.assertEqual(parse(list(REPLY)), PAIRS, 'one character at a time')

    def test_a_pair_is_published_only_once_complete_and_never_revised(self):
        parser = PairParser('EN', 'ZH')
        self.assertEqual(parser.feed('EN: Wake up, little cat.\n'), [], 'no gloss yet')
        self.assertEqual(parser.feed('ZH: 醒醒'), [], 'gloss line not finished')
        self.assertEqual(parser.feed('，小猫。\n'), [('Wake up, little cat.', '醒醒，小猫。')])
        self.assertEqual(parser.feed('E'), [], 'half a marker')
        self.assertEqual(parser.feed('N: It is half past seven.\nZH: 七点半了。'), [])
        self.assertEqual(parser.flush(), [('It is half past seven.', '七点半了。')], 'last line needs no newline')
        self.assertEqual(parser.flush(), [], 'flush releases once')

    def test_tags_stay_on_the_spoken_line_and_never_reach_the_gloss(self):
        spoken, gloss = parse([REPLY])[0]
        self.assertIn('[softly]', spoken)
        self.assertNotIn('[', gloss)

    def test_departures_degrade_instead_of_silencing_the_call(self):
        self.assertEqual(parse(['EN: Hi.\nEN: Still there?\nZH: 还在吗？\n']),
                         [('Hi.', None), ('Still there?', '还在吗？')], 'a missing gloss')
        self.assertEqual(parse(['ZH: 孤零零的释义\nEN: Hi.\n']), [('Hi.', None)],
                         'a gloss with no spoken line is never voiced')
        self.assertEqual(parse(['Hello there.\n']), [('Hello there.', None)], 'an unmarked line is spoken')
        self.assertEqual(parse(['EN: Hi.\n']), [('Hi.', None)], 'the reply ended before the gloss')

    def test_marker_spelling_variants(self):
        self.assertEqual(parse(['en：Hi.\nzh：你好。\n']), [('Hi.', '你好。')], 'case and full-width colon')
        self.assertEqual(parse(['**EN:** Hi.\n**ZH:** 你好。\n']), [('Hi.', '你好。')], 'bold markers')
        self.assertEqual(parse(['JA: もしもし。\nZH: 喂。\n'], marker='JA'), [('もしもし。', '喂。')])
        self.assertEqual(parse(['ENGLISH: Hi.\n']), [('ENGLISH: Hi.', None)], 'only the exact mark counts')


class LanguageConfigTests(unittest.TestCase):
    def load(self, config):
        return call_languages.load(lambda key: json.dumps(config))

    def test_subtitle_marker_and_auto_transcription(self):
        langs = self.load({
            'en': {'label': 'English', 'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'auto', 'subtitle': 'zh'},
            'ru': {'label': 'Русский', 'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'rus', 'subtitle': 'zh'},
            'ja': {'label': '日本語', 'voice_id': '0zi0ae2RvBmXaahlPt2n', 'stt': 'jpn'},
        })
        self.assertEqual((langs['en']['stt'], langs['en']['subtitle'], langs['en']['marker']), ('auto', 'zh', 'EN'))
        self.assertEqual(langs['ru']['marker'], 'RU')
        self.assertIsNone(langs['ja']['subtitle'], 'no subtitle: a one-language call as today')
        self.assertIsNone(langs['ja']['marker'])
        snap = call_languages.snapshot(langs['en'])
        self.assertEqual((snap['subtitle'], snap['marker'], snap['stt']), ('zh', 'EN', 'auto'),
                         'the call freezes its subtitle setting with the rest')

    def test_bad_subtitle_settings_fall_back_to_a_one_language_call(self):
        langs = self.load({
            'zh': {'label': '中文', 'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'zho', 'subtitle': 'zh'},
            'en': {'label': 'English', 'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'eng', 'subtitle': 'Chinese!'},
        })
        self.assertIsNone(langs['zh']['subtitle'], 'spoken and gloss marks must differ')
        self.assertIsNone(langs['en']['subtitle'])
        self.assertEqual(self.load({'x': {'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'automatic'}}), {})


class AutoTranscriptionTests(unittest.TestCase):
    def fields(self, language):
        seen = {}
        stt = ElevenLabsSTT(api_key='k', post=lambda url, headers, body, timeout: (
            seen.update(body=body) or (200, b'{"text": "ok"}')))
        stt.transcribe(b'RIFF', 'audio/wav', language)
        return seen['body'].decode('latin-1')

    def test_auto_lets_scribe_detect_the_language(self):
        self.assertNotIn('name="language_code"', self.fields('auto'))
        self.assertIn('name="language_code"\r\n\r\nrus', self.fields('rus'))
        self.assertIn('name="language_code"\r\n\r\nzho', self.fields('bogus'), 'anything else: the default')


class PromptTests(unittest.TestCase):
    def test_a_bilingual_call_asks_for_marked_pairs_every_turn(self):
        first = brain.format_utterance('早', True, 'English', pairs=('EN', 'ZH'))
        self.assertIn('EN: [softly] Take your time.\nZH: 慢慢来。', first)
        self.assertIn('只给她看，不会念出来', first)
        later = brain.format_utterance('嗯', False, 'English', pairs=('EN', 'ZH'))
        self.assertIn('每句写 EN: 和 ZH: 两行', later, 'the chat history around a call is Chinese')
        self.assertEqual(brain.format_utterance('嗯', False, 'English'),
                         '〔电话〕她刚在电话里说：「嗯」（用English回答）', 'one-language calls are unchanged')


class PairBrain:
    def __init__(self, chunks):
        self.chunks = chunks
        self.pairs = []

    def reply(self, text, first_turn, language=None, opening=None, pairs=None):
        self.pairs.append(pairs)
        yield from self.chunks


class BilingualCallTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine()
        self.config = json.dumps({'en': {'label': 'English', 'voice_id': 'K7JxslCTelez3qaKj5SJ',
                                         'stt': 'auto', 'subtitle': 'zh'}})
        self.brain = PairBrain([REPLY[:23], REPLY[23:57], REPLY[57:]])
        self.stt = FakeSTT(text='早上好')
        self.service = CallService(
            call_store.CallStore(self.path, Clock()), CachedTTS(self.engine, self.tmp.name, spawn=run_now),
            self.brain, clock=Clock(), spawn=run_now, stt=self.stt,
            languages=lambda: call_languages.load(lambda key: self.config),
        )

    def tearDown(self):
        os.unlink(self.path)
        self.tmp.cleanup()

    def test_only_the_spoken_line_is_voiced_and_the_gloss_rides_with_it(self):
        call = self.service.ring('en')
        self.service.answer(call['id'])
        self.service.hear(call['id'], b'RIFF', 'audio/wav', 'zho', 1)
        self.assertEqual(self.stt.calls[-1][2], 'auto', 'she may speak any language')
        self.assertEqual(self.brain.pairs, [('EN', 'ZH')])
        lines, _ = self.service.lines_after(call['id'], 0)
        his = [line for line in lines if line['speaker'] == 'him']
        self.assertEqual([(line['text'], line['gloss']) for line in his], PAIRS)
        self.assertNotIn('gloss', [line for line in lines if line['speaker'] == 'her'][0])
        for line in his:
            self.service.audio(call['id'], line['seq'])
        voiced = ' '.join(self.engine.calls)
        self.assertIn('Wake up, little cat.', voiced)
        for gloss in ('醒醒', '七点半'):
            self.assertNotIn(gloss, voiced, 'the gloss never reaches the voice')

    def test_a_config_edit_mid_call_does_not_change_the_call(self):
        call = self.service.ring('en')
        self.service.answer(call['id'])
        self.config = json.dumps({'en': {'label': 'English', 'voice_id': 'K7JxslCTelez3qaKj5SJ', 'stt': 'eng'}})
        self.service.hear(call['id'], b'RIFF', 'audio/wav', 'zho', 1)
        self.assertEqual(self.brain.pairs, [('EN', 'ZH')], 'subtitles stay on for the call already talking')
        self.assertEqual(self.stt.calls[-1][2], 'auto')


if __name__ == '__main__':
    unittest.main()
