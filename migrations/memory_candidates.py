"""Explicit, idempotent Memory Candidate schema migration."""
import argparse
import sqlite3
from tools.memory_candidates import ensure_schema

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--db-path", default="/opt/frontend/memories.db")
    with sqlite3.connect(parser.parse_args().db_path) as conn: ensure_schema(conn)
