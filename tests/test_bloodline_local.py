"""手元のデータだけで組む5代血統表（bloodline_local）。

2023年以降の馬 Y は netkeiba の血統表を持つ。古い馬 X は Target の horse_data だけを持つ。
X の父は Y の母の父と同じ馬、X の母は国内で走った牝馬（自分の horse_data の行がある）。
"""

from __future__ import annotations

import pytest

from keiba_data import bloodline_local as bl, db

Y, X, OLD_DAM = "2022100001", "2005100001", "2000100001"


@pytest.fixture
def seeded(conn):
    ts = db.now_str()
    conn.execute("INSERT INTO races (race_id, race_date, venue_code, kaiji, nichime, race_no, fetched_at, updated_at) "
                 "VALUES ('200506010101', '2005-01-05', '06', 1, 1, 1, ?, ?)", (ts, ts))
    for umaban, horse_id in enumerate((Y, X), start=1):
        conn.execute("INSERT INTO horses (horse_id, horse_name, updated_at) VALUES (?, ?, ?)", (horse_id, horse_id, ts))
        conn.execute("INSERT INTO entries (race_id, umaban, horse_id) VALUES ('200506010101', ?, ?)",
                     (umaban, horse_id))
    tree = {"f": "A", "ff": "AF", "fm": "AM", "m": "YD", "mf": "B", "mff": "BF", "mfm": "BM"}
    conn.executemany("INSERT INTO pedigree_horses (horse_no, name, updated_at) VALUES (?, ?, ?)",
                     [(no, no, ts) for no in tree.values()])
    conn.executemany("INSERT INTO horse_ancestors (horse_id, path, generation, ancestor_no) VALUES (?, ?, ?, ?)",
                     [(Y, p, len(p), no) for p, no in tree.items()])
    common = ("x.csv", ts)
    conn.execute("INSERT INTO target_horses (horse_id, horse_name, sex, sire_breed_no, sire_name, dam_breed_no, "
                 "dam_name, bms_breed_no, bms_name, source_file, imported_at) VALUES (?, ?, '牡', ?, ?, ?, ?, ?, ?, ?, ?)",
                 (Y, "ワイ", "1120000001", "エー", "1220000001", "ワイノハハ", "1120000002", "ビー", *common))
    conn.execute("INSERT INTO target_horses (horse_id, horse_name, sex, sire_breed_no, sire_name, dam_breed_no, "
                 "dam_name, source_file, imported_at) VALUES (?, ?, '牡', ?, ?, ?, ?, ?, ?)",
                 (X, "エックス", "1120000002", "ビー", "1229999999", "オールドダム", *common))
    # X の母は国内で走った牝馬。番号の対応は無いが、馬名で horse_id に引ける。母自身の父は A、母は対応の無い番号
    conn.execute("INSERT INTO target_horses (horse_id, horse_name, sex, sire_breed_no, sire_name, dam_breed_no, "
                 "dam_name, source_file, imported_at) VALUES (?, 'オールドダム', '牝', ?, ?, ?, ?, ?, ?)",
                 (OLD_DAM, "1120000001", "エー", "1228888888", "ナゾノハハ", *common))
    conn.commit()
    return conn


def test_build_grafts_subtrees_and_follows_dams_own_row(seeded):
    builder, conflicts = bl.builder_from(seeded)
    assert conflicts == 0
    assert builder.b2n == {"1120000001": "A", "1220000001": "YD", "1120000002": "B"}
    cells = builder.build(X)
    assert cells == {
        "f": "B", "ff": "BF", "fm": "BM",                  # 父 B の部分木（Y の母の父から）
        "m": OLD_DAM,                                       # 馬名で引いた母
        "mf": "A", "mff": "AF", "mfm": "AM",               # 母の horse_data の行 → A の部分木
        "mm": "jv:1228888888",                             # 対応の無い番号はそのまま
    }


def test_rebuild_writes_local_rows_only_and_is_idempotent(seeded):
    result = bl.rebuild(seeded)
    assert result.horses == 1
    rows = seeded.execute("SELECT horse_id, source, COUNT(*) FROM horse_ancestors GROUP BY 1, 2").fetchall()
    assert sorted(tuple(r) for r in rows) == [(X, "local", 8), (Y, "netkeiba", 7)]
    names = dict(seeded.execute("SELECT horse_no, name FROM pedigree_horses"))
    assert names["jv:1228888888"] == "ナゾノハハ" and names[OLD_DAM] == "オールドダム"
    bl.rebuild(seeded)
    assert seeded.execute("SELECT COUNT(*) FROM horse_ancestors").fetchone()[0] == 15
    # netkeiba の取り込み対象の数え方は local の行を数えない
    assert db.count_bloodline(seeded) == (1, 2)
    assert [h["horse_id"] for h in db.horses_without_bloodline(seeded)] == [X]
    assert db.count_local_bloodline(seeded) == (1, 8.0)


def test_netkeiba_tree_replaces_local_rows(seeded):
    bl.rebuild(seeded)

    class Cell:
        def __init__(self, path, no):
            self.path, self.generation, self.horse_no, self.name, self.country = path, len(path), no, no, None

    db.save_horse_pedigree_tree(seeded, X, [Cell("f", "B"), Cell("m", OLD_DAM)])
    rows = seeded.execute("SELECT source, COUNT(*) FROM horse_ancestors WHERE horse_id = ? GROUP BY 1", (X,)).fetchall()
    assert [tuple(r) for r in rows] == [("netkeiba", 2)]
