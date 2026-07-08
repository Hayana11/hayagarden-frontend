"""向量检索基础件（记忆升级·方案二A）——配置即用，不配置零开销。

runtime_config 旋钮（config_store）：
  EMBED_API_URL  OpenAI 兼容 embeddings 端点，如
                 https://api.siliconflow.cn/v1/embeddings（云端）
                 http://<mac-mini>:11434/v1/embeddings（Ollama 本地，Mac mini 到货后）
  EMBED_API_KEY  密钥（本地服务可留空）
  EMBED_MODEL    模型名，如 BAAI/bge-m3 / bge-m3
  EMBED_ENABLED  总开关（默认 false）

向量存 memory_vectors 表（migrate_memory_vectors.py 建表，家规#2：迁移脚本+备份先行），
float32 打包成 BLOB。VPS 无 numpy，余弦用 array 模块纯 python 算——
几千行 × 1024 维在 1 核上是几十毫秒级，够用。
"""
import json
import sqlite3
import urllib.request as _req
from array import array

import config_store

DB = '/opt/frontend/memories.db'


def enabled():
    return (config_store.get_bool('EMBED_ENABLED', False)
            and bool(config_store.get('EMBED_API_URL', '')))


def embed_texts(texts, timeout=15):
    """调 OpenAI 兼容 /embeddings。返回 list[list[float]]，失败返回 None。"""
    if not texts:
        return []
    url = config_store.get('EMBED_API_URL', '')
    if not url:
        return None
    model = config_store.get('EMBED_MODEL', 'BAAI/bge-m3')
    key = config_store.get('EMBED_API_KEY', '')
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    body = json.dumps({'model': model, 'input': list(texts)}).encode('utf-8')
    try:
        with _req.urlopen(_req.Request(url, data=body, method='POST', headers=headers),
                          timeout=timeout) as resp:
            data = json.load(resp)
        items = sorted(data.get('data', []), key=lambda d: d.get('index', 0))
        vecs = [it.get('embedding') for it in items]
        if len(vecs) != len(texts) or any(not v for v in vecs):
            return None
        return vecs
    except Exception:
        return None


def pack(vec):
    return array('f', vec).tobytes()


def unpack(blob):
    a = array('f')
    a.frombytes(blob)
    return a


def cosine(a, b):
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def store_vector(post_id, vec, model):
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute(
        "INSERT INTO memory_vectors (post_id, model, dim, vec, updated_at) "
        "VALUES (?,?,?,?,datetime('now','+8 hours')) "
        "ON CONFLICT(post_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
        "vec=excluded.vec, updated_at=excluded.updated_at",
        (post_id, model, len(vec), pack(vec)))
    conn.commit()
    conn.close()


def similar_posts(query_text, top_k=5, min_score=0.35):
    """query 向量化后对 memory_vectors 全量余弦，返回 [(post_id, score)]。
    未启用/没向量/API 失败都安静返回 []。"""
    if not enabled():
        return []
    model = config_store.get('EMBED_MODEL', 'BAAI/bge-m3')
    conn = sqlite3.connect(DB, timeout=10)
    try:
        rows = conn.execute(
            "SELECT post_id, vec FROM memory_vectors WHERE model=?", (model,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    qv = embed_texts([query_text[:1000]], timeout=8)
    if not qv:
        return []
    q = array('f', qv[0])
    scored = []
    for pid, blob in rows:
        try:
            s = cosine(q, unpack(blob))
        except Exception:
            continue
        if s >= min_score:
            scored.append((pid, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]
