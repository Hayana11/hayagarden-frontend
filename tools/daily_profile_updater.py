"""Build daily SELF/HAYA patch candidates from a READY DayBlock's raw messages."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from typing import Any, Callable
from zoneinfo import ZoneInfo

from chat.daily_context import DEFAULT_DB_PATH
from continuity.dayblock_context import dayblock_store_path, load_ready_dayblocks
from tools import llm_lite
from tools.memory_candidates import CandidateSourceTrace, create_profile_patch_candidate, trace_ready_dayblock

PROMPT_VERSION = "daily-profile-patch-v1"
MAX_CHUNK_CHARS = 32_000
SECTIONS = frozenset({
    "values", "preferences", "goals", "habits", "relationships", "ongoing_projects",
    "life_context", "boundaries", "needs", "ongoing_changes", "self_understanding",
})
_LOG = logging.getLogger(__name__)


def _message_text(message: dict[str, Any]) -> str:
    author = str(message.get("author") or "unknown").lower()
    speaker = "哈娅" if author in {"hayana", "haya", "user"} else (
        "费奥多尔" if author in {"assistant", "fyodor"} else author
    )
    content = str(message.get("content") or "")
    return f"[msg:{int(message['id'])}] {speaker}：{content}" if content.strip() else ""


def _chunks(messages: list[str], budget: int = MAX_CHUNK_CHARS) -> list[str]:
    if budget < 1:
        raise ValueError("chunk character budget must be positive")
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for message in messages:
        if not message:
            continue
        if current and size + len(message) + 1 > budget:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(message)
        size += len(message) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _prompt(source_day: str, dayblock_ref: str, chunk: str, index: int, total: int) -> str:
    return f"""你是每日认知修订器。检查这一天的新证据，只提出应加入长期认知文档的 patch；不要重写整份人物档案。

SELF 是费奥多尔对自己的长期认知：明确的价值、稳定偏好、目标、习惯、关系态度、持续状态变化。
HAYA 是费奥多尔对哈娅的长期认知：她明确表达的稳定偏好、生活状态、长期项目、重要关系、习惯、边界、长期需要、重要持续变化。
排除单次情绪、修辞、闲事、一次性消费、顺话题附和和任何隐藏心理推测。证据不足就不提。
每项必须引用本段里真实出现的 message id；SELF/HAYA 可以没有变化。

