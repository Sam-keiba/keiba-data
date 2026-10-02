"""Target CSV の取り込み（target_import）。

実データは使わず、列番号だけ本物に合わせた合成CSV（CP932）で確かめる。
"""

from __future__ import annotations

import csv
import sqlite3

import pytest

from keiba_data import db, target_import as ti


# --- 値の正規化 -------------------------------------------------------------------


def test_time_and_weights():
    assert ti.race_time("1343") == 94.3
    assert ti.race_time("5066") == 306.6
    assert ti.race_time("----") is None
    assert ti.time_raw(94.3) == "1:34.3"
    assert ti.time_raw(58.6) == "0:58.6"
    assert ti.kinryo(" 57☆") == (57.0, "☆")
    assert ti.kinryo(" 55 ") == (55.0, None)
    assert ti.to_int("+18") == 18 and ti.to_int("-4") == -4 and ti.to_int("") is None


def test_dates_and_text():
    assert ti.dotted_date("2024. 1. 6") == "2024-01-06"
    assert ti.dotted_date("2026.10.25") == "2026-10-25"
    assert ti.birth_date("2022", "5月3日") == "2022-05-03"
    assert ti.text("１勝ｸﾗｽ") == "1勝クラス"
    assert ti.text("(0000000000)") is None and ti.text("%1d") is None and ti.text("不明") is None
    with pytest.raises(ti.RowError):
        ti.dotted_date("2024/01/06")


def test_horse_fields():
    assert ti.record(" 2- 1- 1- 7") == (2, 1, 1, 7)
    assert ti.amount_with_count("  2089万(2)") == (2089.0, 2)
    assert ti.amount_with_count("    0.0  (1)") == (0.0, 1)
    assert ti.price("990万円(他)") == (990, "(他)")
    assert ti.price("226.8万円") == (226.8, None)
    assert ti.split_stable("(栗)山田太郎") == ("栗東", "山田太郎")
    assert ti.stable("[外]") == "海外"


def test_vocabulary_for_core_tables():
    assert ti.netkeiba_margin("1 1/4") == "1.1/4"
    assert ti.netkeiba_margin("頭") == "アタマ"
    assert ti.netkeiba_margin("大差") == "大"
    assert ti.netkeiba_margin("取消") is None
    assert ti.race_conditions("A03", "3") == "混,指,馬齢"
    assert ti.race_conditions("000", "4") == "定量"
    assert ti.class_condition("未勝利", "A23") == "未勝利 牝"
    assert ti.class_condition("500万", "A03") == "500万下"
    assert ti.class_condition("G1", "N41") == "オープン 牡・牝"
    assert ti.race_conditions("B03", "3") is None         # 重複期間に出てこないコードは埋めない
    assert ti.surface_of("17") == "turf" and ti.surface_of("24") == "dirt" and ti.surface_of("54") == "jump"


# --- 合成CSV ----------------------------------------------------------------------


def race_row(key18: str, values: dict | None = None) -> list[str]:
    """race_data の1行（110列）。指定しない列は空欄。"""
    row = [""] * ti.RACE_N_COLUMNS
    y, m, d = key18[0:4], key18[4:6], key18[6:8]
    defaults = {
        ti.R_DATE: f"{y}.{int(m):2d}.{int(d):2d}", ti.R_KEY18: key18, ti.R_UMABAN: str(int(key18[16:18])),
        ti.R_KAISAI: "1中1", ti.R_RACE_NO: str(int(key18[14:16])), ti.R_RACE_NAME: "未勝利",
        ti.R_HORSE_NAME: "テストウマ", ti.R_SEX: "牡", ti.R_AGE: "3", ti.R_JOCKEY: "騎手Ａ", ti.R_KINRYO: " 56 ",
        ti.R_N_REGISTERED: "2", ti.R_POPULARITY: "1", ti.R_FINISH: "１", ti.R_GOING: "稍", ti.R_STABLE: "(美)",
        ti.R_TRAINER: "調教師Ａ", ti.R_TIME: "1343", ti.R_LAST_3F: "35.0", ti.R_WEIGHT: "480",
        ti.R_WEIGHT_DIFF: "+2", ti.R_WAKU: "1", ti.R_CORNERS[2]: "3", ti.R_CORNERS[3]: "2",
        ti.R_HORSE_ID: "2021100001", ti.R_JOCKEY_ID: "01001", ti.R_TRAINER_ID: "01002", ti.R_FINISH_POS: "1",
        ti.R_ARRIVAL: "1", ti.R_ABNORMAL: "0", ti.R_TRACK_JV: "17", ti.R_CLASS_CODE: "7", ti.R_SYMBOL: "A03",
        ti.R_RACE_TYPE: "12", ti.R_WEIGHT_TYPE: "3", ti.R_POST_TIME: "10:05", ti.R_CLASS_NAME: "未勝利",
        ti.R_WIN_ODDS: "2.5", ti.R_PRIZE: "550", ti.R_ADDED_PRIZE: "", ti.R_WEATHER: " 晴 ",
        ti.R_SURFACE: "芝", ti.R_DISTANCE: "1600", ti.R_COURSE_SETTING: "A", ti.R_PCI3: "50.0", ti.R_RPCI: "48.0",
    }
    for i, v in {**defaults, **(values or {})}.items():
        row[i] = v
    return row


