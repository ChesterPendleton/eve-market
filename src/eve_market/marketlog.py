"""Import market data exported by the EVE client.

The market window's export button writes a CSV per item into
``Documents/EVE/logs/Marketlogs/``. This matters for two reasons:

* **Player structures.** Citadel markets are invisible to unauthenticated ESI.
  If the destination hub is a citadel rather than an NPC station, the client
  export is the only way to see its book without SSO and docking rights.
* **Ground truth.** It's the client's own view of the market, so it settles
  any argument about whether a price is stale.

The export is a plain CSV; reading a file the client wrote is not automation.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from .esi.models import MarketOrder

# Column names as the client writes them.
_COLUMNS = {
    "price": "price",
    "volremaining": "volume_remain",
    "typeid": "type_id",
    "orderid": "order_id",
    "volentered": "volume_total",
    "minvolume": "min_volume",
    "bid": "is_buy_order",
    "issuedate": "issued",
    "duration": "duration",
    "stationid": "location_id",
    "solarsystemid": "system_id",
    "range": "range",
}


def _parse_issued(raw: str) -> datetime:
    """Parse the client's timestamp, which is not quite ISO 8601."""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(UTC)


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"true", "1", "yes"}


def parse_marketlog(path: Path) -> list[MarketOrder]:
    """Parse one exported market log into orders.

    Rows that can't be parsed are skipped rather than aborting the import — a
    single malformed line shouldn't cost you the whole file.
    """
    orders: list[MarketOrder] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return []
        mapping = {
            name: _COLUMNS[name.strip().lower()]
            for name in reader.fieldnames
            if name.strip().lower() in _COLUMNS
        }
        if "type_id" not in mapping.values() or "price" not in mapping.values():
            raise ValueError(f"{path.name} does not look like an EVE market log")

        for row in reader:
            record: dict[str, object] = {}
            try:
                for column, field in mapping.items():
                    raw = (row.get(column) or "").strip()
                    if raw == "":
                        continue
                    if field == "is_buy_order":
                        record[field] = _truthy(raw)
                    elif field == "issued":
                        record[field] = _parse_issued(raw)
                    elif field == "price":
                        record[field] = float(raw)
                    elif field == "range":
                        record[field] = raw
                    else:
                        record[field] = int(float(raw))
            except (ValueError, TypeError):
                # One unparseable cell shouldn't cost the whole export.
                continue
            record.setdefault("min_volume", 1)
            record.setdefault("duration", 90)
            record.setdefault("range", "station")
            try:
                orders.append(MarketOrder.model_validate(record))
            except Exception:  # noqa: BLE001 - skip unparseable rows
                continue
    return orders


def load_directory(directory: Path, newest_only: bool = True) -> list[MarketOrder]:
    """Load every market log in a directory.

    The client writes a new timestamped file each export, so by default only
    the newest file per item type is used and older dumps are ignored.
    """
    files = sorted(directory.glob("*.txt")) + sorted(directory.glob("*.csv"))
    if not files:
        return []

    if newest_only:
        # Filenames look like "Domain-Tritanium-2026.08.16 1204.txt"; the stem
        # up to the last dash identifies the item, the rest is the timestamp.
        newest: dict[str, Path] = {}
        for path in files:
            key = path.stem.rsplit("-", 1)[0]
            current = newest.get(key)
            if current is None or path.stat().st_mtime > current.stat().st_mtime:
                newest[key] = path
        files = sorted(newest.values())

    orders: list[MarketOrder] = []
    for path in files:
        try:
            orders.extend(parse_marketlog(path))
        except ValueError:
            continue
    return orders
