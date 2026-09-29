"""Read-only MEMORY-INTEROP adapter for the legacy ``posts`` memory store.

R1 is an offline shadow translator.  It converts already-stored legacy rows
into Evidence-only SubmissionEnvelope values and into ContextBundle items.
It does not review, accept, commit, or persist Kernel records, and it does
not mutate the legacy store.

Authority rule: legacy ``posts`` rows do not reliably distinguish a raw user
source from summaries, dreams, model-derived text, or imported memory.
Translation therefore always sets ``origin_kind="derived"``.  That means
"the legacy system stored this text", not "the text is objectively true"
and not "the user directly said this".  Semantic interpretation into State
or Delta is later Processor/Review work, not this adapter's job.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import quote

from tools.memory_interop import (
    MEMORY_INTEROP_PROTOCOL_VERSION,
    AdapterDescriptor,
    ContextBundle,
    ContextItem,
    InteropRequestContext,
    SubmissionEnvelope,
)
from tools.memory_kernel import Evidence
from tools.product_handlers import search_memory_posts


LEGACY_POSTS_ADAPTER_ID = "legacy.posts.v1"
LEGACY_POSTS_ADAPTER_VERSION = "1"
LEGACY_POSTS_SOURCE_TYPE = "legacy.posts"
LEGACY_POSTS_TABLE = "posts"
_LEGACY_SOURCE_REF_PREFIX = "legacy://posts/"
_PROVENANCE_FIELDS = (
    "type",
    "author",
    "layer",
    "tags",
    "pinned",
    "resolved",
    "importance",
    "created_at",
)
_CONTEXT_EXTRA_FIELDS = ("recall_count", "last_recalled_at")

LEGACY_POSTS_DESCRIPTOR = AdapterDescriptor(
    adapter_id=LEGACY_POSTS_ADAPTER_ID,
    adapter_version=LEGACY_POSTS_ADAPTER_VERSION,
    protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
    supported_operations=("ingest", "retrieve", "context_contribute"),
    readable=True,
    writable=False,
    supported_capabilities=("memory.search", "memory.write"),
    metadata={
        "store": LEGACY_POSTS_TABLE,
        "mode": "offline_shadow",
        "origin_kind_default": "derived",
        "write_support": (
            "translates an already-stored legacy row; does not persist the row"
        ),
    },
)


def _text_id(value: Any, name: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} is required")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{name} is required")


def _row_mapping(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {str(key): row[key] for key in row.keys()}
    if isinstance(row, Mapping):
        return {str(key): row[key] for key in row.keys()}
    raise TypeError("legacy row must be a mapping")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("legacy field is not lossless JSON")
        return value
    raise ValueError("legacy field is not lossless JSON")


def _copy_present_fields(
    row: Mapping[str, Any],
    names: tuple[str, ...],
) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for name in names:
        if name not in row:
            continue
        copied[name] = _json_safe(row[name])
    return copied


def _legacy_source_ref(row_id: str) -> str:
    return f"{_LEGACY_SOURCE_REF_PREFIX}{row_id}"


def _occurred_at_from_legacy(raw: Any) -> str | None:
    """Return the raw timestamp only when it is already Kernel-valid.

    Naive SQLite datetimes stay in provenance and do not become occurred_at.
    This helper never infers +08:00 or any other timezone.
    """

    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return raw


def _legacy_row_provenance(row: Mapping[str, Any], row_id: str) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "legacy_table": LEGACY_POSTS_TABLE,
        "legacy_row_id": _json_safe(row["id"]) if "id" in row else row_id,
        "authority": "legacy_store_only",
        "origin_kind_default": "derived",
        "origin_kind_reason": (
            "legacy posts do not distinguish raw user source from derived text"
        ),
    }
    provenance.update(_copy_present_fields(row, _PROVENANCE_FIELDS))
    return provenance


def legacy_post_to_evidence(
    row: Mapping[str, Any] | sqlite3.Row,
    *,
    observed_at: str,
) -> Evidence:
    """Translate one already-stored legacy post into derived Evidence."""

    mapping = _row_mapping(row)
    row_id = _text_id(mapping.get("id"), "legacy row id")
    content = mapping.get("content")
    if not isinstance(content, str):
        raise ValueError("legacy content must be exact text")
    return Evidence(
        evidence_id=f"legacy.posts.{row_id}",
        source_type=LEGACY_POSTS_SOURCE_TYPE,
        source_ref=_legacy_source_ref(row_id),
        observed_at=observed_at,
        provenance=_legacy_row_provenance(mapping, row_id),
        origin_kind="derived",
        occurred_at=_occurred_at_from_legacy(mapping.get("created_at")),
        content=content,
    )


def legacy_post_to_submission_envelope(
    row: Mapping[str, Any] | sqlite3.Row,
    *,
    request_context: InteropRequestContext,
    submission_id: str,
    idempotency_key: str,
    submitted_at: str,
    observed_at: str,
) -> SubmissionEnvelope:
    """Wrap one already-stored legacy post as an Evidence-only envelope.

    ``memory.write`` support here is translation of an existing row, not a
    write of that row.  The envelope is never reviewed, accepted, or committed.
    """

    if type(request_context) is not InteropRequestContext:
        raise TypeError("request_context must be InteropRequestContext")
    if request_context.adapter_id != LEGACY_POSTS_ADAPTER_ID:
        raise ValueError("request_context adapter_id does not match legacy adapter")
    mapping = _row_mapping(row)
    row_id = _text_id(mapping.get("id"), "legacy row id")
    evidence = legacy_post_to_evidence(mapping, observed_at=observed_at)
    return SubmissionEnvelope(
        protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
        submission_id=submission_id,
        idempotency_key=idempotency_key,
        adapter_id=LEGACY_POSTS_ADAPTER_ID,
        request_context=request_context,
        evidence=(evidence,),
        candidate_states=(),
        candidate_deltas=(),
        submitted_at=submitted_at,
        provenance={
            "kind": "legacy_shadow_translation",
            "adapter_id": LEGACY_POSTS_ADAPTER_ID,
            "legacy_table": LEGACY_POSTS_TABLE,
            "legacy_row_id": _json_safe(mapping["id"]) if "id" in mapping else row_id,
            "writable": False,
        },
    )


def open_legacy_posts_readonly(db_path: str) -> sqlite3.Connection:
    """Open the explicit legacy SQLite file in URI ``mode=ro``."""

    path = str(db_path or "").strip()
    if not path:
        raise ValueError("explicit db_path is required")
    resolved = Path(path).expanduser().resolve()
    uri = f"file:{quote(resolved.as_posix(), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


@contextmanager
def legacy_posts_readonly(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = open_legacy_posts_readonly(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _legacy_context_item(
    row: Mapping[str, Any],
    *,
    position: int,
    keyword: str,
    limit: int,
    result_count: int,
) -> ContextItem:
    row_id = _text_id(row.get("id"), "legacy row id")
    content = row.get("content")
    if not isinstance(content, str):
        raise ValueError("legacy content must be exact text")
    provenance = _legacy_row_provenance(row, row_id)
    provenance.update(_copy_present_fields(row, _CONTEXT_EXTRA_FIELDS))
    provenance["translation"] = "legacy_shadow_retrieval"
    return ContextItem(
        item_id=f"legacy.posts.{row_id}",
        source_adapter_id=LEGACY_POSTS_ADAPTER_ID,
        provenance=provenance,
        content=content,
        source_refs=(_legacy_source_ref(row_id),),
        kernel_refs=(),
        retrieval_reason="legacy memory.search shadow",
        selection_metadata={
            "legacy_search": {
                "keyword": keyword,
                "limit": limit,
                "position": position,
                "result_count": result_count,
                "order": "id DESC",
            }
        },
    )


def retrieve_legacy_posts_context_bundle(
    db_path: str,
    *,
    keyword: Any = "",
    request_context: InteropRequestContext,
    bundle_id: str,
    generated_at: str,
    limit: Any = 8,
) -> ContextBundle:
    """Shadow-retrieve through the authoritative ``search_memory_posts`` handler.

    Ordering, limit, DIARY inclusion, resolved-row inclusion, empty-query, and
    Unicode behavior are exactly those of the current legacy search handler.
    This function does not rerank, filter further, or heat recall counters.
    """

    if type(request_context) is not InteropRequestContext:
        raise TypeError("request_context must be InteropRequestContext")
    search = keyword if isinstance(keyword, str) else ""
    limit_n = min(int(limit), 1000)
    with legacy_posts_readonly(db_path) as conn:
        rows = search_memory_posts(conn, keyword=search, limit=limit_n)
    items = tuple(
        _legacy_context_item(
            row,
            position=index,
            keyword=search,
            limit=limit_n,
            result_count=len(rows),
        )
        for index, row in enumerate(rows)
    )
    return ContextBundle(
        bundle_id=bundle_id,
        protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
        generated_at=generated_at,
        contributor_adapter_ids=(LEGACY_POSTS_ADAPTER_ID,),
        items=items,
        request_id=request_context.request_id,
        turn_id=request_context.turn_id,
        metadata={
            "legacy_search": {
                "keyword": search,
                "limit": limit_n,
                "handler": "tools.product_handlers.search_memory_posts",
            }
        },
    )


def shadow_compare_legacy_search(
    db_path: str,
    *,
    keyword: Any = "",
    request_context: InteropRequestContext,
    bundle_id: str,
    generated_at: str,
    limit: Any = 8,
) -> dict[str, Any]:
    """Offline structural parity helper.  Production must not invoke this.

    Compares the current authoritative ``search_memory_posts`` rows against the
    adapter ContextBundle for the same explicit fixture database and query.
    """

    search = keyword if isinstance(keyword, str) else ""
    limit_n = min(int(limit), 1000)
    with legacy_posts_readonly(db_path) as conn:
        rows = search_memory_posts(conn, keyword=search, limit=limit_n)
    bundle = retrieve_legacy_posts_context_bundle(
        db_path,
        keyword=search,
        request_context=request_context,
        bundle_id=bundle_id,
        generated_at=generated_at,
        limit=limit_n,
    )
    row_ids = [_text_id(row.get("id"), "legacy row id") for row in rows]
    item_ids = [item.item_id.removeprefix("legacy.posts.") for item in bundle.items]
    contents = [row.get("content") for row in rows]
    item_contents = [item.content for item in bundle.items]
    source_refs = [
        item.source_refs[0] if item.source_refs else None for item in bundle.items
    ]
    expected_refs = [_legacy_source_ref(row_id) for row_id in row_ids]
    return {
        "row_ids": row_ids,
        "item_ids": item_ids,
        "contents": contents,
        "item_contents": item_contents,
        "source_refs": source_refs,
        "same_ids": row_ids == item_ids,
        "same_order": row_ids == item_ids,
        "same_content": contents == item_contents,
        "same_count": len(rows) == len(bundle.items),
        "same_source_refs": source_refs == expected_refs,
        "contributor_adapter_ids": list(bundle.contributor_adapter_ids),
    }


__all__ = [
    "LEGACY_POSTS_ADAPTER_ID",
    "LEGACY_POSTS_ADAPTER_VERSION",
    "LEGACY_POSTS_DESCRIPTOR",
    "LEGACY_POSTS_SOURCE_TYPE",
    "legacy_post_to_evidence",
    "legacy_post_to_submission_envelope",
    "legacy_posts_readonly",
    "open_legacy_posts_readonly",
    "retrieve_legacy_posts_context_bundle",
    "shadow_compare_legacy_search",
]