def horse_row(horse_id: str, values: dict | None = None) -> list[str]:
    row = [""] * ti.HORSE_N_COLUMNS
    defaults = {
        ti.H_NAME: "テストウマ", ti.H_NAME_EN: "Test Uma", ti.H_HORSE_ID: horse_id, ti.H_SEX: "牡", ti.H_AGE: "5",
        ti.H_STATUS: "抹消", ti.H_STABLE: "(美)調教師Ａ", ti.H_SIRE: "Foreign Sire", ti.H_DAM: "ハハウマ",
        ti.H_BMS: "ハハチチ", ti.H_OWNER: "馬主Ａ", ti.H_OWNER_CODE: "000123", ti.H_BREEDER: "牧場Ａ",
        ti.H_RECORD: " 1- 0- 0- 2", ti.H_BIRTH_YEAR: "2021", ti.H_BIRTHDAY: "4月1日",
        ti.H_CREATED: "2026. 9. 1", ti.H_REGISTERED: "2022. 9. 1", ti.H_SIBLING_SUM: "  100万(1)",
        ti.H_SIBLING_AVG: " 100.0万(1)", ti.H_SIBLINGS[0][0]: "アニウマ", ti.H_SIBLINGS[0][1]: "2",
        ti.H_BREED_NOS[0]: "1120000001", ti.H_BREED_NOS[2]: "1220000001", ti.H_BREED_NOS[3]: "1120000002",
    }
    for i, v in {**defaults, **(values or {})}.items():
        row[i] = v
    return row


def write_csv(path, rows, n_columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="cp932", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"列{i}" for i in range(n_columns)])
        w.writerows(rows)
    return path


@pytest.fixture
def datasets(tmp_path):
    root = tmp_path / "datasets"
    write_csv(root / "race_data" / "race_data_2024.csv", [
        race_row("202401060601010101"),
        race_row("202401060601010102", {ti.R_HORSE_ID: "2021100002", ti.R_FINISH: "２", ti.R_FINISH_POS: "2",
                                          ti.R_ARRIVAL: "2", ti.R_MARGIN: "1 1/4", ti.R_PRIZE: "0",
                                          ti.R_TIME: "1345"}),
        race_row("202401060601010103", {ti.R_KEY18: "2024010606010101XX"}),   # IDが壊れている → 飛ばす
    ], ti.RACE_N_COLUMNS)
    write_csv(root / "horse_data" / "horse_data_2026_5sai.csv", [
        horse_row("2021100001"),
        horse_row("2021100002", {ti.H_NAME: "ベツウマ"}),
    ], ti.HORSE_N_COLUMNS)
    return root


def counts(conn, *tables):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


# --- 取り込み ---------------------------------------------------------------------


def test_load_files_skips_bad_rows_and_logs(conn, datasets):
    results = ti.load_files(conn, datasets)
    race, horse = results
    assert (race.n_rows, race.n_loaded, race.n_skipped) == (3, 2, 1)
    assert (horse.n_rows, horse.n_loaded, horse.n_skipped) == (2, 2, 0)
    assert counts(conn, "target_races", "target_runs", "target_horses", "target_horse_siblings") == {
        "target_races": 1, "target_runs": 2, "target_horses": 2, "target_horse_siblings": 2}
    warning = conn.execute("SELECT key, message FROM parse_warnings").fetchone()
    assert warning["key"] == "race_data/race_data_2024.csv:4" and "18桁" in warning["message"]
    race_row_ = conn.execute("SELECT * FROM target_races").fetchone()
    assert (race_row_["race_id"], race_row_["race_date"], race_row_["going"], race_row_["n_runners"]) == (
        "202406010101", "2024-01-06", "稍重", 2)
    run = conn.execute("SELECT * FROM target_runs WHERE umaban = 1").fetchone()
    assert (run["time_sec"], run["kinryo"], run["stable"], run["weight_diff"]) == (94.3, 56.0, "美浦", 2)


