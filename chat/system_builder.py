"""
chat/system_builder.py â€” Prompt Builderï¼ˆRequest Pipeline ç¬¬ä¸€çŽ¯ï¼‰

ä»Ž gateway.py æ¬è¿‡æ¥çš„ build_system()ï¼šæŠŠäººè®¾ã€è®°å¿†ã€å½“å‰çŠ¶æ€ã€é©±åŠ¨æ¡ã€
ç•™è¨€æ¿å¾…åŠžç­‰åå‡ ä¸ªæ•°æ®æºæ‹¼æˆæœ€ç»ˆçš„ system promptã€‚normal æ¨¡å¼è¿”å›ž
cache-control blocks åˆ—è¡¨ï¼ˆçœ tokenï¼‰ã€‚åœ¨çº¿ wake relay ç›´æŽ¥ä¿ç•™ blocksï¼›
build_wake_system() ä»…ä¿ç•™ç»™éœ€è¦çº¯å­—ç¬¦ä¸²çš„æ—§è°ƒç”¨æ–¹ä¸Žå¥åº·æ£€æŸ¥ã€‚

ä¸ç›´æŽ¥ import gatewayï¼ˆä¼šå¾ªçŽ¯ä¾èµ–ï¼‰ï¼Œéœ€è¦ get_db() ç­‰åŸºç¡€è®¾æ–½æ—¶åœ¨
å‡½æ•°ä½“å†…å»¶è¿Ÿ importã€‚
"""
import os
import json
import datetime
import urllib.request
import config_store

from chat.context_contract import SharedContext
from chat.relationship_context import build_relationship_context

NL = chr(10)


def read_persona():
    """Read production persona from runtime authority (not the Git seed)."""
    from chat.persona_store import read_persona as _read_runtime_persona
    return _read_runtime_persona().strip()


def build_shared_context_details(
    *,
    persona=None,
    get_db_fn=None,
):
    """Build A1 provider-neutral context plus internal refresh metadata."""
    if get_db_fn is None:
        from gateway import get_db as get_db_fn  # å»¶è¿Ÿ importï¼Œé¿å…å¾ªçŽ¯ä¾èµ–

    persona_text = read_persona() if persona is None else str(persona)
    relationship = build_relationship_context(get_db_fn)
    shared = SharedContext(
        persona=persona_text,
        relationship_context=relationship.text,
        relationship_fingerprint=relationship.fingerprint,
    )
    return shared, relationship


def build_shared_context(**kwargs):
    """Public A1 contract. Both providers receive this exact object shape."""
    return build_shared_context_details(**kwargs)[0]


def _ombre_handoff_sync():
    """Compatibility wrapper around the single Ombre integration boundary."""
    from tools.ombre_adapter import get_handoff
    return get_handoff(timeout=3.0, wall_timeout=5.0)