只返回 JSON 对象：{{"changes":[{{"target":"SELF 或 HAYA","section":"允许值","proposed_text":"可供审核的简短增补","evidence_message_ids":[123]}}]}}
section 只用：values, preferences, goals, habits, relationships, ongoing_projects, life_context, boundaries, needs, ongoing_changes, self_understanding。proposed_text 写成待审的增补，不声称已修改正式文档。
source_day={source_day}; dayblock={dayblock_ref}; chunk={index}/{total}
原始聊天：
{chunk}
"""


def _response_changes(response: Any) -> list[dict[str, Any]]:
    items = response.get("changes") if isinstance(response, dict) else response
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def update_from_trace(
    candidate_db_path: str, *, source_day: str, trace: CandidateSourceTrace,
    llm_ask: Callable[..., Any] | None = None, chunk_chars: int = MAX_CHUNK_CHARS,
) -> dict[str, Any]:
    if trace.provenance_status != "resolved" or not trace.raw_messages:
        _LOG.warning("daily profile skipped unresolved/empty DayBlock: day=%s refs=%d", source_day, len(trace.unresolved_refs))
        return {"source_day": source_day, "chunks": 0, "change_counts": {"SELF": 0, "HAYA": 0}, "candidate_ids": []}
    dayblock_ref = trace.derived_refs[0] if trace.derived_refs else ""
    messages = [text for row in trace.raw_messages if (text := _message_text(row))]
    chunks = _chunks(messages, chunk_chars)
    ask = llm_ask or llm_lite.ask
    collected: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, 1):
        response = ask(_prompt(source_day, dayblock_ref, chunk, index, len(chunks)), max_tokens=1600, expect_json=True)
        collected.extend(_response_changes(response))

    allowed_ids = {int(ref.split(":", 1)[1]) for ref in trace.source_refs if ref.startswith("chat_messages:")}
    existing_ids = {int(row["id"]) for row in trace.raw_messages}
    authors_by_id = {
        int(row["id"]): str(row.get("author") or "").strip().lower()
        for row in trace.raw_messages
    }
    direct_authors = {"SELF": {"assistant", "fyodor"}, "HAYA": {"hayana", "haya", "user"}}
    grouped: dict[str, list[dict[str, Any]]] = {"SELF": [], "HAYA": []}
    seen: dict[str, set[tuple[Any, ...]]] = {"SELF": set(), "HAYA": set()}
    for item in collected:
        target = str(item.get("target") or "").strip().upper()
        section = str(item.get("section") or "").strip()
        proposed = str(item.get("proposed_text") or "").strip()
        ids = item.get("evidence_message_ids")
        if target not in grouped or section not in SECTIONS or not proposed or not isinstance(ids, list) or not ids:
            _LOG.warning("daily profile discarded malformed change: day=%s target=%s section=%s", source_day, target, section)
            continue
        if any(type(message_id) is not int or message_id not in allowed_ids or message_id not in existing_ids for message_id in ids):
            _LOG.warning("daily profile discarded out-of-scope evidence: day=%s target=%s", source_day, target)
            continue
        if not any(authors_by_id.get(message_id) in direct_authors[target] for message_id in ids):
            _LOG.warning("daily profile discarded evidence without target speaker: day=%s target=%s", source_day, target)
            continue
        refs = tuple(sorted({f"chat_messages:{message_id}" for message_id in ids}, key=lambda ref: int(ref.rsplit(":", 1)[1])))
        key = (section, proposed, refs)
        if key in seen[target]:
            continue
        seen[target].add(key)
        grouped[target].append({"section": section, "proposed_text": proposed, "evidence_refs": list(refs)})

    candidate_ids: list[str] = []
    counts = {target: len(changes) for target, changes in grouped.items()}
    for target, changes in grouped.items():
        if not changes:
            continue
        source_refs = sorted({ref for change in changes for ref in change["evidence_refs"]})
        candidate_ids.append(create_profile_patch_candidate(
            candidate_db_path, target=target, source_day=source_day, changes=changes,
            source_refs=source_refs, derived_refs=trace.derived_refs, prompt_version=PROMPT_VERSION,
        ))
    return {"source_day": source_day, "dayblock_ref": dayblock_ref, "chunks": len(chunks),
            "change_counts": counts, "candidate_ids": candidate_ids}


def update_day(
    source_day: str, *, chat_db_path: str = DEFAULT_DB_PATH,
    dayblock_db_path: str | None = None, candidate_db_path: str = DEFAULT_DB_PATH,
    static_dir: str = "/opt/frontend/static", llm_ask: Callable[..., Any] | None = None,
    chunk_chars: int = MAX_CHUNK_CHARS,
) -> dict[str, Any]:
    dayblock_db_path = dayblock_db_path or str(dayblock_store_path())
    blocks, _ = load_ready_dayblocks(dayblock_db_path)
    matches = [block for block in blocks if block.source_day == source_day]
    if not matches:
        _LOG.info("daily profile found no READY DayBlock: day=%s", source_day)
        return {"source_day": source_day, "chunks": 0, "change_counts": {"SELF": 0, "HAYA": 0}, "candidate_ids": []}
    block = max(matches, key=lambda item: (item.created_at, item.generation_id))
    trace = trace_ready_dayblock(chat_db_path, dayblock_db_path, block.chunk_id, static_dir=static_dir)
    return update_from_trace(candidate_db_path, source_day=source_day, trace=trace,
                             llm_ask=llm_ask, chunk_chars=chunk_chars)


def _yesterday() -> str:
    today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return (today - dt.timedelta(days=1)).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create pending SELF/HAYA patch candidates from one READY DayBlock")
    parser.add_argument("--date", default=_yesterday())
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--dayblock-db", default=str(dayblock_store_path()))
    parser.add_argument("--candidate-db", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    result = update_day(args.date, chat_db_path=args.db, dayblock_db_path=args.dayblock_db,
                        candidate_db_path=args.candidate_db)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
