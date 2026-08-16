"""Fetching and parsing the Static Data Export slice this tool needs.

Only the ``invTypes`` table matters here: item names, group ids and packaged
volumes. Fuzzwork stopped publishing per-table CSV dumps; what it serves today
is a whole-database SQLite dump at a stable URL, so that is what we fetch —
streamed and decompressed on the fly, then read with the stdlib sqlite3.
"""

from __future__ import annotations

import sqlite3
import urllib.request
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import settings

SDE_URL = "https://www.fuzzwork.co.uk/dump/latest-sqlite.db.gz"


def download_sde(
    dest: Path,
    url: str = SDE_URL,
    progress: Callable[[int], None] | None = None,
) -> None:
    """Stream the gzipped SQLite SDE to ``dest``, decompressing as it arrives.

    Streaming matters: the download is ~150MB compressed and ~500MB after,
    and holding either in memory would be rude to small machines.
    """
    req = urllib.request.Request(url, headers={"User-Agent": settings.user_agent})
    decompressor = zlib.decompressobj(31)  # 31 = expect gzip framing
    received = 0
    tmp = dest.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=1800) as resp, tmp.open("wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            received += len(chunk)
            out.write(decompressor.decompress(chunk))
            if progress is not None:
                progress(received)
        out.write(decompressor.flush())
    tmp.replace(dest)


def types_from_sqlite(path: Path) -> list[dict[str, Any]]:
    """Read the invTypes table into rows shaped for ``Database.upsert_types``.

    Column lookup is case-insensitive and missing columns become NULL, so a
    schema drift in the dump degrades a field rather than crashing the load.
    """
    con = sqlite3.connect(str(path))
    try:
        tables = [
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        table = next((t for t in tables if t.lower() == "invtypes"), None)
        if table is None:
            raise ValueError(
                "no invTypes table in the SDE; found: " + ", ".join(sorted(tables)[:15])
            )
        cols = {r[1].lower(): r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}

        def col(name: str) -> str:
            real = cols.get(name.lower())
            return f'"{real}"' if real else "NULL"

        query = (
            f"SELECT {col('typeID')}, {col('typeName')}, {col('groupID')}, "
            f"{col('volume')}, {col('packagedVolume')}, {col('published')} "
            f'FROM "{table}"'
        )
        rows: list[dict[str, Any]] = []
        for tid, name, gid, vol, pvol, pub in con.execute(query):
            if tid is None:
                continue
            volume = float(vol) if vol is not None else None
            rows.append(
                {
                    "type_id": int(tid),
                    "type_name": name or str(tid),
                    "group_id": int(gid) if gid is not None else None,
                    "volume": volume,
                    "packaged_volume": float(pvol) if pvol is not None else volume,
                    "published": bool(pub) if pub is not None else True,
                }
            )
        return rows
    finally:
        con.close()