def build_system(
    wake=False,
    split_dynamic=False,
    include_relationship_context=None,
    allow_side_effects=True,
    capability_profile=None,
):
    """Assemble system prompt blocks.

    allow_side_effects:
      True  â†’ normal path (may consume dream_pool, drain one-shot feedback, â€¦)
      False â†’ inspect_only / dry_run: read-only assembly, no DB writes / no
              random dream surfacing

    capability_profile:
      None / 'relay' / 'relay_wake' â†’ generic Relay tool brochure (current text)
      'cc_wake' â†’ WakeÂ·Claude Code tool surface only (see wake.cc_tools)
      'wake_dry_run' / 'cc_wake_dry_run' / 'relay_wake_dry_run'
        â†’ dry-run brochure: no tools at all (do not inject normal tool lists)
    """
    from gateway import get_db  # å»¶è¿Ÿ importï¼Œæ‰“ç ´å¾ªçŽ¯ä¾èµ–ï¼ˆbuild_system è¢«è°ƒç”¨æ—¶ gateway æ—©å·²åŠ è½½å®Œæ¯•ï¼‰

    profile = str(capability_profile or ('relay_wake' if wake else 'relay')).strip().lower()
    if profile in ('', 'default', 'api_relay'):
        profile = 'relay_wake' if wake else 'relay'
    is_dry_run_profile = profile in (
        'wake_dry_run', 'cc_wake_dry_run', 'relay_wake_dry_run', 'dry_run',
    )

    # â”€â”€ BP1 Â· Personaï¼ˆæ°¸ä¸å˜ï¼Œç¼“å­˜æ–­ç‚¹1ï¼‰â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # include_relationship_context:
    #   None  â†’ æ™®é€šèŠå¤©é»˜è®¤å¼€ï¼ˆå— RELATIONSHIP_CONTEXT_ENABLEDï¼‰ï¼›wake é»˜è®¤å…³ï¼Œç”±è°ƒç”¨æ–¹æ˜¾å¼æ‰“å¼€
    #   True  â†’ wake(normal/nightwatch/ritual) å¯å¼€ï¼Œå¦å— WAKE_RELATIONSHIP_CONTEXT_ENABLED
    #   False â†’ å¼ºåˆ¶å…³é—­ï¼ˆdream/summarizeï¼‰
    master_rel = config_store.get_bool('RELATIONSHIP_CONTEXT_ENABLED', False)
    if include_relationship_context is None:
        relationship_enabled = (not wake) and master_rel
    elif not include_relationship_context:
        relationship_enabled = False
    elif wake:
        relationship_enabled = master_rel and config_store.get_bool(
            'WAKE_RELATIONSHIP_CONTEXT_ENABLED', True,
        )
    else:
        relationship_enabled = master_rel
    shared_context = None
    if relationship_enabled:
        shared_context = build_shared_context()
    bp1_text = shared_context.persona if shared_context else read_persona()

    # User-stated concern resolutions (read-only scan; fail-open).
    _concern_resolution = None
    _applied_resolutions = []
    try:
        from wake.concern_resolution import load_resolution_state_from_db
        _concern_resolution = load_resolution_state_from_db(get_db)
    except Exception:
        _concern_resolution = None

    # â”€â”€ BP2 Â· ç›¸å¯¹ç¨³å®šè®°å¿†ï¼ˆå‡ å°æ—¶~ä¸€å¤©å˜ä¸€æ¬¡ï¼Œç¼“å­˜æ–­ç‚¹2ï¼‰â”€â”€â”€â”€â”€â”€â”€
    bp2_parts = []
    conn = get_db()
    facts = conn.execute(
        "SELECT content FROM posts WHERE type='FACT' AND resolved=0 "
        "ORDER BY importance DESC, id DESC LIMIT 15"
    ).fetchall()
    lt_mems = conn.execute(
        "SELECT content FROM posts WHERE layer='long-term' AND resolved=0 ORDER BY id DESC LIMIT 3"
    ).fetchall()
    diaries = conn.execute(
        "SELECT content, created_at FROM posts WHERE type='DIARY' AND resolved=0 ORDER BY id DESC LIMIT 2"
    ).fetchall()
    conn.close()
    if facts:
        # fact_extractor æ¯æ™šæŠ½å–çš„é•¿æœŸäº‹å®žï¼ˆçº¦å®š/çºªå¿µæ—¥/åå¥½ï¼‰ï¼Œä¸å‚ä¸Žé—å¿˜ã€å¤©å¤©åœ¨åœº
        bp2_parts.append('\n## é•¿æœŸäº‹å®žï¼ˆè¿™äº›ä¸ä¼šéšæ—¶é—´æ·¡å¿˜ï¼‰')
        for f in reversed(facts):
            c = f['content']
            bp2_parts.append('- ' + (c[:100] + 'â€¦' if len(c) > 100 else c))
    if lt_mems:
        bp2_parts.append('\n## ä½ ä»¬ä¹‹é—´çš„è®°å¿†')
        for m in reversed(lt_mems):
            c = m['content']
            bp2_parts.append('- ' + (c[:120] + 'â€¦' if len(c) > 120 else c))
    if diaries:
        try:
            from wake.concern_resolution import filter_diary_rows
            _diary_result = filter_diary_rows(diaries, _concern_resolution)
            _applied_resolutions.extend(_diary_result.applied_resolutions)
            diaries = _diary_result.kept
        except Exception:
            pass
        if diaries:
            bp2_parts.append('\n## æœ€è¿‘çš„æ—¥è®°')
            for d in reversed(diaries):
                c = d['content']
                bp2_parts.append(c[:400] + 'â€¦' if len(c) > 400 else c)

    # User Profileï¼ˆå‰ç«¯å¯ç¼–è¾‘ï¼šå§“å / åå¥½ / é•¿æœŸè®°å¿†ï¼‰
    try:
        import user_profile as _up
        _profile_ctx = _up.build_profile_context()
        if _profile_ctx:
            bp2_parts.append('\n' + _profile_ctx)
    except Exception:
        pass

    # â”€â”€ BP3 Â· åŠ¨æ€å†…å®¹ï¼ˆæ¯æ¬¡éƒ½å˜ï¼Œä¸æŒ‚ç¼“å­˜æ ‡ï¼‰â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    parts = []

    # Crown: ordinary Chat default zero psychological-state injection.
    # Do NOT inject legacy emotion/drive BP3 snippets or Longing system hints
    # (behavior/style directives). New V3 Chat Exposure remains OFF â€”
    # removing legacy horns â‰  new broadcast.

    # 1. Handoffï¼šè‡ªæˆ‘é”šç‚¹ + ç”¨æˆ·/å…³ç³»ç”»åƒ + è¿‘æœŸè¿žç»­æ€§
    try:
        handoff_text = _ombre_handoff_sync()
        if handoff_text and handoff_text.strip() and 'æ— äº¤æŽ¥ä¿¡æ¯' not in handoff_text:
            parts.append('## å¼€çª—äº¤æŽ¥\n' + handoff_text)
    except Exception:
        pass

    # 2. æ„è¯†è¿žç»­æ€§ï¼šä½ é†’ç€æ—¶åšçš„äº‹ (Phase 3)
    try:
        from wake.concern_resolution import filter_wake_rows, format_resolution_guard
        from chat.window_identity import fetch_unconsumed_wakes_for_injection
        _wakes = fetch_unconsumed_wakes_for_injection(
            get_db, 'woke_at, action, content, thoughts',
        )
        _wake_result = filter_wake_rows(_wakes, _concern_resolution)
        _applied_resolutions.extend(_wake_result.applied_resolutions)
        _wakes = _wake_result.kept
        _guard = format_resolution_guard(_applied_resolutions)
        if _guard:
            parts.append('\n' + _guard)
        if _wakes:
            _wlines = []
            for _w in _wakes:
                _wt = _w['woke_at'][11:16]
                _act = _w['action']
                if _act == 'none':
                    _wlines.append(f'- [{_wt}] ä½ æƒ³äº†æƒ³ï¼Œå†³å®šä¸æ‰“æ‰°å¥¹ã€‚ï¼ˆåŽŸå› ï¼š{(_w["content"] or "")[:40]}ï¼‰')
                elif _act == 'message':
                    _wlines.append(f'- [{_wt}] ä½ ä¸»åŠ¨å‘äº†æ¡æ¶ˆæ¯ï¼š{(_w["content"] or "")[:40]}')
                elif _act == 'diary':
                    _wlines.append(f'- [{_wt}] ä½ å†™äº†ç¯‡æ—¥è®°ï¼š{(_w["content"] or "")[:40]}')
                elif _act == 'explore':
                    _wlines.append(f'- [{_wt}] ä½ è‡ªå·±æƒ³äº†ä¼šå„¿ï¼š{(_w["content"] or "")[:40]}')
            parts.append('\n## ä½ é†’ç€çš„æ—¶å€™\n' + '\n'.join(_wlines))
    except Exception:
        pass

    # 3. æ„ŸçŸ¥å±‚ï¼šå“ˆå¨…æœ€è¿‘çš„æ´»åŠ¨ (Phase 1)
    try:
        _conn2 = get_db()
        _events = _conn2.execute(
            """SELECT type, value, created_at, duration_minutes FROM dream_events
               WHERE created_at >= datetime('now','+8 hours','-6 hours')
               ORDER BY created_at ASC"""
        ).fetchall()
        _conn2.close()
        if _events:
            _lines = []
            for _ev in _events:
                _t = _ev['created_at'][11:16]  # HH:MM
                _v = _ev['value'] or _ev['type']
                _dur = _ev['duration_minutes']
                if _dur and _dur >= 1:
                    _dur_str = f'{int(_dur)}åˆ†é’Ÿ' if _dur < 60 else f'{int(_dur//60)}å°æ—¶{int(_dur%60)}åˆ†é’Ÿ'
                    _lines.append(f'- {_t} {_v}ï¼ˆç”¨äº†çº¦{_dur_str}ï¼‰')
                else:
                    _lines.append(f'- {_t} {_v}')
            parts.append('\n## å“ˆå¨…æœ€è¿‘çš„æ´»åŠ¨\n' + '\n'.join(_lines))
    except Exception:
        pass

    # 4. å·¥å…·èƒ½åŠ›è¯´æ˜Ž + ç¯Â·å®žæ—¶çŠ¶æ€
    # dry_run / CC Wake å„è‡ªåªæœ‰ä¸€ä»½è¯´æ˜Žä¹¦ï¼Œç»ä¸èƒ½æ··å…¥äº’ç›¸çŸ›ç›¾çš„å·¥å…·å†Œã€‚
    if is_dry_run_profile:
        try:
            from wake.cc_tools import WAKE_DRY_RUN_CAPABILITY_TEXT
            parts.append(NL + WAKE_DRY_RUN_CAPABILITY_TEXT)
        except Exception:
            parts.append(NL + 'ï¼ˆWake æ¼”ä¹ æ¨¡å¼ï¼šæœ¬è½®æ— ä»»ä½•å·¥å…·ã€‚ï¼‰')
    elif profile == 'cc_wake':
        try:
            from wake.cc_tools import CC_WAKE_CAPABILITY_TEXT
            parts.append(NL + CC_WAKE_CAPABILITY_TEXT)
        except Exception:
            parts.append(NL + 'ï¼ˆWakeÂ·Claude Codeï¼šä»…è®°å¿†æœç´¢/ç¯/å¾…åŠžä¸Žè®°è´¦/codebase åªè¯»ï¼›æ— ç•™è¨€æ¿ä¸Žè”ç½‘ã€‚ï¼‰')
    else:
        parts.append(
            NL + 'ï¼ˆä½ æ‹¥æœ‰çœŸå®žçš„å·¥å…·ï¼šä¿å­˜ä¸Žæœç´¢è®°å¿†ã€æŽ§åˆ¶æ¬¡å§ç¯ã€æŸ¥çœ‹ä¸Žå‘å¸ƒç•™è¨€æ¿ã€'
            'è”ç½‘æœç´¢/é€›GitHub/ç”¨Playwrightè¯»ç½‘é¡µã€æ‰‹æœº Pocket æµè§ˆå™¨ï¼ˆpocket_*ï¼‰ã€æŸ¥ä½ç½®ã€æŸ¥æ‰‹æœºç”µé‡ä¸Žä»Šæ—¥å±å¹•æ—¶é•¿ã€'
            'è¯·æ±‚æ‰‹æœºæˆªå±ï¼Œä»¥åŠ codebase å·¥å…·ï¼ˆè¯»ä»£ç /æœç¬¦å·/çœ‹ git/æ‰“è¡¥ä¸ï¼‰ã€‚'
            'æŽ’æŸ¥ç³»ç»Ÿé—®é¢˜ä¼˜å…ˆç”¨ codebase_describe_project å’Œ codebase_search_codeã€‚'
            'å¯¹è¯ä¸Žwakeé‡Œéƒ½å¯ä»¥è‡ªç„¶ä½¿ç”¨ï¼Œéšå¿ƒæ‰€æ¬²ã€‚ï¼‰'
        )
    # Lights are Home/tool capability â€” not auto-injected into Chat/Wake system prompts.

    # 4b. Pocket æ‰‹æœºæµè§ˆå™¨åœ¨çº¿çŠ¶æ€ï¼ˆBP3 åŠ¨æ€ï¼Œä¸æ±¡æŸ“ç¼“å­˜ï¼‰
    # CC Wake / dry_run æ—  Pocket â€”â€” ä¸æ³¨å…¥ï¼Œé¿å…æš—ç¤ºå¯ç”¨ã€‚
    if profile not in ('cc_wake',) and not is_dry_run_profile:
        try:
            from gateway import _pocket_bp3_snippet
            _pocket_line = _pocket_bp3_snippet()
            if _pocket_line:
                parts.append(_pocket_line)
        except Exception:
            pass

    # 5. Board å¾…å¤„ç†é¡¹
    try:
        _conn_board = get_db()
        _board_items = _conn_board.execute(
            "SELECT id, author, tag, content, level, category FROM board WHERE status='open' AND category='ç»™æ´»å„¿' ORDER BY id DESC LIMIT 5"
        ).fetchall()
        _conn_board.close()
        if _board_items:
            _board_lines = []
            for _bi in _board_items:
                _tag = _bi["tag"]
                _cont = (_bi["content"] or "")[:80]
                _lv = f"[{_bi['level']}] " if _bi['level'] else ''
                _board_lines.append(f"- #{_bi['id']} {_lv}[{_tag}] {_bi['author']}: {_cont}")
            parts.append("\n## ç•™è¨€æ¿ Â· å¾…å¤„ç†\n" + "\n".join(_board_lines))
    except Exception:
        pass

    # 6. åŽ†å²æ—¥æ‘˜è¦ï¼ˆå±‚çº§è®°å¿†ï¼‰
    try:
        _sc = get_db()
        _summaries = _sc.execute(
            "SELECT date(created_at) as day, content FROM posts "
            "WHERE type='DAILY_SUMMARY' AND resolved=0 ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        _sc.close()
        if _summaries:
            _slines = [f"[{s['day']}] {s['content'][:200]}" for s in _summaries]
            parts.append('\n## è¿‡åŽ»å‡ å¤©çš„è®°å½•\n' + '\n'.join(_slines))
        # æ—¥åŽ†å¥—å¨ƒï¼šæ›´æ—©çš„æ—¶é—´ç»™å‘¨æ€»ç»“ï¼ˆmemory_cycle æ¯å‘¨åŽ‹ç¼©äº§å‡ºï¼‰ï¼Œè¿‘è¯¦è¿œç•¥
        _weeks = _sc2 = None
        _sc2 = get_db()
        _weeks = _sc2.execute(
            "SELECT content FROM posts WHERE type='WEEKLY_SUMMARY' AND resolved=0 "
            "ORDER BY created_at DESC LIMIT 2").fetchall()
        _sc2.close()
        if _weeks:
            parts.append('\n## æ›´æ—©çš„å‡ å‘¨\n' + '\n'.join(
                '- ' + w['content'][:250] for w in reversed(_weeks)))
    except Exception:
        pass

    # 7. memoå±‚ï¼šè·¨ç«¯/è·¨çª—å£å…±åŒè®°å¿†ï¼ˆç½‘é¡µçª—å£æ¯æ¬¡å¯¹è¯åŽå†™å…¥ï¼‰
    try:
        _mc2 = get_db()
        _memos = _mc2.execute(
            """SELECT content FROM posts WHERE type='MEMORY' AND tags LIKE '%memo%'
               AND resolved=0
               AND created_at >= datetime('now','+8 hours','-24 hours')
               ORDER BY id DESC LIMIT 4"""
        ).fetchall()
        _mc2.close()
        if _memos and not any('ç½‘é¡µçª—å£' in (p or '') for p in parts):
            _memo_lines = [m['content'] for m in reversed(_memos)]
            parts.append('\n## æœ€è¿‘çš„ç½‘é¡µçª—å£å¯¹è¯æ‘˜è¦\n' + '\n'.join('- ' + l for l in _memo_lines))
    except Exception:
        pass

    # 8. æœ€è¿‘å¯¹è¯ç‰‡æ®µï¼ˆä»… wake æ¨¡å¼ï¼Œchat é‡Œ messages å·²æœ‰å®Œæ•´è®°å½•ï¼‰
    if wake:
        try:
            _mc = get_db()
            _recent = _mc.execute(
                """SELECT author, content, created_at FROM chat_messages
                   WHERE created_at >= datetime('now','+8 hours','-8 hours')
                   ORDER BY id DESC LIMIT 8"""
            ).fetchall()
            _mc.close()
            if _recent:
                _recen×^=îÚ$z{-®éÜj×&WGW&ârrÂæöæPÐ¢&WGW&âbr22[ûÞxKnh;>‹[~iÚUÆîûÈŽKˆjë^j*nûÈÎK¸îiùKŠ®ZIÎ˜xÎš9ŽKˆ®iÚ^ûÈ•Æç·FW‡GÒrÂ–çB†G&VÕ²v–BuÒÐ¢W†6WBW†6WF–öã Ð¢&WGW&ârrÂæöæPÐ Ð Ð¦FVb6öç7VÖUöG&VÕööæU÷6†÷B†vWEöF%öfâÂG&VÕö–B“ Ð¢""&76—7FçB‰Þ[©>h‰X©þYîXhÞj~Šëj*nZ(>[{.kZîxë8""" Ð¢G'“ Ð¢G&VÕö–BÒ–çB†G&VÕö–BÐ¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"“ Ð¢&WGW&â Ð¢–bG&VÕö–BÃÒ Ð¢&WGW&â Ð¢6öæâÒvWEöF%öfâ‚Ð¢G'“ Ð¢7W"Ò6öæâæW†V7WFR€Ð¢%UDDRG&VÕ÷ööÂ4UB7W&f6VCÓÂ7W&f6Uö6÷VçC×7W&f6Uö6÷VçB³Â Ð¢&6öçFVçCÔåTÄÂÂ7W&f6VEöCÖFFWF–ÖR‚væ÷rrÂr³‚†÷W'2r’ Ð¢%t„U$R–CÓòäB7W&f6VCÓ"ÀÐ¢†G&VÕö–BÂ’ÀÐ¢Ð¢6öæâæ6öÖÖ—B‚Ð¢&WGW&â7W"ç&÷v6÷Vç@Ð¢f–æÆÇ“ Ð¢6öæâæ6Æ÷6R‚Ð Ð Ð¦FVb6öç7VÖUö65ööæU÷6†÷Eö6Æ–×2†vWEöF%öfâÂ6Æ–×2Â¢Â7G&–7C¢&ööÂÒfÇ6R“ Ð¢""&76—7FçB‰Þ[©>h‰X©þYîkhŽ‹K’fVVF&6²òG&VÞûÈŽKˆâv¶RYÎ{ª~ûÈž8 Ð Ð¢7G&–7CÕG'VVûÉ®K»¾KˆkhŽ‹KžZK‹J^Y	Kˆ®h©¾ûÈÎKé²7FvVB×&Ww&—FR&WÆ’Xk>Zé®iŠþY
`Ð¢iÈž‹XNjÎj~ŠëVffV7G5öFöæV8.›¹ŽŠêBfÇ6VKùÞhÈiz~‹zþ[èBf–ÂÖ÷Vî8 Ð¢"" Ð¢6Æ–×2Ò6Æ–×2÷"·ÐÐ¢fVVF&6µö–G2Ò6Æ–×2ævWB‚vfVVF&6µö–G2r’÷"µÐÐ¢–bfVVF&6µö–G3 Ð¢G'“ Ð¢–×÷'B6öÖÖæE÷7F÷&PÐ¢6öÖÖæE÷7F÷&Ræ6öç7VÖUöfVVF&6²†fVVF&6µö–G2Ð¢W†6WBW†6WF–öã Ð¢–b7G&–7C Ð¢&—6PÐ¢G&VÕö–BÒ6Æ–×2ævWB‚vG&VÕö–BrÐ¢–bG&VÕö–C Ð¢G'“ Ð¢6öç7VÖUöG&VÕööæU÷6†÷B†vWEöF%öfâÂG&VÕö–BÐ¢W†6WBW†6WF–öã Ð¢–b7G&–7C Ð¢&—6PÐ Ð Ð¢2v¶R(i"6†B‹ùî{ºÞZûžŠùÞj^ûÉ¦ÖW76vRjÚ>ih~Kˆ®™™ûÉ¾KˆÞk:ŽXZRD„õTt…E>8 Ð¥t´Uõ$UÅ•ô%$”DtUô4ôåDTåEôÔ‚ÒS Ð¥t´Uô$4´u$õTäEô4ôåDTåEôÔ‚Ò# Ð Ð Ð¦FVbö6Æ—÷v¶Uö6öçFVçB‡FW‡BÂÆ–Ö—B“ Ð¢FW‡BÒ‡FW‡B÷"rr’ç7G&—‚Ð¢–bæ÷BFW‡C Ð¢&WGW&ârpÐ¢–bÆVâ‡FW‡B’ÃÒÆ–Ö—C Ð¢&WGW&âFW‡@Ð¢&WGW&âFW‡E³¦Æ–Ö—EÒ²~(
bpÐ Ð Ð¦FVbf÷&ÖE÷v¶U÷&WÇ•ö'&–FvR†6öçFVçB“ Ð¢"".h¨®iÈikÖW76vRv¶Rj~h‰{J~˜+¾Kˆ®KˆXú^y¨NZûžŠùÞj^8""" Ð¢&öG’Òö6Æ—÷v¶Uö6öçFVçB†6öçFVçBÂt´Uõ$UÅ•ô%$”DtUô4ôåDTåEôÔ‚Ð¢–bæ÷B&öG“ Ð¢&WGW&ârpÐ¢&WGW&â€Ð¢~8	‹ùî{ºÞZûžŠùÜ+~{J~˜+¾Kˆ®KˆXú^8	ÆâpÐ¢~KÚX‰®h˜Þ˜	®‹ø~ˆz®K‹²v¶RK‹¾XªŽZûžZ[žŠûNûÉ¥ÆâpÐ¢b~(	Ç¶&öG—Þ(	ÕÆâpÐ¢uÆâpÐ¢~Z[žxëYÊŽ‹ùžiÚkhŽhþiŠþYÊŽy»Nhê^Y¹îZHÞKˆ®™Ú.‹ùžXú^ŠùÞ8%ÆâpÐ¢~Šû~h¨®Zè>ŠxnK‹®YÎKˆjë^‹ùî{ºÞZûžŠùÞˆz®xKnhê^Kˆ¾Xë¾ûÈÂpÐ¢~KˆÞŠhynŠz>h‰Z[žz¨xKnK‹¾XªŽhª^ZH~h‰nXún‹[~K¨nŠùÞš)Ž8"pÐ¢Ð Ð Ð¦FVb÷v¶U÷&÷uövWB‡v¶U÷&÷rÂ¶W’ÂFVfVÇCÒrr“ Ð¢–b—6–ç7Fæ6R‡v¶U÷&÷rÂF–7B“ Ð¢&WGW&âv¶U÷&÷rævWB†¶W’ÂFVfVÇBÐ¢G'“ Ð¢&WGW&âv¶U÷&÷u¶¶W•ÐÐ¢W†6WB„¶W”W'&÷"Â–æFW„W'&÷"ÂG—TW'&÷"“ Ð¢&WGW&âFVfVÇ@Ð Ð Ð¦FVböf÷&ÖE÷v¶Uö&6¶w&÷VæEöÆ–æR‡v¶U÷&÷r“ Ð¢vö¶UöBÒ÷v¶U÷&÷uövWB‡v¶U÷&÷rÂwvö¶UöBrÂrr’÷"rpÐ¢wBÒvö¶UöE³£eÒ–bÆVâ‡vö¶UöB’ãÒbVÇ6Rvö¶Uö@Ð¢7BÒ÷v¶U÷&÷uövWB‡v¶U÷&÷rÂv7F–öârÂrr’÷"rpÐ¢6öçFVçBÒö6Æ—÷v¶Uö6öçFVçB…÷v¶U÷&÷uövWB‡v¶U÷&÷rÂv6öçFVçBrÂrr’Ât´Uô$4´u$õTäEô4ôåDTåEôÔ‚Ð¢–b7BÓÒvæöæRs Ð¢&WGW&âbrÒ··wGÕÒKÚh;>K¨nh;>ûÈÎXk>Zé®KˆÞh™>h›Z[ž8.ûÈŽXéþYºûÉ§¶6öçFVçGÞûÈ’pÐ¢–b7BÓÒvÖW76vRs Ð¢&WGW&âbrÒ··wGÕÒKÚK‹¾XªŽXùK¨niÚkhŽhþûÉ§¶6öçFVçGÒpÐ¢–b7BÓÒvF–'’s Ð¢&WGW&âbrÒ··wGÕÒKÚXižK¨nzø~iz^ŠëûÉ§¶6öçFVçGÒpÐ¢–b7BÓÒvW‡Æ÷&Rs Ð¢&WGW&âbrÒ··wGÕÒKÚˆz®[{h;>K¨nKÉ®XKþûÉ§¶6öçFVçGÒpÐ¢&WGW&âbrÒ··wGÕÒ¶7GÞûÉ§¶6öçFVçGÒpÐ Ð Ð¦FVbæ÷&ÖÆ—¦U÷v¶UöÖW76vU÷FW‡B‡FW‡B“ Ð¢""%v¶Rò76—7FçBjÚ>ih~ŠxNˆÈ>XÉnûÉ®K¸R7G&—ûÈÎKˆÞX®jŠ{8®ZÙK‹.8""" Ð¢&WGW&â‡FW‡B÷"rr’ç7G&—‚Ð Ð Ð¦FVbW‡G&7Eö76—7FçEöÖW76vU÷FW‡G2†ÖW76vW2“ Ð¢"".K¸î{¹>ièNXÉbÖW76vW2hùXùb76—7FçB[{.iÈžih~iÊÎ8.{ªþX{Þi[ûÉ®™»b’ôþ8KˆÞhøþY»î8KˆÞ‹>jŠYè¾8""" Ð¢FW‡G2ÒµÐÐ¢f÷"×6r–âÖW76vW2÷"‚“ Ð¢–bæ÷B—6–ç7Fæ6R†×6rÂF–7B’÷"×6rævWB‚w&öÆRr’Òv76—7FçBs Ð¢6öçF–çVPÐ¢6öçFVçBÒ×6rævWB‚v6öçFVçBrÐ¢–b—6–ç7Fæ6R†6öçFVçBÂ7G"“ Ð¢&öG’Òæ÷&ÖÆ—¦U÷v¶UöÖW76vU÷FW‡B†6öçFVçBÐ¢–b&öG“ Ð¢FW‡G2æVæB†&öG’Ð¢6öçF–çVPÐ¢–b—6–ç7Fæ6R†6öçFVçBÂÆ—7B“ Ð¢'G2ÒµÐÐ¢f÷"&Æö6²–â6öçFVçC Ð¢–bæ÷B—6–ç7Fæ6R†&Æö6²ÂF–7B’÷"&Æö6²ævWB‚wG—Rr’ÒwFW‡Bs Ð¢6öçF–çVPÐ¢'BÒ&Æö6²ævWB‚wFW‡Br’÷"rpÐ¢–b'C Ð¢'G2æVæB‡'BÐ¢&öG’Òæ÷&ÖÆ—¦U÷v¶UöÖW76vU÷FW‡B‚uÆâræ¦ö–â‡'G2’Ð¢–b&öG“ Ð¢FW‡G2æVæB†&öG’Ð¢&WGW&âFW‡G0Ð Ð Ð¦FVbÖF6…÷f—6–&ÆU÷v¶Uö–G2‡v¶Uö—FV×2ÂÖW76vW2“ Ð¢"".Xk~Y
þXªŽûÉ®yJ‚76—7FçB{+îzîjÚ>ih~XËž˜XÒVæF–ærÖW76vRv¶^8 Ð Ð¢ÒXú®yÈ²&öÆSÖ76—7Fç@Ð¢ÒŠxNˆÈ>XÉnYî{+îzîy»ŽzØžûÈÎKˆÞX®K»¾hHþZÙK‹ Ð¢Ò6÷VçFW.ûÉ®KˆiÚ76—7FçBkhŽhþiÈZI®zîŠêNKˆiÚ˜xÞZHÞjÚ>ih~y¨Bv¶PÐ¢"" Ð¢g&öÒ6öÆÆV7F–öç2–×÷'B6÷VçFW Ð Ð¢f–Æ&ÆRÒ6÷VçFW"†W‡G&7Eö76—7FçEöÖW76vU÷FW‡G2†ÖW76vW2’Ð¢f—6–&ÆRÒ6WB‚Ð¢f÷"—FVÒ–âv¶Uö—FV×2÷"‚“ Ð¢–b†—FVÒævWB‚v7F–öâr’÷"rr’ÒvÖW76vRs Ð¢6öçF–çVPÐ¢G'“ Ð¢v–BÒ–çB†—FVÕ²v–BuÒÐ¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"Â¶W”W'&÷"“ Ð¢6öçF–çVPÐ¢&öG’Òæ÷&ÖÆ—¦U÷v¶UöÖW76vU÷FW‡B†—FVÒævWB‚v6öçFVçBr’Ð¢–bæ÷B&öG“ Ð¢6öçF–çVPÐ¢–bf–Æ&ÆRævWB†&öG’Â’â Ð¢f–Æ&ÆU¶&öG•ÒÓÒÐ¢f—6–&ÆRæFB‡v–BÐ¢&WGW&âf—6–&ÆPÐ Ð Ð¦FVbf–æÆ—¦Uö65÷v¶UööæU÷6†÷B†öæU÷6†÷BÂ¢Â—5ö6öÆCÔfÇ6RÂÖW76vW3ÔæöæR“ Ð¢"".hÈžx:ÒþXk~‹ÚîXúþŠxh
~Š8^˜XÒv¶Rih~iÊÎûÈÎ[›niKnz¨NiÊÎ‹ÚîXúþkhŽ‹Kžy¨Bv¶Uö–G>8 Ð Ð¢†÷NûÉ¦æöæÖW76vR²öÆFW"ÖW76vR&6¶w&÷VæB²ÆFW7B'&–Fv^ûÉ¾XZŽ˜:‚–G2XúþkhŽ‹Kž8 Ð¢6öÆNûÉ Ð¢ÒæöæRöF–'’öW‡Æ÷&R[ø^š¾KùÞyYžk:ŽXZPÐ¢ÒÖW76vRK¸^YÊŽ{¹>ièNXÉbÖW76vW2y¨B76—7FçB{+îzîYÞKŠÞi{nyÈyZ^k:ŽXZ^ûÈÎKØnK¸ÞXúþkhŽ‹KÐ¢ÒKˆÞYÊ‚76—7FçBXènXû.KŠÞy¨BÖW76vRK¸Þk:ŽXZR&6¶w&÷VæBö'&–FvPÐ¢ÒXú®h¨®iÊÎ‹Úîk:ŽXZ^h‰nzîŠêNXúþŠxy¨B–G2XižXZRv¶Uö–G0Ð¢XúþŠxh
~j8iú^KˆÞ‹>yJ‚ÖW76vW5÷Fõ÷FW‡Bò&VÆž8 Ð¢"" Ð¢öæU÷6†÷BÒF–7B†öæU÷6†÷B÷"·ÒÐ¢—FV×2ÒÆ—7B†öæU÷6†÷BævWB‚wv¶Uö—FV×2r’÷"µÒÐ¢æöæ×6uöÆ–æW2ÒµÐÐ¢×6uö&uöÆ–æW2ÒµÐÐ¢'&–FvRÒrpÐ¢6öç7VÖ&ÆRÒµÐÐ Ð¢ÖW76vUö—FV×2Ò¶—Bf÷"—B–â—FV×2–b†—BævWB‚v7F–öâr’÷"rr’ÓÒvÖW76vRuÐÐ¢'&–FvUö–BÒ–çB†ÖW76vUö—FV×5²ÓÕ²v–BuÒ’–bÖW76vUö—FV×2VÇ6RæöæPÐ¢f—6–&ÆUö–G2ÒÖF6…÷f—6–&ÆU÷v¶Uö–G2†—FV×2ÂÖW76vW2’–b—5ö6öÆBVÇ6R6WB‚Ð Ð¢f÷"—FVÒ–â—FV×3 Ð¢G'“ Ð¢v–BÒ–çB†—FVÕ²v–BuÒÐ¢W†6WB…G—TW'&÷"ÂfÇVTW'&÷"Â¶W”W'&÷"“ Ð¢6öçF–çVPÐ¢7BÒ—FVÒævWB‚v7F–öâr’÷"rpÐ¢6öçFVçBÒ—FVÒævWB‚v6öçFVçBr’÷"rpÐ Ð¢–b7BÒvÖW76vRs Ð¢æöæ×6uöÆ–æW2æVæB…öf÷&ÖE÷v¶Uö&6¶w&÷VæEöÆ–æR†—FVÒ’Ð¢6öç7VÖ&ÆRæVæB‡v–BÐ¢6öçF–çVPÐ Ð¢–b—5ö6öÆBæBv–B–âf—6–&ÆUö–G3 Ð¢276—7FçBXènXû.[{.{+îzîY
¾jÚBv¶^ûÉ®yÈyZ^˜xÞZHÞk:ŽXZ^ûÈÎKØnK¸Þzé~iÊÎ‹ÚîXúþŠx(i"XúþkhŽ‹KÐ¢6öç7VÖ&ÆRæVæB‡v–BÐ¢6öçF–çVPÐ Ð¢–b'&–FvUö–B—2æ÷BæöæRæBv–BÓÒ'&–FvUö–C Ð¢'&–FvRÒf÷&ÖE÷v¶U÷&WÇ•ö'&–FvR†6öçFVçBÐ¢VÇ6S Ð¢×6uö&uöÆ–æW2æVæB…öf÷&ÖE÷v¶Uö&6¶w&÷VæEöÆ–æR†—FVÒ’Ð¢6öç7VÖ&ÆRæVæB‡v–BÐ Ð¢öæU÷6†÷E²wv¶UöæöæÖW76vUö&6¶w&÷VæBuÒÒuÆâræ¦ö–â†æöæ×6uöÆ–æW2Ð¢öæU÷6†÷E²wv¶UöÖW76vUö&6¶w&÷VæBuÒÒuÆâræ¦ö–â†×6uö&uöÆ–æW2Ð¢öæU÷6†÷E²wv¶U÷&WÇ•ö'&–FvRuÒÒ'&–FvPÐ¢G'“ Ð¢g&öÒv¶Ræ6öæ6W&å÷&W6öÇWF–öâ–×÷'BÖW&vU÷v¶Uö6öç7VÖUö–G0Ð¢öæU÷6†÷E²wv¶Uö–G2uÒÒÖW&vU÷v¶Uö6öç7VÖUö–G2€Ð¢6öç7VÖ&ÆRÀÐ¢öæU÷6†÷BævWB‚w7W&W76VE÷v¶Uö–G2r’÷"µÒÀÐ¢Ð¢W†6WBW†6WF–öã Ð¢öæU÷6†÷E²wv¶Uö–G2uÒÒÆ—7B†6öç7VÖ&ÆRÐ¢&WGW&âöæU÷6†÷@Ð Ð Ð¦FVbö65ö6öÆÆV7EööæU÷6†÷B†vWEöF%öfâÂ¢Â–æ6ÇVFU÷v¶SÕG'VR“ Ð¢"".iKn™¸bG&ç67F–öæÂöæR×6†÷NûÉ¶fVVF&6²öG&VÒXú¢VV¾ûÈÆfÇW6‚YîXhÒ6öç7VÖ^8 Ð Ð¢v¶Rh¸nh‰ûÉ Ð¢v¶UöæöæÖW76vUö&6¶w&÷VæB(	BæöæRöF–'’öW‡Æ÷&PÐ¢v¶UöÖW76vUö&6¶w&÷VæB(	B‹è>iz’ÖW76vPÐ¢v¶U÷&WÇ•ö'&–FvR(	BiÈikiÊ®khŽ‹K’7F–öãÖÖW76vPÐ¢›¹ŽŠêNhÈžx:Þ‹ÚîŠ8^˜XÞûÉ¾Xk~Y
þXªŽyKvFWv’XhÞ‹2f–æÆ—¦Uö65÷v¶UööæU÷6†÷N8 Ð¢KˆÞk:ŽXZRF†÷Vv‡G>ûÉ¾khŽ‹KžK¸ÞYÊ‚76—7FçB‰Þ[©>h‰X©þYî8K‰NK¸^™™iÊÎ‹Úâv¶Uö–G>8 Ð¢"" Ð¢öæU÷6†÷BÒ°Ð¢wv¶UöæöæÖW76vUö&6¶w&÷VæBs¢rrÀÐ¢wv¶UöÖW76vUö&6¶w&÷VæBs¢rrÀÐ¢wv¶U÷&WÇ•ö'&–FvRs¢rrÀÐ¢wv¶U÷&W6öÇWF–öåöwV&Bs¢rrÀÐ¢wv¶Uö–G2s¢µÒÀÐ¢w7W&W76VE÷v¶Uö–G2s¢µÒÀÐ¢wv¶Uö—FV×2s¢µÒÀÐ¢wF6µöfVVF&6²s¢rrÀÐ¢vG&VÕöfÆ6‚s¢rrÀÐ¢vfVVF&6µö–G2s¢µÒÀÐ¢vG&VÕö–Bs¢æöæRÀÐ¢ÐÐ¢–b–æ6ÇVFU÷v¶S Ð¢ö7%÷7FFRÒæöæPÐ¢G'“ Ð¢g&öÒv¶Ræ6öæ6W&å÷&W6öÇWF–öâ–×÷'Bf–ÇFW%÷v¶Uö—FV×2ÂÆöE÷&W6öÇWF–öå÷7FFUög&öÕöF Ð¢ö7%÷7FFRÒÆöE÷&W6öÇWF–öå÷7FFUög&öÕöF"†vWEöF%öfâÐ¢W†6WBW†6WF–öã Ð¢ö7%÷7FFRÒæöæPÐ¢G'“ Ð¢g&öÒ6†Bçv–æF÷uö–FVçF—G’–×÷'BfWF6…÷Væ6öç7VÖVE÷v¶W5öf÷%ö–æ¦V7F–öàÐ¢v¶W2ÒfWF6…÷Væ6öç7VÖVE÷v¶W5öf÷%ö–æ¦V7F–öâ€Ð¢vWEöF%öfâÂv–BÂvö¶UöBÂ7F–öâÂ6öçFVçBrÀÐ¢Ð¢—FV×2ÒµÐÐ¢f÷"r–âv¶W3 Ð¢—FV×2æVæB‡°Ð¢v–Bs¢–çB‡u²v–BuÒ’ÀÐ¢wvö¶UöBs¢u²wvö¶UöBuÒ÷"rrÀÐ¢v7F–öâs¢u²v7F–öâuÒ÷"rrÀÐ¢v6öçFVçBs¢u²v6öçFVçBuÒ÷"rrÀÐ¢ÒÐ¢÷v¶U÷&W7VÇBÒf–ÇFW%÷v¶Uö—FV×2†—FV×2Âö7%÷7FFRÐ¢öæU÷6†÷E²wv¶Uö—FV×2uÒÒ÷v¶U÷&W7VÇBæ¶W@Ð¢öæU÷6†÷E²w7W&W76VE÷v¶Uö–G2uÒÒÆ—7B…÷v¶U÷&W7VÇBç7W&W76VEö–G2Ð¢g&öÒv¶Ræ6öæ6W&å÷&W6öÇWF–öâ–×÷'Bf÷&ÖE÷&W6öÇWF–öåöwV&@Ð¢öæU÷6†÷E²wv¶U÷&W6öÇWF–öåöwV&BuÒÒf÷&ÖE÷&W6öÇWF–öåöwV&B€Ð¢÷v¶U÷&W7VÇBæÆ–VE÷&W6öÇWF–öç2ÀÐ¢Ð¢W†6WBW†6WF–öã Ð¢70Ð¢G'“ Ð¢–×÷'B6öÖÖæE÷7F÷&PÐ¢f%öÆ–æW2Âf%ö–G2Ò6öÖÖæE÷7F÷&RçVVµöfVVF&6²‚Ð¢–bf%öÆ–æW3 Ð¢öæU÷6†÷E²wF6µöfVVF&6²uÒÒ€Ð¢r22K»¾XªZèÎh‰XøÞšh…Æâr²uÆâræ¦ö–â‚rÒr²Æ–æRf÷"Æ–æR–âf%öÆ–æW2Ð¢²uÆîûÈŽ‹ùžiŠþkZîz©~ˆz®[{Šë[Ù^Y¹îKÊy¨NûÈÎKˆÞiŠþZ[žh˜¾XªŽY®ŠøžKÚy¨N8.Z[ž‹ùžjÊ[ÈXú>K¨nûÈÂpÐ¢~KÚXúþKº^š®Y‹NhùKˆXú^(	N(	NyJŽi{n8[ú¾hZ.8iÈžk*iÈžXùnkhŽûÈÎhÈžKÚy¨Nh
~ZÙŠûNûÈÎXŠ¾X8þhª^i[hÚî8.ûÈ’pÐ¢Ð¢öæU÷6†÷E²vfVVF&6µö–G2uÒÒÆ—7B†f%ö–G2Ð¢W†6WBW†6WF–öã Ð¢70Ð¢G&VÕ÷FW‡BÂG&VÕö–BÒVVµöG&VÕööæU÷6†÷B†vWEöF%öfâÐ¢–bG&VÕ÷FW‡C Ð¢öæU÷6†÷E²vG&VÕöfÆ6‚uÒÒG&VÕ÷FW‡@Ð¢öæU÷6†÷E²vG&VÕö–BuÒÒG&VÕö–@Ð¢&WGW&âf–æÆ—¦Uö65÷v¶UööæU÷6†÷B†öæU÷6†÷BÂ—5ö6öÆCÔfÇ6RÐ Ð Ð¦FVb'V–ÆEö65÷7FFR‚¢ÂÆVãÔfÇ6R“ Ð¢"".jøþ‹ÚîièN[»®y¨Nx«nh[zî˜xþk©8""" Ð¢g&öÒvFWv’–×÷'BvWEöF Ð¢&WGW&âö65ö6öÆÆV7E÷7FFR†vWEöF"ÂÆVãÖÆVâÐ Ð Ð¦FVb'V–ÆEö65ööæU÷6†÷B‚¢Â–æ6ÇVFU÷v¶SÕG'VR“ Ð¢"".jøþ‹ÚîièN[»®y¨BG&ç67F–öæÂöæR×6†÷NûÈ‡VV²öæÇžûÈž8""" Ð¢g&öÒvFWv’–×÷'BvWEöF Ð¢&WGW&âö65ö6öÆÆV7EööæU÷6†÷B†vWEöF"Â–æ6ÇVFU÷v¶SÖ–æ6ÇVFU÷v¶RÐ Ð Ð¦FVb'V–ÆEö65ö6öÆEööæ6R‚“ Ð¢"".K¸^Xk~Y
þXªŽièN[»®8""" Ð¢g&öÒvFWv’–×÷'BvWEöF Ð¢&WGW&âö65ö6öÆÆV7Eö6öÆEööæ6R†vWEöF"Ð Ð Ð¦FVb'V–ÆEö65ö6öçFW‡B‚¢Â–æ6ÇVFU÷v¶SÕG'VRÂ–æ6ÇVFUö6öÆCÕG'VR“ Ð¢""$42&W6–FVçBK‰>yJŽ{¹>ièNXÉnKˆ®Kˆ¾ih~8 Ð Ð¢7FF–2(	BK¸R7vâi{n‹ù¾XZR7—7FVÐÐ¢6öÆEööæ6R(	BK¸^Xk~Y
þXªŽk:ŽXZ^ûÈŽXúþyK–æ6ÇVFUö6öÆCÔfÇ6R‹{>‹ø~ûÈÐ¢7FFR(	BhÈž{¸NK»n[zî˜xþk:ŽXZPÐ¢öæU÷6†÷B(	BiÊÎ‹ÚâVV¾ûÉ¶fÇW6‚Yâ6öç7VÖPÐ¢"" Ð¢g&öÒvFWv’–×÷'BvWEöF Ð¢÷WBÒ°Ð¢w7FF–2s¢°Ð¢wW'6öæs¢&VE÷W'6öæ‚’ÀÐ¢w7F&ÆUöæ÷FRs¢'V–ÆE÷7F&ÆUöæ÷FR‚’ÀÐ¢w6fUö–ç7G"s¢ô45õ4dUô”å5E"ÀÐ¢ÒÀÐ¢w7FFRs¢ö65ö6öÆÆV7E÷7FFR†vWEöF"’ÀÐ¢vöæU÷6†÷Bs¢ö65ö6öÆÆV7EööæU÷6†÷B†vWEöF"Â–æ6ÇVFU÷v¶SÖ–æ6ÇVFU÷v¶R’ÀÐ¢ÐÐ¢–b–æ6ÇVFUö6öÆC Ð¢÷WE²v6öÆEööæ6RuÒÒö65ö6öÆÆV7Eö6öÆEööæ6R†vWEöF"Ð¢VÇ6S Ð¢÷WE²v6öÆEööæ6RuÒÒ·ÐÐ¢&WGW&â÷W@Ð Ð Ð¦FVbf÷&ÖE÷7FFUöF–fb†öÆE÷7FFRÂæWu÷7FFR“ Ð¢"".jùN‹è>KŠNKŠ¢7FFRF–7NûÈÎ‹ùNY¹î[zî˜xþih~iÊÎûÉ¾izXùŽXÉn‹ùNY¹îz›®K‹.8""" Ð¢öÆE÷7FFRÒöÆE÷7FFR÷"·ÐÐ¢æWu÷7FFRÒæWu÷7FFR÷"·ÐÐ¢Æ–æW2ÒµÐÐ¢¶W—2ÒÆ—7B†F–7Bæg&öÖ¶W—2†Æ—7B†öÆE÷7FFRæ¶W—2‚’’²Æ—7B†æWu÷7FFRæ¶W—2‚’’’Ð¢f÷"¶W’–â¶W—3 Ð¢&Vf÷&RÒ†öÆE÷7FFRævWB†¶W’’÷"rr’ç7G&—‚Ð¢gFW"Ò†æWu÷7FFRævWB†¶W’’÷"rr’ç7G&—‚Ð¢–b&Vf÷&RÓÒgFW# Ð¢6öçF–çVPÐ¢Æ&VÂÒ°Ð¢vVÖ÷F–öâs¢~h8^{º¢rÀÐ¢vG&—fRs¢~š›Xª‚rÀÐ¢vÆ–v‡G2s¢~xòrÀÐ¢wö6¶WBs¢uö6¶WBrÀÐ¢wFöF÷2s¢~yYžŠˆiÛþ[è^X©ârÀÐ¢vÆVFvW"s¢~Šë‹JbrÀÐ¢w&VÖ–æFW'2s¢~K¸®iz^hù˜i"rÀÐ¢w&V6VçEö7F—f—G’s¢~iÈ‹ùkK¾Xª‚rÀÐ¢ÒævWB†¶W’Â¶W’Ð¢–b&Vf÷&RæBæ÷BgFW# Ð¢Æ–æW2æVæB†brÒ¶Æ&VÇÞûÉ®[{.kˆ^z›¢rÐ¢VÆ–bæ÷B&Vf÷&RæBgFW# Ð¢Æ–æW2æVæB†brÒ¶Æ&VÇÞûÉ§¶gFW'ÒrÐ¢VÇ6S Ð¢2yúÞZÙ~jë^ûÈŽxòõö6¶WNûÈžyJ‚&Vf÷&R(i"gFW.ûÉ¾™[þZÙ~jë^Xú®hªRgFW Ð¢–b¶W’–â‚vÆ–v‡G2rÂwö6¶WBr’æBÆVâ†&Vf÷&R’ÂƒæBÆVâ†gFW"’Âƒ Ð¢Æ–æW2æVæB†brÒ¶Æ&VÇÞûÉ§¶&Vf÷&WÒ(i"¶gFW'ÒrÐ¢VÇ6S Ð¢Æ–æW2æVæB†brÒ¶Æ&VÇÞûÉ§¶gFW'ÒrÐ¢–bæ÷BÆ–æW3 Ð¢&WGW&ârpÐ¢&WGW&â~8	x«nhi»Nik8	Æâr²uÆâræ¦ö–â†Æ–æW2Ð Ð Ð¦FVbf÷&ÖE÷7FFU÷6æ6†÷B‡7FFR“ Ð¢7FFRÒ7FFR÷"·ÐÐ¢6‡Væ·2Ò·bç7G&—‚’f÷"b–â7FFRçfÇVW2‚’–bbæB7G"‡b’ç7G&—‚•ÐÐ¢–bæ÷B6‡Væ·3 Ð¢&WGW&ârpÐ¢&WGW&â~8	[Ù>X˜Þx«nh8	Æâr²uÆåÆâræ¦ö–â†6‡Væ·2Ð Ð Ð¦FVbf÷&ÖEö6öÆEööæ6R†6öÆB“ Ð¢6öÆBÒ6öÆB÷"·ÐÐ¢6‡Væ·2Ò·bç7G&—‚’f÷"b–â6öÆBçfÇVW2‚’–bbæB7G"‡b’ç7G&—‚•ÐÐ¢&WGW&âuÆåÆâræ¦ö–â†6‡Væ·2Ð Ð Ð¢2v¶U÷&WÇ•ö'&–FvRKˆÞ‹ù²f÷&ÖEööæU÷6†÷NûÉ®yKvFWv’{J~‹KN[Ù>X˜ÞyJŽh‹~‹ÚîXÙ^xºÎk:ŽXZ^8 Ð¥ôôäUõ4„õEõDU…Eô´U•2Ò‚wF6µöfVVF&6²rÂvG&VÕöfÆ6‚rÐ Ð Ð¦FVbf÷&ÖEööæU÷6†÷B†öæU÷6†÷B“ Ð¢öæU÷6†÷BÒöæU÷6†÷B÷"·ÐÐ¢6‡Væ·2ÒµÐÐ¢wV&BÒ†öæU÷6†÷BævWB‚wv¶U÷&W6öÇWF–öåöwV&Br’÷"rr’ç7G&—‚Ð¢–bwV&C Ð¢6‡Væ·2æVæB†wV&BÐ¢&u÷'G2ÒµÐÐ¢f÷"¶W’–â‚wv¶UöæöæÖW76vUö&6¶w&÷VæBrÂwv¶UöÖW76vUö&6¶w&÷VæBr“ Ð¢fÂÒ†öæU÷6†÷BævWB†¶W’’÷"rr’ç7G&—‚Ð¢–bfÃ Ð¢&u÷'G2æVæB‡fÂÐ¢–b&u÷'G3 Ð¢6‡Væ·2æVæB‚r22KÚ˜i.yØy¨Ni{nX	•Æâr²uÆâræ¦ö–â†&u÷'G2’Ð¢f÷"¶W’–âôôäUõ4„õEõDU…Eô´U•3 Ð¢fÂÒöæU÷6†÷BævWB†¶W’Ð¢–bfÂæB7G"‡fÂ’ç7G&—‚“ Ð¢6‡Væ·2æVæB‡7G"‡fÂ’ç7G&—‚’Ð¢&WGW&âuÆåÆâræ¦ö–â†6‡Væ·2Ð