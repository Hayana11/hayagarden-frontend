"""Provider-neutral Gallery operations shared by legacy and capability paths."""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from chat.gallery_provenance import TrustedGalleryTurn


class GalleryServiceError(RuntimeError):
    pass


def _db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def describe_gallery_original(pid: str, *, question: str | None = None) -> str | None:
    try:
        import gallery_store
        from chat.gallery_visual import describe_image_bytes
        photo = gallery_store.read_photo_bytes(pid)
        if not photo:
            return None
        data, mime = photo
        return describe_image_bytes(data, mime, question=question, timeout=15)
    except Exception:
        return None


def generate_gallery_meaning(
    *, db_path: str, note: str, first_impression: str,
    visual_description: str, source_msg_id: int,
) -> dict[str, Any] | None:
    source_text = ""
    conn = None
    try:
        conn = _db(db_path)
        row = conn.execute(
            "SELECT author, content FROM chat_messages WHERE id=?",
            (int(source_msg_id),),
        ).fetchone()
        if row:
            who = "哈娅" if str(row["author"]).lower() in {"hayana", "haya", "user"} else "费佳"
            source_text = "%s：%s" % (who, str(row["content"] or "").strip()[:500])
    except Exception:
        source_text = ""
    finally:
        if conn is not None:
            conn.close()
    system = (
        "你是费奥多尔，正在整理一条关系性照片记忆。你没有在本次调用中看到图片像素；"
        "只能参考明确提供的中性画面描述、当时第一印象、备注和绑定的来源消息。"
        "summary 写这张图为何在那一刻值得留下，不要把 visual_description 改写成记忆摘要。"
        "严格只输出 JSON："
        '{"summary":"一句克制的第一人称关系性记忆",'
        '"emotion":"一个情绪词","keywords":["3到6个检索词"],"importance":0到100的整数}'
    )
    prompt = (
        "备注：%s\n那时的第一印象：%s\n中性画面描述：%s\n与图片同条的来源消息：%s"
        % (
            str(note or "")[:500] or "（无）",
            str(first_impression or "")[:800] or "（无）",
            str(visual_description or "")[:1000] or "（尚无）",
            source_text or "（未能读取精确来源消息）",
        )
    )
    try:
        from relay.manager import relay
        from chat.response_parser import extract_text
        result = relay.call(
            {"max_tokens": 400, "system": system, "messages": [{"role": "user", "content": prompt}]},
            timeout=25,
            use_ws_model=True,
        )
        match = re.search(r"\{.*\}", extract_text(result) or "", re.S)
        if not match:
            return None
        parsed = json.loads(match.group(0))
        keywords = parsed.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [item for item in re.split(r"[,，\s]+", keywords) if item]
        return {
            "summary": str(parsed.get("summary") or "").strip()[:200],
            "emotion": str(parsed.get("emotion") or "").strip()[:10],
            "keywords": [str(item).strip()[:16] for item in keywords if str(item).strip()][:6],
            "importance": max(0, min(100, int(parsed.get("importance", 50) or 50))),
        }
    except Exception:
        return None


def save_gallery_image(
    turn: TrustedGalleryTurn,
    *,
    db_path: str,
    attachment: str = "",
    image_index: Any = None,
    note: str = "",
    album: str | None = None,
    first_impression: str = "",
    describe_fn: Callable[..., str | None] = describe_gallery_original,
    meaning_fn: Callable[..., dict[str, Any] | None] = generate_gallery_meaning,
) -> dict[str, Any]:
    """Persist one trusted image.  Post-save enrichment can fail without rollback."""
    import gallery_store
    note = str(note or "")[:500]
    album = str(album or "").strip()[:80] or None
    first_impression = str(first_impression or "").strip()[:800]
    if attachment:
        if image_index is not None:
            raise GalleryServiceError("attachment and image_index are mutually exclusive")
        result = gallery_store.save_attachment(
            attachment,
            note=note,
            album_name=album,
            source_type="chat",
            source_msg_id=turn.request_message_id,
            source_chat_id=turn.chat_id,
            first_impression=first_impression,
        )
    else:
        from chat.cc_vision_bridge import resolve_image_bytes
        from chat.gallery_context import resolve_current_turn_image
        selected = resolve_current_turn_image(
            turn.request_message_id,
            turn.chat_id,
            get_db_fn=lambda: _db(db_path),
            image_index=image_index,
        )
        attachment_root = Path(
            os.environ.get("HAYAGARDEN_ATTACHMENTS_ROOT") or "/opt/frontend"
        ).resolve()
        image_bytes, mime = resolve_image_bytes(
            selected["ref"],
            upload_dir=str(attachment_root / "static" / "uploads"),
            attach_dir=str(attachment_root / "attachments"),
        )
        result = gallery_store.save_image_bytes(
            image_bytes,
            mime,
            note=note,
            album_name=album,
            source_type="chat",
            source_msg_id=turn.request_message_id,
            source_chat_id=turn.chat_id,
            first_impression=first_impression,
        )
    if not result:
        raise GalleryServiceError("trusted image is unavailable")
    pid = str(result["pid"])
    existing = gallery_store.get(pid) or {}
    if result.get("reused_existing"):
        return {
            "ok": True,
            "pid": pid,
            "gallery_ref": "gallery://" + pid,
            "reused_existing": True,
            "metadata_overwritten": False,
            "source_msg_id": existing.get("source_msg_id"),
            "source_chat_id": existing.get("source_chat_id"),
            "visual_description_available": bool(existing.get("visual_description")),
        }

    visual_description = describe_fn(pid)
    if visual_description:
        try:
            gallery_store.set_meaning(pid, visual_description=visual_description)
        except Exception:
            visual_description = None
    meaning = meaning_fn(
        db_path=db_path,
        note=note,
        first_impression=first_impression,
        visual_description=visual_description or "",
        source_msg_id=turn.request_message_id,
    )
    summary = ""
    if meaning and meaning.get("summary"):
        try:
            from tools import memory_tool
            tags = ("gallery:%s " % pid) + " ".join(meaning.get("keywords") or ())
            if meaning.get("emotion"):
                tags += " " + str(meaning["emotion"])
            mem_id = memory_tool.save_memory(
                content=meaning["summary"],
                type="PHOTO",
                author="fyodor",
                layer="long-term",
                tags=tags.strip(),
                importance=meaning.get("importance", 50),
            )
            gallery_store.set_meaning(
                pid,
                summary=meaning["summary"],
                emotion=meaning.get("emotion", ""),
                keywords=meaning.get("keywords", []),
                importance=meaning.get("importance", 50),
                mem_id=mem_id,
            )
            summary = str(meaning["summary"])
        except Exception:
            pass
    return {
        "ok": True,
        "pid": pid,
        "gallery_ref": "gallery://" + pid,
        "reused_existing": False,
        "source_msg_id": turn.request_message_id,
        "source_chat_id": turn.chat_id,
        "visual_description_available": bool(visual_description),
        "first_impression_saved": bool(first_impression),
        "summary_available": bool(summary),
    }


