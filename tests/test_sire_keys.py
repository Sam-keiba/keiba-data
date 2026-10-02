"""種牡馬の名寄せ（sire_keys）。"""

from __future__ import annotations

from keiba_data import db, sire_keys as sk


def test_grouper_joins_domestic_and_overseas_numbers_by_name():
    g = sk.SireGrouper()
    g.add("1120001232", "サンデーサイレンス")
    g.add("1140004339", "Sunday Silence")
    g.add("1140004339", "サンデーサイレンス")       # 2023年以降の馬で、海外記録の番号にカナが付いた
    [group] = g.result()
    assert group.breed_nos == {"1120001232", "1140004339"}
    assert sk.sire_key_of(group) == "1120001232"


def test_grouper_refuses_two_domestic_numbers():
    g = sk.SireGrouper()
    g.add("1120000001", "ドウメイ")
    g.add("1120000002", "ドウメイ")                 # 同名異馬の疑い
    assert g.refused == [("1120000002", "ドウメイ")]
    assert len(g.result()) == 2


def test_rebuild_and_fill_missing(conn):
    ts = db.now_str()
    conn.executemany("INSERT INTO horses (horse_id, horse_name, sire, broodmare_sire, updated_at, source) "
                     "VALUES (?, ?, ?, ?, ?, ?)", [
                         ("2001100001", "フルイウマ", "Sunday Silence", None, ts, "target"),
                         ("2021100001", "アタラシイ", "キタサンブラック", "サンデーサイレンス", ts, "scrape"),
                         # 輸入された母の父（海外記録の番号）が、本体ではカナで書かれている → 2つの番号をつなぐ
                         ("2010100009", "ツナギウマ", None, "サンデーサイレンス", ts, "scrape"),
                     ])
    common = "'x.csv', '2026-01-01 00:00:00'"
    conn.execute(f"INSERT INTO target_horses (horse_id, sire_name, sire_breed_no, source_file, imported_at) "
                 f"VALUES ('2001100001', 'Sunday Silence', '1140004339', {common})")
    conn.execute(f"INSERT INTO target_horses (horse_id, sire_name, sire_breed_no, bms_name, bms_breed_no, "
                 f"source_file, imported_at) VALUES ('2021100001', 'Kitasan Black', '1120002500', "
                 f"'サンデーサイレンス', '1120001232', {common})")
    conn.execute(f"INSERT INTO target_horses (horse_id, bms_name, bms_breed_no, source_file, imported_at) "
                 f"VALUES ('2010100009', 'Sunday Silence', '1140004339', {common})")
    conn.commit()
    sk.rebuild_sire_keys(conn)
    rows = dict(conn.execute("SELECT horse_id, sire_key FROM horses WHERE sire IS NOT NULL"))
    assert rows == {"2001100001": "1120001232", "2021100001": "1120002500"}
    assert conn.execute("SELECT broodmare_sire_key FROM horses WHERE horse_id = '2021100001'").fetchone()[0] \
        == "1120001232"
    # 代表名は2023年以降の馬が使う表記
    assert conn.execute("SELECT name FROM stallions WHERE sire_key = '1120001232'").fetchone()[0] == "サンデーサイレンス"
    # 後から入った馬は名前で引いて埋まる
    conn.execute("INSERT INTO horses (horse_id, sire, updated_at) VALUES ('2024100001', 'Sunday Silence', ?)", (ts,))
    conn.commit()
    assert sk.fill_missing_sire_keys(conn) == 1
    assert conn.execute("SELECT sire_key FROM horses WHERE horse_id = '2024100001'").fetchone()[0] == "1120001232"