def test_load_is_idempotent(conn, datasets):
    ti.load_files(conn, datasets)
    first = counts(conn, "target_races", "target_runs", "target_horses", "target_horse_siblings")
    again = ti.load_files(conn, datasets)
    assert all(r.unchanged for r in again)                    # 中身が同じなので読み飛ばす
    forced = ti.load_files(conn, datasets, force=True)
    assert not any(r.unchanged for r in forced)
    assert counts(conn, "target_races", "target_runs", "target_horses", "target_horse_siblings") == first
    assert conn.execute("SELECT COUNT(*) FROM target_import_files").fetchone()[0] == 2


def test_dry_run_writes_nothing(conn, datasets):
    results = ti.load_files(conn, datasets, dry_run=True)
    assert sum(r.n_loaded for r in results) == 4
    assert counts(conn, "target_runs", "target_horses", "target_import_files", "parse_warnings") == {
        "target_runs": 0, "target_horses": 0, "target_import_files": 0, "parse_warnings": 0}


def test_duplicate_horse_keeps_newer_row(conn, tmp_path):
    root = tmp_path / "datasets"
    write_csv(root / "horse_data" / "horse_data_2026_5sai.csv", [
        horse_row("2021100001", {ti.H_STATUS: "在厩", ti.H_CREATED: "2026. 9. 1"}),
        horse_row("2021100001", {ti.H_STATUS: "抹消", ti.H_CREATED: "2026. 9.10"}),
    ], ti.HORSE_N_COLUMNS)
    [result] = ti.load_files(conn, root, kinds=("horse",))
    assert (result.n_loaded, result.n_skipped) == (1, 1)
    assert conn.execute("SELECT status FROM target_horses").fetchone()[0] == "抹消"


# --- 本体へのマージ ---------------------------------------------------------------


def seed_existing(conn):
    """スクレイピング由来の既存行（父はカナ、生年月日は空欄）。"""
    ts = db.now_str()
    conn.execute("INSERT INTO horses (horse_id, horse_name, sex, sire, birth_date, updated_at) "
                 "VALUES ('2021100001', 'テストウマ', '牡', 'フォーリンサイアー', NULL, ?)", (ts,))
    conn.commit()


def test_merge_inserts_missing_rows_and_fills_blanks_only(conn, datasets):
    seed_existing(conn)
    ti.load_files(conn, datasets)
    stats = ti.merge(conn)

    # 既存の馬: 父（カナ）はそのまま、空欄の生年月日だけ埋まり、記録が残る
    h = conn.execute("SELECT * FROM horses WHERE horse_id = '2021100001'").fetchone()
    assert (h["sire"], h["birth_date"], h["source"]) == ("フォーリンサイアー", "2021-04-01", "scrape")
    assert conn.execute("SELECT COUNT(*) FROM target_fills WHERE table_name = 'horses' AND row_key = '2021100001' "
                        "AND column_name = 'birth_date'").fetchone()[0] == 1
    # 新しい馬: 同じ繁殖登録番号の父は、既存の書き方（カナ）にそろえる
    h2 = conn.execute("SELECT * FROM horses WHERE horse_id = '2021100002'").fetchone()
    assert (h2["sire"], h2["source"]) == ("フォーリンサイアー", "target")
    assert stats.name_aliases >= 1

    race = conn.execute("SELECT * FROM races").fetchone()
    assert (race["race_id"], race["surface"], race["direction"], race["going_turf"], race["class_condition"],
            race["race_conditions"], race["age_condition"], race["source"]) == (
        "202406010101", "turf", "right", "稍重", "未勝利", "混,指,馬齢", "3歳", "target")
    second = conn.execute("SELECT * FROM results WHERE umaban = 2").fetchone()
    assert (second["time_raw"], second["margin"], second["prize_man_yen"], second["corner_passing"]) == (
        "1:34.5", "1.1/4", None, "3-2")
    assert counts(conn, "entries", "results", "jockeys", "trainers", "owners") == {
        "entries": 2, "results": 2, "jockeys": 1, "trainers": 1, "owners": 1}
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_merge_never_overwrites_existing_values(conn, datasets):
    ti.load_files(conn, datasets)
    ti.merge(conn)
    conn.execute("UPDATE results SET win_odds = 9.9 WHERE umaban = 1")
    conn.execute("UPDATE races SET weather = NULL")
    conn.commit()
    stats = ti.merge(conn)
    assert conn.execute("SELECT win_odds FROM results WHERE umaban = 1").fetchone()[0] == 9.9
    assert conn.execute("SELECT weather FROM races").fetchone()[0] == "晴"
    assert stats.filled == {("races", "weather"): 1}


def test_merge_is_idempotent(conn, datasets):
    seed_existing(conn)
    ti.load_files(conn, datasets)
    ti.merge(conn)
    tables = ("races", "entries", "results", "horses", "jockeys", "trainers", "owners", "target_fills")
    first = counts(conn, *tables)
    stats = ti.merge(conn)
    assert counts(conn, *tables) == first
    assert sum(stats.inserted.values()) == 0 and not stats.filled