def recall_gallery_photo(
    *, keyword: str | None = None, emotion: str | None = None,
    pid: str | None = None, inspect_question: str | None = None,
    record_selection: bool = False, touch_memory: bool = False,
    strict: bool = True,
    describe_fn: Callable[..., str | None] = describe_gallery_original,
) -> dict[str, Any] | None:
    """Read Gallery memory.  Capability callers keep both write flags false."""
    import gallery_store
    photo = (
        gallery_store.get(str(pid).strip())
        if pid
        else gallery_store.pick_for_recall(keyword=keyword, emotion=emotion, strict=strict)
    )
    if not photo:
        return None
    if record_selection:
        gallery_store.mark_sent(photo["pid"])
    if touch_memory and photo.get("mem_id"):
        try:
            from tools import memory_tool
            memory_tool.touch_memories([photo["mem_id"]])
        except Exception:
            pass
    try:
        keywords = json.loads(photo.get("keywords") or "[]")
    except Exception:
        keywords = []
    question = str(inspect_question or "").strip()[:500]
    inspected = describe_fn(photo["pid"], question=question) if question else None
    return {
        "pid": photo["pid"],
        "summary": photo.get("summary") or "",
        "visual_description": photo.get("visual_description") or "",
        "first_impression": photo.get("first_impression") or "",
        "emotion": photo.get("emotion") or "",
        "keywords": keywords,
        "source_msg_id": photo.get("source_msg_id"),
        "source_chat_id": photo.get("source_chat_id"),
        "semantic_memory_only": not bool(inspected),
        "original_reloaded": bool(inspected),
        "inspection_answer": inspected or None,
        "delivery_marker": "[[gallery:%s]]" % photo["pid"],
    }


@contextlib.contextmanager
def browser_singleflight(lock_path: str | Path, *, timeout: float = 70.0):
    """Cross-process Chromium one-shot lock; kernel releases it on process exit."""
    try:
        import fcntl
    except ImportError as exc:
        raise GalleryServiceError("browser file locking is unavailable") from exc
    target = Path(lock_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+b")
    deadline = time.monotonic() + max(0.0, float(timeout))
    acquired = False
    try:
        while not acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GalleryServiceError("browser is busy")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def screenshot_chat(
    viewpoint: str = "fyodor",
    *,
    repo_root: str | Path | None = None,
    attachments_root: str | Path | None = None,
    timeout: float = 42.0,
    lock_timeout: float = 5.0,
    run_fn: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    view = str(viewpoint or "").strip().lower()
    if view not in {"fyodor", "hayana"}:
        raise GalleryServiceError("viewpoint must be fyodor or hayana")
    root = Path(repo_root or os.environ.get("UH_A0_REPO_ROOT") or "/opt/frontend").resolve()
    attachment_root = Path(
        attachments_root or os.environ.get("HAYAGARDEN_ATTACHMENTS_ROOT") or "/opt/frontend"
    ).resolve()
    lock_path = attachment_root / ".gallery-browser.lock"
    url = "http://127.0.0.1:5050/chat?shot=1"
    if view == "fyodor":
        url += "&as=me"
    with browser_singleflight(lock_path, timeout=lock_timeout):
        try:
            proc = run_fn(
                ["node", str(root / "tools" / "browser.js"), "shot", url],
                capture_output=True,
                text=True,
                timeout=float(timeout),
            )
        except subprocess.TimeoutExpired as exc:
            raise GalleryServiceError("screenshot timed out") from exc
        output = str(getattr(proc, "stdout", "") or "").strip()
        if not output:
            raise GalleryServiceError(
                "screenshot produced no output: "
                + str(getattr(proc, "stderr", "") or "")[:200]
            )
        try:
            payload = json.loads(output.splitlines()[-1])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GalleryServiceError("screenshot output is invalid") from exc
        if payload.get("ok") is not True or not payload.get("shot"):
            raise GalleryServiceError("screenshot failed: " + str(payload.get("error") or "")[:200])
        import attachment_store
        aid = attachment_store.save(str(payload["shot"]), kind="image", mime="image/png")
    if not aid:
        raise GalleryServiceError("screenshot attachment registration failed")
    return {
        "ok": True,
        "viewpoint": view,
        "attachment": "attachment://" + str(aid),
    }


__all__ = [
    "GalleryServiceError",
    "browser_singleflight",
    "describe_gallery_original",
    "generate_gallery_meaning",
    "recall_gallery_photo",
    "save_gallery_image",
    "screenshot_chat",
]

