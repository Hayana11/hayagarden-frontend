"""Audio tags: he chooses how a line is said, the voice performs it.

On a call he may put a few square-bracket delivery tags in a line
("[whispers] 过来一点。", "[laughs] 才不是。"). Eleven v3/v4 models act them
out. Only the tags listed here reach the voice: an unknown tag could be read
aloud or played as a sound effect, so it is dropped, and a model without tag
support gets none. The screen never shows tags (the page strips them too).
"""
import re

# Delivery only: no sound effects. Lowercase, as the model is asked to write them.
ALLOWED = frozenset((
    'whispers', 'softly', 'gently', 'warmly', 'tenderly', 'quietly',
    'laughs', 'laughs softly', 'chuckles', 'giggles',
    'sighs', 'exhales', 'yawns', 'sleepy',
    'curious', 'teasing', 'playfully', 'mischievously', 'sarcastic',
    'excited', 'happy', 'sad', 'pouting', 'serious', 'hesitant',
))
# The tags the prompts suggest (both his call replies and the opening of a call
# he places use this one hint, so they cannot drift apart). All are in ALLOWED.
SUGGESTED = ('whispers', 'softly', 'laughs', 'chuckles', 'sighs', 'teasing', 'sleepy')
PROMPT_HINT = (
    '语气由你自己定：想轻一点、笑出来、叹口气的时候，在那句话前面加一个英文方括号标签，'
    '比如 ' + ' '.join('[%s]' % tag for tag in SUGGESTED) + '；只在真的需要时用，不必每句都有。'
)
_TAG = re.compile(r'\[([^\[\]\n]{1,40})\]')
_SPACES = re.compile(r'[ \t]{2,}')


def _norm(tag):
    return ' '.join(tag.lower().split())


def supports_tags(model_id):
    """Eleven v3 and v4 models perform audio tags; older ones would read them out."""
    model = (model_id or '').strip().lower()
    return model.startswith('eleven_v3') or model.startswith('eleven_v4')


def speakable(text, model_id):
    """``text`` as the voice should get it: allowed tags kept (when the model takes tags), the rest removed."""
    keep = supports_tags(model_id)

    def repl(match):
        tag = _norm(match.group(1))
        return '[%s]' % tag if keep and tag in ALLOWED else ''

    return _SPACES.sub(' ', _TAG.sub(repl, text or '')).strip()


_BASE_TAG_OK = re.compile(r"^[a-z][a-z ,'-]{0,39}$")


def clean_base_tag(raw):
    """A configured base delivery ("speaking slowly, in a low calm voice") or None.
    Operator config, so not limited to ALLOWED, but only plain lowercase words."""
    tag = _norm(str(raw or '').strip().strip('[]'))
    return tag if _BASE_TAG_OK.match(tag) else None


def voice_text(text, model_id, base_tag=None):
    """What the voice is sent: ``speakable`` text, led by the base delivery tag
    when the model performs tags (every line, before his own tags)."""
    spoken = speakable(text, model_id)
    if spoken and base_tag and supports_tags(model_id):
        return '[%s] %s' % (base_tag, spoken)
    return spoken


def strip_tags(text):
    """``text`` without any bracket tags: what is shown, and what counts as words."""
    return _SPACES.sub(' ', _TAG.sub('', text or '')).strip()


def is_tag_only(text):
    return bool((text or '').strip()) and not strip_tags(text)


def outside_tags(text, index):
    """Whether position ``index`` of ``text`` is outside every [...] tag."""
    depth = 0
    for ch in text[:index]:
        if ch == '[':
            depth += 1
        elif ch == ']' and depth:
            depth -= 1
    return depth == 0
