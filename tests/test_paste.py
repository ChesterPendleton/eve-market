from __future__ import annotations

from pathlib import Path

from eve_market import clipboard as clip
from eve_market.marketlog import load_directory, parse_marketlog

# The header the EVE client writes when exporting a market log.
MARKETLOG_HEADER = (
    "price,volRemaining,typeID,range,orderID,volEntered,minVolume,bid,"
    "issueDate,duration,stationID,regionID,solarSystemID,jumps"
)


def test_multibuy_uses_tabs_so_multiword_names_survive():
    text = clip.multibuy_list([("Small Shield Extender II", 25)])
    assert text == "Small Shield Extender II\t25"


def test_multibuy_skips_zero_and_negative_quantities():
    text = clip.multibuy_list([("Tritanium", 100), ("PLEX", 0), ("Pyerite", -5)])
    assert text == "Tritanium\t100"


def test_multibuy_of_nothing_is_empty():
    assert clip.multibuy_list([]) == ""


def test_price_format_has_no_thousands_separator():
    """EVE's order dialog rejects '1,234.56'."""
    assert clip.format_price(1234.5) == "1234.50"
    assert clip.format_price(1_000_000.0) == "1000000.00"
    assert "," not in clip.format_price(9_999_999.99)


def test_try_copy_never_raises_when_no_clipboard_exists():
    # Headless CI has no clipboard backend; this must report False, not explode.
    assert clip.try_copy("test") in (True, False)


def _write_log(tmp_path: Path, name: str, rows: list[str]) -> Path:
    path = tmp_path / name
    path.write_text(MARKETLOG_HEADER + "\n" + "\n".join(rows) + "\n")
    return path


def test_parses_a_client_market_log(tmp_path: Path):
    path = _write_log(
        tmp_path,
        "Genesis-Tritanium-2026.08.16 1204.txt",
        [
            ("5.94,8000000,34,32767,6002,10000000,1,False,"
             "2026-08-11 14:02:00.000,90,60003760,10000002,30000142,0"),
            ("5.10,12000000,34,32767,6001,20000000,1,True,"
             "2026-08-10 09:15:00.000,90,60003760,10000002,30000142,0"),
        ],
    )
    orders = parse_marketlog(path)
    assert len(orders) == 2

    sell = next(o for o in orders if not o.is_buy_order)
    buy = next(o for o in orders if o.is_buy_order)
    assert sell.price == 5.94
    assert sell.type_id == 34
    assert sell.location_id == 60003760
    assert sell.system_id == 30000142
    assert buy.volume_remain == 12_000_000
    assert buy.issued.year == 2026


def test_malformed_rows_are_skipped_not_fatal(tmp_path: Path):
    path = _write_log(
        tmp_path,
        "Genesis-Tritanium-2026.08.16 1205.txt",
        [
            ("5.94,8000000,34,32767,6002,10000000,1,False,"
             "2026-08-11 14:02:00.000,90,60003760,10000002,30000142,0"),
            "not-a-price,x,,,,,,,,,,,,",
        ],
    )
    assert len(parse_marketlog(path)) == 1


def test_non_marketlog_csv_is_rejected(tmp_path: Path):
    path = tmp_path / "something-else.csv"
    path.write_text("alpha,beta\n1,2\n")
    import pytest

    with pytest.raises(ValueError, match="does not look like"):
        parse_marketlog(path)


def test_directory_load_keeps_only_the_newest_export_per_item(tmp_path: Path):
    import os
    import time

    old = _write_log(
        tmp_path,
        "Genesis-Tritanium-2026.08.16 1100.txt",
        ["1.00,1,34,32767,1,1,1,False,2026-08-11 14:02:00.000,90,60003760,1,30000142,0"],
    )
    new = _write_log(
        tmp_path,
        "Genesis-Tritanium-2026.08.16 1200.txt",
        ["2.00,1,34,32767,2,1,1,False,2026-08-11 14:02:00.000,90,60003760,1,30000142,0"],
    )
    # Make the modification times unambiguous.
    past = time.time() - 3600
    os.utime(old, (past, past))

    orders = load_directory(tmp_path)
    assert len(orders) == 1
    assert orders[0].price == 2.00
    assert new.exists()


def test_directory_load_of_empty_folder_is_empty(tmp_path: Path):
    assert load_directory(tmp_path) == []
