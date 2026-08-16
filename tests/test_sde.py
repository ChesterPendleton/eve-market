"""The SDE parser: reading invTypes out of Fuzzwork's SQLite dump."""

import sqlite3

import pytest

from eve_market import sde


@pytest.fixture()
def sqlite_sde(tmp_path):
    path = tmp_path / "sde.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE invTypes (typeID INT, groupID INT, typeName TEXT, "
        "volume REAL, packagedVolume REAL, published INT)"
    )
    con.executemany(
        "INSERT INTO invTypes VALUES (?,?,?,?,?,?)",
        [
            (3244, 76, "Warp Disruptor II", 5.0, 5.0, 1),
            (34, 18, "Tritanium", 0.01, 0.01, 1),
            (648, 380, "Badger", 20000.0, 2500.0, 1),  # ships pack down
            (9999, None, None, None, None, None),  # nulls everywhere
        ],
    )
    con.commit()
    con.close()
    return path


def test_reads_all_rows(sqlite_sde):
    rows = sde.types_from_sqlite(sqlite_sde)
    assert len(rows) == 4
    by_id = {r["type_id"]: r for r in rows}
    assert by_id[3244]["type_name"] == "Warp Disruptor II"
    assert by_id[3244]["packaged_volume"] == 5.0


def test_packaged_volume_preferred_over_volume(sqlite_sde):
    rows = {r["type_id"]: r for r in sde.types_from_sqlite(sqlite_sde)}
    # A ship's packaged volume is what fits in a hauler, not its hull volume.
    assert rows[648]["packaged_volume"] == 2500.0
    assert rows[648]["volume"] == 20000.0


def test_null_fields_degrade_not_crash(sqlite_sde):
    rows = {r["type_id"]: r for r in sde.types_from_sqlite(sqlite_sde)}
    ghost = rows[9999]
    assert ghost["type_name"] == "9999"  # falls back to the id as a name
    assert ghost["volume"] is None
    assert ghost["packaged_volume"] is None
    assert ghost["published"] is True  # unknown means "don't hide it"


def test_missing_packaged_volume_column(tmp_path):
    # Older dumps lack packagedVolume entirely; volume should stand in.
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE invTypes (typeID INT, typeName TEXT, volume REAL)")
    con.execute("INSERT INTO invTypes VALUES (587, 'Rifter', 27289.5)")
    con.commit()
    con.close()
    rows = sde.types_from_sqlite(path)
    assert rows[0]["packaged_volume"] == 27289.5


def test_no_invtypes_table_is_a_clear_error(tmp_path):
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).execute("CREATE TABLE unrelated (x INT)")
    with pytest.raises(ValueError, match="invTypes"):
        sde.types_from_sqlite(path)