def test_merge_dry_run_rolls_back(conn, datasets):
    ti.load_files(conn, datasets)
    stats = ti.merge(conn, dry_run=True)
    assert stats.inserted["races"] == 1
    assert counts(conn, "races", "entries", "horses") == {"races": 0, "entries": 0, "horses": 0}


def test_migrate_adds_source_without_touching_rows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE races (race_id TEXT PRIMARY KEY, race_date TEXT NOT NULL);
        CREATE TABLE horses (horse_id TEXT PRIMARY KEY, horse_name TEXT, updated_at TEXT NOT NULL);
        INSERT INTO races VALUES ('202406010101', '2024-01-06');
        INSERT INTO horses VALUES ('2021100001', 'テストウマ', '2024-01-06 00:00:00');
    """)
    old.close()
    conn = db.connect(path)
    assert conn.execute("SELECT source FROM races").fetchone()[0] == "scrape"
    assert conn.execute("SELECT source FROM horses").fetchone()[0] == "scrape"
    db.init_db(conn)                       # 2回目も壊れない
    conn.close()


def test_merge_derives_plain_name_export_owner_and_sire_keys(conn, datasets):
    seed_existing(conn)
    ti.load_files(conn, datasets)
    ti.merge(conn)
    race = conn.execute("SELECT race_name, race_name_plain FROM races").fetchone()
    assert (race["race_name"], race["race_name_plain"]) == ("未勝利", "3歳未勝利")   # netkeiba と同じ書き方
    # 書き出し時点の馬主は別の列に入り、レース当時の馬主（owner_id）は空のまま
    rows = conn.execute("SELECT owner_id, owner_id_at_export FROM entries").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(None, "000123"), (None, "000123")]
    # 英字（Target）とカナ（本体）の父が、同じ繁殖登録番号で1つのキーにまとまる
    keys = conn.execute("SELECT DISTINCT sire_key FROM horses").fetchall()
    assert [k[0] for k in keys] == ["1120000001"]
    names = dict(conn.execute("SELECT name, sire_key FROM stallion_names"))
    assert names["Foreign Sire"] == names["フォーリンサイアー"] == "1120000001"
    assert conn.execute("SELECT name FROM stallions WHERE sire_key = '1120000001'").fetchone()[0] == "フォーリンサイアー"
    # 母のキーは繁殖登録番号そのもの
    assert [r[0] for r in conn.execute("SELECT DISTINCT dam_key FROM horses")] == ["1220000001"]


def test_merge_copies_the_apprentice_mark_to_entries(conn, tmp_path):
    root = tmp_path / "datasets"
    write_csv(root / "race_data" / "race_data_2024.csv", [
        race_row("202401060601010101", {ti.R_KINRYO: "53▲"}),
        race_row("202401060601010102", {ti.R_HORSE_ID: "2021100002", ti.R_FINISH: "２", ti.R_FINISH_POS: "2",
                                          ti.R_ARRIVAL: "2"}),
    ], ti.RACE_N_COLUMNS)
    ti.load_files(conn, root, kinds=("race",))
    ti.merge(conn)
    rows = dict(conn.execute("SELECT umaban, kinryo_mark FROM entries"))
    assert rows == {1: "▲", 2: None}
    kinryo = conn.execute("SELECT kinryo FROM entries WHERE umaban = 1").fetchone()[0]
    assert kinryo == 53.0


def test_merge_copies_the_apprentice_mark_to_entries(conn, tmp_path):
    root = tmp_path / "datasets"
    write_csv(root / "race_data" / "race_data_2024.csv", [
        race_row("202401060601010101", {ti.R_KINRYO: "53▲"}),
        race_row("202401060601010102", {ti.R_HORSE_ID: "2021100002", ti.R_FINISH: "２", ti.R_FINISH_POS: "2",
                                          ti.R_ARRIVAL: "2"}),
    ], ti.RACE_N_COLUMNS)
    ti.load_files(conn, root, kinds=("race",))
    ti.merge(conn)
    rows = dict(conn.execute("SELECT umaban, kinryo_mark FROM entries"))
    assert rows == {1: "▲", 2: None}
    kinryo = conn.execute("SELECT kinryo FROM entries WHERE umaban = 1").fetchone()[0]
    assert kinryo == 53.0


def test_merge_dry_run_leaves_derived_columns_empty(conn, datasets):
    ti.load_files(conn, datasets)
    ti.merge(conn, dry_run=True)
    assert counts(conn, "stallions", "stallion_names") == {"stallions": 0, "stallion_names": 0}
