"""Local JLCPCB parts cache backed by SQLite.

The full JLCPCB catalogue is large; this keeps a local SQLite cache and offers
search / lookup / alternative-suggestion over it. ``download`` fetches a
prebuilt SQLite database from a URL (``--url`` or ``JLCPCB_DB_URL``); if none is
given it creates an empty, correctly-shaped database so the other tools work
against a cache you can populate yourself.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def db_path() -> Path:
    base = os.environ.get("KICAD_MCP_HOME", str(Path.home() / ".kicad_mcp_py"))
    return Path(base) / "jlcpcb.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    lcsc TEXT PRIMARY KEY,
    mpn TEXT,
    description TEXT,
    package TEXT,
    category TEXT,
    value TEXT,
    stock INTEGER,
    price REAL,
    datasheet TEXT,
    basic INTEGER
);
CREATE INDEX IF NOT EXISTS idx_parts_mpn ON parts(mpn);
CREATE INDEX IF NOT EXISTS idx_parts_value ON parts(value);
CREATE INDEX IF NOT EXISTS idx_parts_desc ON parts(description);
"""


def _connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def ensure_db() -> Path:
    _connect().close()
    return db_path()


def download(url: Optional[str] = None) -> Dict[str, Any]:
    url = url or os.environ.get("JLCPCB_DB_URL")
    if not url:
        ensure_db()
        return {"ok": True, "downloaded": False,
                "note": "no URL given; created an empty cache at " + str(db_path()),
                "path": str(db_path())}
    import urllib.request
    target = db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        urllib.request.urlretrieve(url, str(target))  # noqa: S310 (user-provided URL)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "url": url}
    # Make sure the schema exists even if the download was a raw db.
    _connect().close()
    return {"ok": True, "downloaded": True, "path": str(target), "url": url}


def _row(r: sqlite3.Row) -> Dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def search(query: str, limit: int = 25) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        like = f"%{query}%"
        cur = conn.execute(
            "SELECT * FROM parts WHERE mpn LIKE ? OR description LIKE ? OR value LIKE ? "
            "ORDER BY basic DESC, stock DESC LIMIT ?",
            (like, like, like, limit),
        )
        return [_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_part(lcsc: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.execute("SELECT * FROM parts WHERE lcsc = ?", (lcsc,))
        r = cur.fetchone()
        return _row(r) if r else None
    finally:
        conn.close()


def suggest_alternatives(value: str, package: Optional[str] = None,
                         limit: int = 10) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        if package:
            cur = conn.execute(
                "SELECT * FROM parts WHERE value = ? AND package = ? "
                "ORDER BY basic DESC, stock DESC LIMIT ?", (value, package, limit))
        else:
            cur = conn.execute(
                "SELECT * FROM parts WHERE value = ? ORDER BY basic DESC, stock DESC LIMIT ?",
                (value, limit))
        return [_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


def stats() -> Dict[str, Any]:
    p = db_path()
    if not p.exists():
        return {"exists": False, "path": str(p)}
    conn = _connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
    finally:
        conn.close()
    return {"exists": True, "path": str(p), "part_count": count,
            "size_bytes": p.stat().st_size, "modified": p.stat().st_mtime}


def datasheet_for(mpn_or_lcsc: str) -> Optional[str]:
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT datasheet FROM parts WHERE lcsc = ? OR mpn = ? LIMIT 1",
            (mpn_or_lcsc, mpn_or_lcsc))
        r = cur.fetchone()
        return r["datasheet"] if r and r["datasheet"] else None
    finally:
        conn.close()
