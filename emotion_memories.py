"""Read and update per-memory valence/arousal through the Ombre adapter."""

from __future__ import annotations

from tools import ombre_adapter
from valence_scale import normalize_arousal, normalize_valence


def _emotion_label(v: float, a: float) -> str:
    if v >= 0.3 and a >= 0.60:
        return "喜悦"
    if v >= 0.3 and a >= 0.40:
        return "愉悦"
    if v >= 0.3:
        return "平静"
    if v >= -0.1 and a >= 0.65:
        return "兴奋"
    if v >= -0.1 and a < 0.35:
        return "松弛"
    if v < -0.3 and a >= 0.60:
        return "焦虑"
    if v < -0.3 and a >= 0.35:
        return "沉重"
    if v < -0.3:
        return "低落"
    return "迷离"


def _record_to_point(record: dict) -> dict:
    raw_v = record.get("valence", 0.5)
    raw_a = record.get("arousal", 0.3)
    valence = normalize_valence(float(raw_v), scale="unipolar")
    arousal = normalize_arousal(float(raw_a))
    domains = record.get("domain") or []
    return {
        "bucket_id": str(record.get("id") or ""),
        "time": str(record.get("last_active") or record.get("created") or "")[:10],
        "valence": round(valence, 2),
        "arousal": round(arousal, 2),
        "emotion": _emotion_label(valence, arousal),
        "note": str(record.get("content") or "").replace("[[", "").replace("]]", "").strip()[:80],
        "domain": "、".join(str(item) for item in domains),
        "scale": "bipolar",
    }


def list_memory_points(*, limit: int = 15) -> list[dict]:
    records = ombre_adapter.list_memory_records(
        bucket_type="dynamic",
        include_content=True,
        limit=limit,
        sort="last_active_desc",
    )
    return [_record_to_point(record) for record in records if record.get("id")]


def update_memory_point(bucket_id: str, valence: float, arousal: float) -> dict:
    normalized_id = str(bucket_id or "").strip()
    if not normalized_id or "/" in normalized_id or "\\" in normalized_id:
        raise ValueError("valid bucket_id required")
    result = ombre_adapter.update_memory_emotion(
        normalized_id,
        float(valence),
        float(arousal),
    )
    if result is False:
        raise RuntimeError("emotion update failed")
    record = ombre_adapter.get_memory_record(normalized_id)
    if not record:
        raise RuntimeError("updated emotion record unavailable")
    return _record_to_point(record)
