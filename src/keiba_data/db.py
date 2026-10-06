"""SQLiteへの保存・読み出し（標準ライブラリのsqlite3のみ）。

冪等性の方針:
- races とマスタ（horses/jockeys/trainers/owners）は `INSERT ... ON CONFLICT DO UPDATE` で上書き
- レースに従属する entries/results/payouts/race_laps は、1トランザクション内で
  そのレースの行を削除してから入れ直す（パーサ修正で行が減った場合にも古い行が残らない）
同じレースを何度保存しても、行が重複して増えることはない。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from importlib import resources
from pathlib import Path

from keiba_data import config, race_names
from keiba_data.race_id import decode_race_id
from keiba_data.scrapers.jra_odds import BET_TYPES
from keiba_data.scrapers.race_result import RaceResultPage
from keiba_data.scrapers.shutuba import ShutubaPage

RACE_COLUMNS = (
    "race_id", "race_date", "venue_code", "kaiji", "nichime", "race_no", "race_name", "grade",
    "surface", "direction", "distance_m", "course_detail", "weather", "going_turf", "going_dirt",
    "post_time", "age_condition", "class_condition", "race_conditions", "condition_raw", "n_runners",
)
ENTRY_COLUMNS = (
    "race_id", "umaban", "waku", "horse_id", "sex", "age", "kinryo", "jockey_id", "trainer_id",
    "owner_id", "horse_weight", "weight_diff",
)
RESULT_COLUMNS = (
    "race_id", "umaban", "horse_id", "finish_position", "finish_status", "time_sec", "time_raw",
    "margin", "corner_passing", "last_3f", "win_odds", "popularity", "prize_man_yen",
)
PAYOUT_COLUMNS = ("race_id", "bet_type", "combination", "payout_yen", "popularity")
LAP_COLUMNS = ("race_id", "seq", "distance_m", "lap_sec")

# JRA公式の結果ページから取る勝ち馬の値（races の列。netkeibaの取り込みでは触らない）
JRA_WINNER_COLUMNS = (
    ("winner_umaban", "INTEGER"), ("winner_waku", "INTEGER"),
    ("winner_name", "TEXT"), ("winner_last_3f", "REAL"),
)

DATA_TABLES = (
    "races", "entries", "results", "payouts", "race_laps", "horses", "jockeys", "trainers", "owners",
    "upcoming_races", "upcoming_entries", "track_conditions", "board_horses", "bet_slips",
    "horse_marks",
)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path: Path | str = config.DB_PATH, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """DBに接続し、スキーマを適用して返す。

    check_same_thread=False は、スクリプトを何度も実行し直すStreamlitから
    1つの接続を使い回すために使う。
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # 取り込み（`keiba pedigree` と `keiba bloodline` など）を同時に流すと書き込みがぶつかる。
    # WALなので読みは止まらないが、書き手どうしは待ち合わせになるので待ち時間を与える
    # （既定は0秒で、ぶつかった瞬間に "database is locked" で落ちてしまう）。
    conn.execute("PRAGMA busy_timeout = 30000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    _migrate(conn)
    schema = resources.files("keiba_data").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    conn.executemany(
        "INSERT INTO venues (venue_code, venue_name) VALUES (?, ?) "
        "ON CONFLICT(venue_code) DO UPDATE SET venue_name = excluded.venue_name",
        list(config.VENUE_CODES.items()),
    )
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """スキーマ変更への追従。

    races には後から jra_cname（JRA公式のレース結果ページへのリンク）を、
    track_conditions には measured_at（測定日時）を足した。
    upcoming_entries は当初 (race_id, umaban) を主キーにしていたが、枠順確定前は
    馬番が無いため (race_id, seq) に変えた。古い定義のままなら作り直す
    （未開催レースの出馬表は `keiba entries` で数分あれば取り直せる）。
    """
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # 予想印（race_marks）は一度入れたが、印を1つ進めるたびにページが再読み込みされて
    # 使い勝手が悪かったため取りやめた。作られていたら片付ける。
    if "race_marks" in tables:
        conn.execute("DROP TABLE race_marks")
        conn.commit()
    # JRA公式のレース結果ページへのリンク（CNAMEトークン）を後から足す。
    # netkeiba側の保存（RACE_COLUMNS）には含めない列なので、取り込み順に関係なく残る。
    if "races" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(races)")}
        if "jra_cname" not in columns:
            conn.execute("ALTER TABLE races ADD COLUMN jra_cname TEXT")
            conn.commit()
        # 勝ち馬のコーナー通過順位（JRA公式由来）。開催の勝ちタイム一覧で脚質を出すのに使う
        if "winner_corner" not in columns:
            conn.execute("ALTER TABLE races ADD COLUMN winner_corner TEXT")
            conn.commit()
        # 勝ち馬の馬番・枠・馬名・上り3F（JRA公式由来）。開催の勝ちタイム一覧に出す
        for column, kind in JRA_WINNER_COLUMNS:
            if column not in columns:
                conn.execute(f"ALTER TABLE races ADD COLUMN {column} {kind}")
                conn.commit()
    # Target（`target-import`）で作った行と見分ける出所。定数の既定値なので、
    # 既存の行は書き換えずに 'scrape' として読める
    for table in ("races", "horses"):
        if table in tables:
            columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "source" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'scrape'")
                conn.commit()
    # 付記の無いレース名（race_names.py）。scrape の行は移行時にまとめて埋める
    # （Target の行は target-import のマージで作る）
    if "races" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(races)")}
        if "race_name_plain" not in columns:
            conn.execute("ALTER TABLE races ADD COLUMN race_name_plain TEXT")
            if "race_name" in columns:
                race_names.refresh_scrape_plain_names(conn)
            conn.commit()
    # Target書き出し時点の馬主（target-import）。レース当時の馬主（owner_id）とは分けて持つ
    if "entries" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
        if "owner_id_at_export" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN owner_id_at_export TEXT REFERENCES owners(owner_id)")
            conn.commit()
        # 減量騎手の印（target_runs・JRAの出馬表から写す）。厩舎分析の「見習い騎手起用」に使う
        if "kinryo_mark" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN kinryo_mark TEXT")
            conn.commit()
    # JRAの調教師名鑑から入れる正式名など（netkeibaの名前は4文字で切れている）
    if "trainers" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(trainers)")}
        for column, kind in (("full_name", "TEXT"), ("kana", "TEXT"), ("birth_date", "TEXT"),
                             ("license_year", "INTEGER"), ("meikan_updated_at", "TEXT")):
            if column not in columns:
                conn.execute(f"ALTER TABLE trainers ADD COLUMN {column} {kind}")
        conn.commit()
    # 5代血統表の出どころ（netkeiba / 手元のデータから組んだ local。`bloodline-local`）
    if "horse_ancestors" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(horse_ancestors)")}
        if "source" not in columns:
            conn.execute("ALTER TABLE horse_ancestors ADD COLUMN source TEXT NOT NULL DEFAULT 'netkeiba'")
            conn.commit()
    # 予想ボードで「消した」馬（保存済みの盤を壊さないよう、後から列を足す）
    if "board_horses" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(board_horses)")}
        if "is_excluded" not in columns:
            conn.execute(
                "ALTER TABLE board_horses ADD COLUMN is_excluded INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()
    # 買い目の「買い方」（ながし・ボックス）。先にテーブルができている手元のDB用
    if "bet_slips" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(bet_slips)")}
        if "kind" not in columns:
            conn.execute("ALTER TABLE bet_slips ADD COLUMN kind TEXT")
            conn.commit()
    # 血統・馬主・生産牧場（JRA公式の出馬表）を後から足す。
    # 競走馬の詳細ページ（`keiba pedigree`）からは生年月日と血統登録番号も取れるので、
    # そのぶんの列も足す（父を名前ではなく番号で束ねたいときのため）。
    if "horses" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(horses)")}
        for column in ("sire", "dam", "broodmare_sire", "owner_name", "breeder",
                       "birth_date", "sire_no", "broodmare_sire_no", "trainer_name",
                       "sire_key", "broodmare_sire_key", "dam_key"):
            if column not in columns:
                conn.execute(f"ALTER TABLE horses ADD COLUMN {column} TEXT")
        conn.commit()
    # 当日の馬場情報（JRAの馬場情報ページ）から測定日時・馬場状態・天候を入れるようになったので、
    # 後から列を足す
    if "track_conditions" in tables:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(track_conditions)")}
        for column in ("measured_at", "going_turf", "going_dirt", "weather"):
            if column not in columns:
                conn.execute(f"ALTER TABLE track_conditions ADD COLUMN {column} TEXT")
        conn.commit()
    if "upcoming_entries" not in tables:
        return
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(upcoming_entries)")}
    if "seq" not in columns:
        conn.execute("DROP TABLE upcoming_entries")
        conn.execute("UPDATE upcoming_races SET entry_status = 'list_only'")
        conn.commit()
    elif "kinryo_mark" not in columns:
        conn.execute("ALTER TABLE upcoming_entries ADD COLUMN kinryo_mark TEXT")
        conn.commit()


def _insert_sql(table: str, columns: tuple[str, ...]) -> str:
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})"


def _upsert_sql(table: str, columns: tuple[str, ...], key: str, keep: tuple[str, ...] = ()) -> str:
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != key and c not in keep)
    return f"{_insert_sql(table, columns)} ON CONFLICT({key}) DO UPDATE SET {updates}"


# --- races ------------------------------------------------------------------


def has_race(conn: sqlite3.Connection, race_id: str) -> bool:
    return conn.execute("SELECT 1 FROM races WHERE race_id = ?", (race_id,)).fetchone() is not None


def saved_race_ids(conn: sqlite3.Connection, race_ids: list[str]) -> set[str]:
    if not race_ids:
        return set()
    placeholders = ", ".join("?" for _ in race_ids)
    rows = conn.execute(f"SELECT race_id FROM races WHERE race_id IN ({placeholders})", race_ids)
    return {r["race_id"] for r in rows}


def save_race_page(conn: sqlite3.Connection, page: RaceResultPage) -> None:
    """結果ページ1件ぶん（レース・マスタ・出走表・結果・払戻・ラップ）を1トランザクションで保存する。"""
    race = page.race
    race_id = race["race_id"]
    if not race.get("race_date"):
        raise ValueError(f"開催日が取得できていないため保存できません: {race_id}")
    ts = now_str()

    with conn:
        conn.execute(
            _upsert_sql("races", RACE_COLUMNS + ("fetched_at", "updated_at"), "race_id", keep=("fetched_at",)),
            [race.get(c) for c in RACE_COLUMNS] + [ts, ts],
        )
        conn.execute("UPDATE races SET race_name_plain = ? WHERE race_id = ?",
                     (race_names.plain_scrape_name(race.get("race_name")), race_id))
        for e in page.entries:
            if e["horse_id"]:
                conn.execute(
                    _upsert_sql("horses", ("horse_id", "horse_name", "sex", "updated_at"), "horse_id"),
                    (e["horse_id"], e["horse_name"], e["sex"], ts),
                )
            if e["jockey_id"]:
                conn.execute(
                    _upsert_sql("jockeys", ("jockey_id", "jockey_name", "updated_at"), "jockey_id"),
                    (e["jockey_id"], e["jockey_name"], ts),
                )
            if e["trainer_id"]:
                conn.execute(
                    _upsert_sql("trainers", ("trainer_id", "trainer_name", "stable", "updated_at"), "trainer_id"),
                    (e["trainer_id"], e["trainer_name"], e["trainer_stable"], ts),
                )
            if e["owner_id"]:
                conn.execute(
                    _upsert_sql("owners", ("owner_id", "owner_name", "updated_at"), "owner_id"),
                    (e["owner_id"], e["owner_name"], ts),
                )

        # entriesを消すとresultsもON DELETE CASCADEで消える
        conn.execute("DELETE FROM entries WHERE race_id = ?", (race_id,))
        conn.execute("DELETE FROM payouts WHERE race_id = ?", (race_id,))
        conn.execute("DELETE FROM race_laps WHERE race_id = ?", (race_id,))
        conn.executemany(_insert_sql("entries", ENTRY_COLUMNS), [[e.get(c) for c in ENTRY_COLUMNS] for e in page.entries])
        conn.executemany(_insert_sql("results", RESULT_COLUMNS), [[r.get(c) for c in RESULT_COLUMNS] for r in page.results])
        conn.executemany(_insert_sql("payouts", PAYOUT_COLUMNS), [[p.get(c) for c in PAYOUT_COLUMNS] for p in page.payouts])
        conn.executemany(_insert_sql("race_laps", LAP_COLUMNS), [[lap.get(c) for c in LAP_COLUMNS] for lap in page.laps])
        # netkeibaの結果ページには減量の印が無いので、Target・JRAの出馬表から写し直す
        fill_kinryo_marks(conn, race_id)


def race_ids_with_results(conn: sqlite3.Connection, race_ids: list[str]) -> set[str]:
    """**馬ごとの結果まで**入っているrace_id。

    JRA公式から取り込んだ行（レース情報とラップだけ）は含めない。
    netkeibaの取得側はこれを「取得済み」の判定に使うので、JRAが先に入れたレースも
    あとから馬ごとの結果を取りに行ける。
    """
    if not race_ids:
        return set()
    placeholders = ", ".join("?" for _ in race_ids)
    rows = conn.execute(
        f"SELECT DISTINCT race_id FROM results WHERE race_id IN ({placeholders})", race_ids
    )
    return {r["race_id"] for r in rows}


def save_jra_race(conn: sqlite3.Connection, race: dict, laps: list[dict]) -> None:
    """JRA公式から読んだレース情報とラップだけを保存する。

    - `entries` / `results` / `payouts` には**触れない**（netkeibaが入れた分を壊さない）
    - `races` は値がNULLの列は既存の値を残す（netkeibaのほうが詳しい列があるため）
    - `race_laps` は**ラップがあるときだけ**入れ替える（障害レースで既存を消さない）
    """
    race_id = race["race_id"]
    if not race.get("race_date"):
        raise ValueError(f"開催日が取得できていないため保存できません: {race_id}")
    ts = now_str()
    # winner_corner は RACE_COLUMNS の外（netkeibaの取り込みでは触らない列）なので、
    # jra_cname と同じく、あとから結果を入れ直しても消えない。
    saved_columns = RACE_COLUMNS + ("winner_corner",) + tuple(c for c, _ in JRA_WINNER_COLUMNS)
    columns = saved_columns + ("fetched_at", "updated_at")
    updates = ", ".join(
        f"{c} = COALESCE(excluded.{c}, {c})" for c in saved_columns if c != "race_id"
    )
    with conn:
        conn.execute(
            f"{_insert_sql('races', columns)} ON CONFLICT(race_id) DO UPDATE SET "
            f"{updates}, updated_at = excluded.updated_at",
            [race.get(c) for c in saved_columns] + [ts, ts],
        )
        conn.execute("UPDATE races SET race_name_plain = COALESCE(?, race_name_plain) WHERE race_id = ?",
                     (race_names.plain_scrape_name(race.get("race_name")), race_id))
        if laps:
            conn.execute("DELETE FROM race_laps WHERE race_id = ?", (race_id,))
            conn.executemany(
                _insert_sql("race_laps", LAP_COLUMNS),
                [[lap.get(c) for c in LAP_COLUMNS] for lap in laps],
            )


def save_jra_race_links(conn: sqlite3.Connection, links: dict[str, str]) -> int:
    """JRA公式のレース結果ページへのリンク（CNAMEトークン）を races に保存する。

    まだ `races` に無いレース（未取得の開催）は黙って飛ばす。列は `RACE_COLUMNS` の外なので、
    あとから netkeiba の結果を保存し直してもこのリンクは消えない。保存できた件数を返す。
    """
    if not links:
        return 0
    with conn:
        cur = conn.executemany(
            "UPDATE races SET jra_cname = ? WHERE race_id = ?",
            [(cname, race_id) for race_id, cname in links.items()],
        )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def races_without_jra_link(conn: sqlite3.Connection, start: str, end: str) -> set[str]:
    """その期間で、まだJRAのリンクが入っていないレースの (開催日, 競馬場) を返す。

    リンクは開催単位（レース選択ページ1枚）でまとめて取れるので、埋める単位も開催にする。
    """
    rows = conn.execute(
        "SELECT DISTINCT race_date, venue_code FROM races "
        "WHERE race_date BETWEEN ? AND ? AND jra_cname IS NULL "
        "ORDER BY race_date, venue_code",
        (start, end),
    )
    return {(r["race_date"], r["venue_code"]) for r in rows}


# --- 血統・馬主・生産牧場（JRA公式の出馬表から） -----------------------------

HORSE_PROFILE_COLUMNS = ("sire", "dam", "broodmare_sire", "owner_name", "breeder")


def save_horse_profiles(conn: sqlite3.Connection, race_id: str, profiles: list) -> int:
    """JRAの出馬表から読んだ血統・馬主・生産牧場を horses に入れる。

    JRAは馬のnetkeiba IDを持たないので、**そのレースの出馬表（upcoming_entries）の
    馬番**で突き合わせる（枠順確定前は馬番が無いので馬名で突き合わせる）。
    値がNULLの列は既存の値を残す。保存できた頭数を返す。
    """
    entries = list(conn.execute(
        "SELECT umaban, horse_id, horse_name FROM upcoming_entries WHERE race_id = ?", (race_id,)
    ))
    by_umaban = {r["umaban"]: r for r in entries if r["umaban"] is not None}
    by_name = {r["horse_name"]: r for r in entries if r["horse_name"]}

    ts = now_str()
    saved = 0
    with conn:
        for profile in profiles:
            entry = by_umaban.get(profile.umaban) or by_name.get(profile.horse_name)
            if entry is None or not entry["horse_id"]:
                continue
            values = (profile.sire, profile.dam, profile.broodmare_sire,
                      profile.owner, profile.breeder)
            updates = ", ".join(f"{c} = COALESCE(excluded.{c}, {c})" for c in HORSE_PROFILE_COLUMNS)
            conn.execute(
                "INSERT INTO horses (horse_id, horse_name, "
                f"{', '.join(HORSE_PROFILE_COLUMNS)}, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(horse_id) DO UPDATE SET "
                f"horse_name = COALESCE(excluded.horse_name, horse_name), {updates}, "
                "updated_at = excluded.updated_at",
                (entry["horse_id"], profile.horse_name, *values, ts),
            )
            saved += 1
    return saved


def save_upcoming_weights(conn: sqlite3.Connection, race_id: str, profiles: list) -> int:
    """JRAの出馬表から読んだ馬体重を `upcoming_entries` に入れる。入れた頭数を返す。

    馬体重は当日発表（発走の1時間ほど前）なので、まだ載っていない馬は
    `horse_weight` が None で届く。その馬は**触らない**（前に入れた値を消さない）。
    突き合わせは血統と同じで、**馬番**（枠順確定前は馬名）。
    """
    entries = list(conn.execute(
        "SELECT seq, umaban, horse_name FROM upcoming_entries WHERE race_id = ?", (race_id,)
    ))
    by_umaban = {r["umaban"]: r for r in entries if r["umaban"] is not None}
    by_name = {r["horse_name"]: r for r in entries if r["horse_name"]}

    saved = 0
    with conn:
        for profile in profiles:
            if profile.horse_weight is None:
                continue
            entry = by_umaban.get(profile.umaban) or by_name.get(profile.horse_name)
            if entry is None:
                continue
            conn.execute(
                "UPDATE upcoming_entries SET horse_weight = ?, weight_diff = ? "
                "WHERE race_id = ? AND seq = ?",
                (profile.horse_weight, profile.weight_diff, race_id, entry["seq"]),
            )
            saved += 1
    return saved


def save_trainer_profile(conn: sqlite3.Connection, profile) -> None:
    """JRAの調教師名鑑から読んだ1人ぶんを trainers に入れる（`jra_trainer.TrainerProfile`）。

    netkeibaの4文字の名前（trainer_name）には触らない（下流が今もそれで表示・照合している）。
    まだ1走もしていない新規開業の調教師は行ごと足す（trainer_name は空のまま）。
    """
    ts = now_str()
    with conn:
        conn.execute(
            "INSERT INTO trainers (trainer_id, stable, full_name, kana, birth_date, license_year, "
            "meikan_updated_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(trainer_id) DO UPDATE SET stable = COALESCE(excluded.stable, stable), "
            "full_name = excluded.full_name, kana = excluded.kana, birth_date = excluded.birth_date, "
            "license_year = excluded.license_year, meikan_updated_at = excluded.meikan_updated_at, "
            "updated_at = excluded.updated_at",
            (profile.trainer_id, profile.stable, profile.full_name, profile.kana, profile.birth_date,
             profile.license_year, ts, ts),
        )


def save_upcoming_kinryo_marks(conn: sqlite3.Connection, race_id: str, profiles: list) -> int:
    """JRAの出馬表から読んだ減量騎手の印を `upcoming_entries` に入れる。入れた頭数を返す。

    印は騎手に付くものなので、**JRAの出馬表の騎手と netkeiba の出馬表の騎手が同じ行だけ**
    書く（どちらかの取り直しが遅れて騎手が食い違う間は、どの騎手の印か分からないため触らない）。
    印の無い騎手は NULL で上書きする（減量の無いレース・減量の外れた騎手）。
    """
    entries = list(conn.execute(
        "SELECT seq, umaban, horse_name, jockey_id FROM upcoming_entries WHERE race_id = ?", (race_id,)
    ))
    by_umaban = {r["umaban"]: r for r in entries if r["umaban"] is not None}
    by_name = {r["horse_name"]: r for r in entries if r["horse_name"]}

    saved = 0
    with conn:
        for profile in profiles:
            entry = by_umaban.get(profile.umaban) or by_name.get(profile.horse_name)
            if entry is None or not profile.jockey_id or entry["jockey_id"] != profile.jockey_id:
                continue
            conn.execute(
                "UPDATE upcoming_entries SET kinryo_mark = ? WHERE race_id = ? AND seq = ?",
                (profile.kinryo_mark, race_id, entry["seq"]),
            )
            saved += 1
    return saved


def fill_kinryo_marks(conn: sqlite3.Connection, race_id: str | None = None) -> int:
    """entries.kinryo_mark を埋め直す。書き換えた行数を返す（`race_id` を渡せばそのレースだけ）。

    Targetの書き出し（target_runs）に行がある走はそれに従う（印が無ければNULL）。
    無い走（Targetを書き出した日より後）は、JRAの出馬表の印（upcoming_entries）を
    **同じ馬番・同じ騎手**のときだけ写す（乗り替わった走には前の騎手の印を付けない）。
    呼び手がトランザクションを持つ（save_race_page の中からも呼ぶため、ここではcommitしない）。
    """
    value = (
        "CASE WHEN EXISTS (SELECT 1 FROM target_runs t "
        "                  WHERE t.race_id = entries.race_id AND t.umaban = entries.umaban) "
        "THEN (SELECT NULLIF(t.kinryo_mark, '') FROM target_runs t "
        "      WHERE t.race_id = entries.race_id AND t.umaban = entries.umaban) "
        "ELSE (SELECT u.kinryo_mark FROM upcoming_entries u "
        "      WHERE u.race_id = entries.race_id AND u.umaban = entries.umaban "
        "        AND u.jockey_id = entries.jockey_id) END"
    )
    where, params = ("AND race_id = ?", (race_id,)) if race_id else ("", ())
    return conn.execute(
        f"UPDATE entries SET kinryo_mark = {value} WHERE kinryo_mark IS NOT {value} {where}", params
    ).rowcount


def latest_race_date(conn: sqlite3.Connection) -> date | None:
    """**馬ごとの結果まで**入っている最新の開催日（netkeibaの取得再開点に使う）。

    JRA由来のレース情報だけの行を数えてしまうと、`update` が「もう最新だ」と
    勘違いして馬ごとの結果を取りに行かなくなる。
    """
    row = conn.execute(
        "SELECT MAX(race_date) AS d FROM races ra "
        "WHERE EXISTS (SELECT 1 FROM results r WHERE r.race_id = ra.race_id)"
    ).fetchone()
    return date.fromisoformat(row["d"]) if row["d"] else None


# --- kaisai_days / calendar_months ------------------------------------------


def add_kaisai_days(conn: sqlite3.Connection, days: list[date]) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO kaisai_days (kaisai_date) VALUES (?) ON CONFLICT(kaisai_date) DO NOTHING",
            [(d.isoformat(),) for d in days],
        )


def incomplete_kaisai_days(conn: sqlite3.Connection, start: date, end: date) -> list[date]:
    rows = conn.execute(
        "SELECT kaisai_date FROM kaisai_days WHERE completed = 0 AND kaisai_date BETWEEN ? AND ? "
        "ORDER BY kaisai_date",
        (start.isoformat(), end.isoformat()),
    )
    return [date.fromisoformat(r["kaisai_date"]) for r in rows]


def kaisai_days_between(conn: sqlite3.Connection, start: date, end: date) -> list[date]:
    rows = conn.execute(
        "SELECT kaisai_date FROM kaisai_days WHERE kaisai_date BETWEEN ? AND ? ORDER BY kaisai_date",
        (start.isoformat(), end.isoformat()),
    )
    return [date.fromisoformat(r["kaisai_date"]) for r in rows]


def update_kaisai_day(conn: sqlite3.Connection, day: date, n_races: int, n_saved: int, completed: bool) -> None:
    with conn:
        conn.execute(
            "INSERT INTO kaisai_days (kaisai_date, n_races, n_saved, completed, checked_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kaisai_date) DO UPDATE SET n_races = excluded.n_races, n_saved = excluded.n_saved, "
            "completed = excluded.completed, checked_at = excluded.checked_at",
            (day.isoformat(), n_races, n_saved, int(completed), now_str()),
        )


def is_calendar_month_final(conn: sqlite3.Connection, year: int, month: int) -> bool:
    row = conn.execute(
        "SELECT is_final FROM calendar_months WHERE year_month = ?", (f"{year:04d}-{month:02d}",)
    ).fetchone()
    return bool(row and row["is_final"])


def mark_calendar_month(conn: sqlite3.Connection, year: int, month: int, is_final: bool) -> None:
    with conn:
        conn.execute(
            "INSERT INTO calendar_months (year_month, is_final, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(year_month) DO UPDATE SET is_final = excluded.is_final, fetched_at = excluded.fetched_at",
            (f"{year:04d}-{month:02d}", int(is_final), now_str()),
        )


# --- runs / logs -------------------------------------------------------------


def start_run(conn: sqlite3.Connection, command: str, args: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (command, args, started_at, status) VALUES (?, ?, ?, 'running')",
            (command, args, now_str()),
        )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str, n_requests: int, n_races_saved: int,
    n_warnings: int, message: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, n_requests = ?, n_races_saved = ?, "
            "n_warnings = ?, message = ? WHERE run_id = ?",
            (now_str(), status, n_requests, n_races_saved, n_warnings, message, run_id),
        )


def log_fetch(conn: sqlite3.Connection, run_id: int | None, url: str, status_code: int | None, ok: bool, message: str | None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO fetch_log (run_id, fetched_at, url, status_code, ok, message) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, now_str(), url, status_code, int(ok), message),
        )


def log_warning(conn: sqlite3.Connection, run_id: int | None, key: str | None, url: str | None, message: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO parse_warnings (run_id, created_at, url, key, message) VALUES (?, ?, ?, ?, ?)",
            (run_id, now_str(), url, key, message),
        )


# --- status ------------------------------------------------------------------


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in DATA_TABLES}


def race_date_range(conn: sqlite3.Connection) -> tuple[str | None, str | None, int]:
    row = conn.execute(
        "SELECT MIN(race_date) AS lo, MAX(race_date) AS hi, COUNT(DISTINCT race_date) AS n FROM races"
    ).fetchone()
    return row["lo"], row["hi"], row["n"]


def recent_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()


# --- upcoming_races / upcoming_entries ---------------------------------------

UPCOMING_RACE_COLUMNS = (
    "race_id", "race_date", "venue_code", "kaiji", "nichime", "race_no", "race_name", "grade",
    "surface", "direction", "distance_m", "course_detail", "post_time", "age_condition",
    "class_condition", "race_conditions", "n_entries",
)
UPCOMING_ENTRY_COLUMNS = (
    "race_id", "seq", "umaban", "waku", "horse_id", "horse_name", "sex", "age", "kinryo", "jockey_id",
    "jockey_name", "trainer_id", "trainer_name", "stable", "horse_weight", "weight_diff", "status",
    "kinryo_mark",
)


def _upsert_upcoming_race(conn: sqlite3.Connection, race: dict, entry_status: str, ts: str) -> None:
    """レース1件を上書き保存する。値がNULLの列は既存の値を残す（一覧→出馬表の順で情報が増えるため）。"""
    columns = UPCOMING_RACE_COLUMNS + ("entry_status", "fetched_at", "updated_at")
    updates = ", ".join(
        f"{c} = COALESCE(excluded.{c}, {c})" for c in UPCOMING_RACE_COLUMNS if c != "race_id"
    )
    # entry_statusは list_only → entries の向きにだけ進める（一覧の再取得で出馬表取得済みを戻さない）
    sql = (
        f"{_insert_sql('upcoming_races', columns)} ON CONFLICT(race_id) DO UPDATE SET {updates}, "
        "entry_status = CASE WHEN excluded.entry_status = 'list_only' THEN entry_status "
        "ELSE excluded.entry_status END, "
        "updated_at = excluded.updated_at"
    )
    conn.execute(sql, [race.get(c) for c in UPCOMING_RACE_COLUMNS] + [entry_status, ts, ts])


def save_upcoming_race_list(conn: sqlite3.Connection, day: date, races: list[dict]) -> int:
    """レース一覧から分かる範囲（R番号・レース名・発走時刻・コース・頭数）だけを保存する。"""
    ts = now_str()
    with conn:
        for item in races:
            race_info = decode_race_id(item["race_id"])
            surface, distance_m = parse_course_text(item.get("course_text"))
            _upsert_upcoming_race(
                conn,
                {
                    "race_id": item["race_id"],
                    "race_date": day.isoformat(),
                    "venue_code": race_info.venue_code,
                    "kaiji": race_info.kaiji,
                    "nichime": race_info.nichime,
                    "race_no": item.get("race_no") or race_info.race_no,
                    "race_name": item.get("race_name"),
                    "surface": surface,
                    "distance_m": distance_m,
                    "post_time": item.get("post_time"),
                    "n_entries": item.get("n_entries"),
                },
                "list_only",
                ts,
            )
    return len(races)


def save_upcoming_shutuba(conn: sqlite3.Connection, page: ShutubaPage) -> None:
    """出馬表1件を保存する。出走馬は毎回入れ直すので、騎手変更・取消・頭数変更が反映される。"""
    race_id = page.race["race_id"]
    if not page.race.get("race_date"):
        raise ValueError(f"開催日が取得できていないため保存できません: {race_id}")
    ts = now_str()
    # 枠順が確定していれば entries、出走馬だけ判明している段階は registered
    entry_status = "entries" if any(e.get("umaban") for e in page.entries) else "registered"
    entries = _keep_known_marks(conn, race_id, _keep_known_weights(conn, race_id, page.entries))
    with conn:
        _upsert_upcoming_race(conn, page.race, entry_status, ts)
        conn.execute("DELETE FROM upcoming_entries WHERE race_id = ?", (race_id,))
        conn.executemany(
            _insert_sql("upcoming_entries", UPCOMING_ENTRY_COLUMNS),
            [[e.get(c) for c in UPCOMING_ENTRY_COLUMNS] for e in entries],
        )


def _keep_known_weights(conn: sqlite3.Connection, race_id: str, entries: list[dict]) -> list[dict]:
    """すでに入っている馬体重を引き継いだ出走馬のリストを返す。

    出走馬は入れ直し（DELETE→INSERT）なので、そのままだと**netkeibaの出馬表を
    取り直したときにJRAから取った馬体重が消えてしまう**。届いた出馬表に馬体重が
    無い馬だけ、前の値を持ち越す（馬IDで、無ければ馬名で突き合わせる）。
    """
    known = {}
    for row in conn.execute(
        "SELECT horse_id, horse_name, horse_weight, weight_diff FROM upcoming_entries "
        "WHERE race_id = ? AND horse_weight IS NOT NULL", (race_id,)
    ):
        weight = (row["horse_weight"], row["weight_diff"])
        if row["horse_id"]:
            known[row["horse_id"]] = weight
        if row["horse_name"]:
            known.setdefault(row["horse_name"], weight)
    if not known:
        return entries

    kept = []
    for entry in entries:
        weight = known.get(entry.get("horse_id")) or known.get(entry.get("horse_name"))
        if weight is None or entry.get("horse_weight") is not None:
            kept.append(entry)
            continue
        kept.append({**entry, "horse_weight": weight[0], "weight_diff": weight[1]})
    return kept


def _keep_known_marks(conn: sqlite3.Connection, race_id: str, entries: list[dict]) -> list[dict]:
    """JRAの出馬表から入れた減量騎手の印を、netkeibaの出馬表を取り直しても残す。

    印は騎手に付くので、**同じ馬に同じ騎手が乗る行だけ**持ち越す（乗り替わりなら捨てる）。
    """
    known = {
        (row["horse_id"], row["jockey_id"]): row["kinryo_mark"]
        for row in conn.execute(
            "SELECT horse_id, jockey_id, kinryo_mark FROM upcoming_entries "
            "WHERE race_id = ? AND kinryo_mark IS NOT NULL AND horse_id IS NOT NULL", (race_id,)
        )
    }
    if not known:
        return entries
    return [
        {**e, "kinryo_mark": known[(e.get("horse_id"), e.get("jockey_id"))]}
        if e.get("kinryo_mark") is None and (e.get("horse_id"), e.get("jockey_id")) in known else e
        for e in entries
    ]


def parse_course_text(text: str | None) -> tuple[str | None, int | None]:
    """レース一覧の「芝2200m」「ダ1800m」「障2880m」を (surface, distance_m) に分解する。"""
    if not text:
        return None, None
    m = re.match(r"(芝|ダ|障)(\d+)m", text.strip())
    if not m:
        return None, None
    return {"芝": "turf", "ダ": "dirt", "障": "jump"}[m.group(1)], int(m.group(2))


# --- track_conditions（クッション値・含水率） ---------------------------------

TRACK_CONDITION_COLUMNS = (
    "venue_code", "date", "nichime", "day_label", "is_pre_meeting_day", "course_setting",
    "cushion_value", "turf_moisture_goal", "turf_moisture_4corner",
    "dirt_moisture_goal", "dirt_moisture_4corner", "measured_at",
    "going_turf", "going_dirt", "weather", "source_pdf",
)


def upsert_track_conditions(
    conn: sqlite3.Connection, records: list, keep_existing: bool = False
) -> int:
    """馬場情報を保存する（同じ日を何度読んでも増えない）。保存した件数を返す。

    keep_existing=True のときは、**値が無い項目で既存の値を消さない**
    （`COALESCE(excluded.col, 既存)`）。当日の馬場情報ページ（scrapers/baba.py）は
    開催◯日目や使用コースA/B/Cを持たないので、PDF由来のそれらを残すために使う。
    """
    rows = [
        [
            r.venue_code, r.date, r.nichime, r.day_label, int(r.is_pre_meeting_day), r.course_setting,
            r.cushion_value, r.turf_moisture_goal, r.turf_moisture_4corner,
            r.dirt_moisture_goal, r.dirt_moisture_4corner,
            getattr(r, "measured_at", None),
            getattr(r, "going_turf", None), getattr(r, "going_dirt", None),
            getattr(r, "weather", None), r.source_pdf, now_str(),
        ]
        for r in records
        if r.venue_code and r.date
    ]
    if not rows:
        return 0
    columns = TRACK_CONDITION_COLUMNS + ("updated_at",)
    keys = ("venue_code", "date", "updated_at")
    if keep_existing:
        updates = ", ".join(
            f"{c} = COALESCE(excluded.{c}, track_conditions.{c})"
            for c in columns if c not in keys
        )
        updates += ", updated_at = excluded.updated_at"
    else:
        updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in ("venue_code", "date"))
    with conn:
        conn.executemany(
            f"{_insert_sql('track_conditions', columns)} "
            f"ON CONFLICT(venue_code, date) DO UPDATE SET {updates}",
            rows,
        )
    return len(rows)


def cushion_coverage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(クッション値が付く開催日数, DBの開催日数) を返す。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(EXISTS(
                   SELECT 1 FROM track_conditions t
                   WHERE t.venue_code = d.venue_code AND t.date = d.race_date
                         AND t.cushion_value IS NOT NULL
               )) AS covered
        FROM (SELECT DISTINCT venue_code, race_date FROM races) d
        """
    ).fetchone()
    return int(row["covered"] or 0), int(row["total"] or 0)


BOARD_COLUMNS = (
    "race_id", "horse_id", "tier", "position", "lane_offset", "comment", "is_manual",
    "is_excluded",
)


def board_record(row: dict) -> dict | None:
    """画面から届いた1頭ぶんを、保存する形に整える。**保存しない馬は None**。

    手を入れていない馬（動かしてもいない・コメントも無い・消してもいない）は保存しない。
    位置（Tier・横位置・段）は**手で動かした馬のときだけ**覚える。
    消しただけ・コメントだけの馬にも位置を残すと、次に開いたときに
    「保存済みの引き伸ばし後の値」と「他の馬の生の値」が混ざり、
    そこからもう一度引き伸ばされて**全馬の位置がずれる**。

    SQLite（save_board）と共有ストア（keiba-app の同期）の両方がこの判定を使う。
    """
    touched = (
        row.get("is_manual")
        or row.get("is_excluded")          # 消しただけの馬も残す
        or (row.get("comment") or "").strip()
    )
    if not row.get("horse_id") or not touched:
        return None
    placed = bool(row.get("is_manual"))
    return {
        "horse_id": row["horse_id"],
        "tier": row.get("tier") if placed else None,
        "position": row.get("position") if placed else None,
        "lane_offset": row.get("lane_offset") if placed else None,
        "comment": (row.get("comment") or "").strip() or None,
        "is_manual": 1 if placed else 0,
        "is_excluded": 1 if row.get("is_excluded") else 0,
    }


def save_board(conn: sqlite3.Connection, race_id: str, rows: list[dict]) -> int:
    """予想ボードの配置とコメントを保存する（レース×馬で1行）。保存した件数を返す。

    画面の「保存」を押したときに、そのレースの全馬ぶんをまとめて受け取る。
    残すかどうか・何を残すかは `board_record` が決める。
    自動仮配置は開くたびに最新のデータで作り直したいので、
    DBに残すのは「手を入れた結果」だけにしている。
    """
    now = now_str()
    keep, drop = [], []
    for row in rows:
        horse_id = row.get("horse_id")
        if not horse_id:
            continue
        record = board_record(row)
        if record is None:
            drop.append((race_id, horse_id))
            continue
        keep.append([
            race_id, horse_id, record["tier"], record["position"], record["lane_offset"],
            record["comment"], record["is_manual"], record["is_excluded"], now,
        ])
    columns = BOARD_COLUMNS + ("updated_at",)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in ("race_id", "horse_id"))
    with conn:
        if drop:   # 手を入れたあとで元に戻した馬は、行ごと消して自動配置に返す
            conn.executemany("DELETE FROM board_horses WHERE race_id = ? AND horse_id = ?", drop)
        if keep:
            conn.executemany(
                f"{_insert_sql('board_horses', columns)} "
                f"ON CONFLICT(race_id, horse_id) DO UPDATE SET {updates}",
                keep,
            )
    return len(keep)


def clear_board(conn: sqlite3.Connection, race_id: str) -> int:
    """そのレースのボードを自動仮配置の状態に戻す（手を入れた結果を捨てる）。"""
    with conn:
        cursor = conn.execute("DELETE FROM board_horses WHERE race_id = ?", (race_id,))
    return cursor.rowcount


# --- 予想印 ---------------------------------------------------------------------

# 付けられる印（この順が「印順」の並び）。「消」は買わない馬（画面ではグレーにする）
MARKS = ("◎", "○", "▲", "△", "☆", "✓", "消")


def get_marks(conn: sqlite3.Connection, race_id: str) -> dict[str, str]:
    """そのレースの予想印 `{馬ID: 印}`（印を付けた馬だけ）。"""
    rows = conn.execute("SELECT horse_id, mark FROM horse_marks WHERE race_id = ?", (race_id,))
    return {r["horse_id"]: r["mark"] for r in rows}


def save_mark(conn: sqlite3.Connection, race_id: str, horse_id: str, mark: str | None) -> None:
    """1頭の予想印を付ける。`MARKS` に無い印（None・「--」など）は外す（行ごと消す）。"""
    with conn:
        if mark not in MARKS:
            conn.execute(
                "DELETE FROM horse_marks WHERE race_id = ? AND horse_id = ?", (race_id, horse_id)
            )
            return
        conn.execute(
            "INSERT INTO horse_marks (race_id, horse_id, mark, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(race_id, horse_id) DO UPDATE SET "
            "mark = excluded.mark, updated_at = excluded.updated_at",
            (race_id, horse_id, mark, now_str()),
        )


# --- オッズ（JRA公式） ---------------------------------------------------------

ODDS_COLUMNS = ("race_id", "bet_type", "combo", "odds_low", "odds_high")


def save_odds(conn: sqlite3.Connection, page) -> int:
    """1レース1券種ぶんのオッズを保存する（`jra_odds.OddsPage`）。保存した点数を返す。

    オッズは**最新だけ**を残すので、その (レース, 券種) の行をいったん消してから入れ直す。
    途中で券種の点数が減っても古い組み合わせが残らない。
    """
    rows = [
        (page.race_id, page.bet_type, row["combo"], row.get("odds_low"), row.get("odds_high"))
        for row in page.rows
    ]
    with conn:
        conn.execute(
            "DELETE FROM odds WHERE race_id = ? AND bet_type = ?", (page.race_id, page.bet_type)
        )
        if rows:
            conn.executemany(_insert_sql("odds", ODDS_COLUMNS), rows)
        conn.execute(
            "INSERT INTO odds_updates (race_id, bet_type, odds_label, n_combos, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(race_id, bet_type) DO UPDATE SET "
            "odds_label = excluded.odds_label, n_combos = excluded.n_combos, "
            "fetched_at = excluded.fetched_at",
            (page.race_id, page.bet_type, page.odds_label, len(rows), now_str()),
        )
    return len(rows)


def save_jra_odds_links(conn: sqlite3.Connection, links: dict[str, dict[str, str]]) -> int:
    """オッズのページを開くトークンを `{race_id: {券種: cname}}` の形で保存する。

    末尾2文字はチェックサムなので組み立てられない。いちどためておけば、券種の
    切り替えが1リクエストで済む。発売されていない券種は入ってこないので、欠けるのは正常。
    """
    rows = [
        (race_id, bet_type, cname)
        for race_id, per_bet in links.items()
        for bet_type, cname in per_bet.items()
    ]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO jra_odds_links (race_id, bet_type, cname) VALUES (?, ?, ?) "
            "ON CONFLICT(race_id, bet_type) DO UPDATE SET cname = excluded.cname",
            rows,
        )
    return len(rows)


def get_jra_odds_links(conn: sqlite3.Connection, race_id: str) -> dict[str, str]:
    """そのレースのオッズのトークン `{券種: cname}`。"""
    rows = conn.execute(
        "SELECT bet_type, cname FROM jra_odds_links WHERE race_id = ?", (race_id,)
    )
    return {r["bet_type"]: r["cname"] for r in rows}


def save_jra_entry_links(conn: sqlite3.Connection, links: dict[str, str]) -> int:
    """JRA公式の出馬表を開くトークンを `{race_id: cname}` の形で保存する（オッズと同じ理由）。"""
    if not links:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO jra_entry_links (race_id, cname) VALUES (?, ?) "
            "ON CONFLICT(race_id) DO UPDATE SET cname = excluded.cname",
            list(links.items()),
        )
    return len(links)


def get_jra_entry_link(conn: sqlite3.Connection, race_id: str) -> str | None:
    """そのレースのJRA出馬表のトークン（無ければNone）。"""
    row = conn.execute(
        "SELECT cname FROM jra_entry_links WHERE race_id = ?", (race_id,)
    ).fetchone()
    return row["cname"] if row else None


# --- 買い目（自分で組んだ組み合わせと金額） -----------------------------------

BET_SLIP_COLUMNS = ("race_id", "group_id", "bet_type", "combo", "amount_yen", "kind", "seq")
_COMBO_RE = re.compile(r"^\d+(-\d+)*$")


def normalize_slip_groups(groups: list[dict] | None) -> list[dict]:
    """画面から届いた買い目を、保存する形に整える（ブラウザから届く値なので形を確かめて捨てる）。

    `[{"bet": "umaren", "combos": ["1-2"], "amount": 100, "kind": "ながし"}, …]` を返す。
    券種が知らないもの・組み合わせが空のまとまりは捨て、同じ組み合わせは1つにする。
    金額は**1点あたり**で、負の値は0にする。SQLiteと共有ストアの両方が使う。
    """
    known = {bet.key for bet in BET_TYPES}
    out = []
    for group in groups or []:
        bet_type = group.get("bet")
        combos = [str(c) for c in (group.get("combos") or []) if _COMBO_RE.match(str(c))]
        if bet_type not in known or not combos:
            continue
        out.append({
            "bet": bet_type,
            "combos": list(dict.fromkeys(combos)),          # 同じ組み合わせは1つに
            "amount": max(0, int(group.get("amount") or 0)),
            "kind": (group.get("kind") or "").strip() or None,
        })
    return out


def save_bet_slips(conn: sqlite3.Connection, race_id: str, groups: list[dict]) -> int:
    """そのレースの買い目を**まるごと入れ替える**。残ったまとまりの数を返す。

    画面から届くのは「いま右カラムに積まれているすべて」なので、消した買い目が
    残らないよう、いったん消してから入れ直す（オッズと同じ考え方）。
    形の確かめは `normalize_slip_groups`。`kind` は買い方（通常 / フォーメーション /
    ながし / ボックス）で、無くてもよい。番号（group_id）は**受け取った並びで振り直す**ので、
    画面側が同じ番号を2つ送ってきても主キーがぶつからない。
    """
    now, rows = now_str(), []
    kept = normalize_slip_groups(groups)
    for group_id, group in enumerate(kept, start=1):
        for seq, combo in enumerate(group["combos"]):
            rows.append((race_id, group_id, group["bet"], combo, group["amount"], group["kind"], seq, now))
    with conn:
        conn.execute("DELETE FROM bet_slips WHERE race_id = ?", (race_id,))
        if rows:
            conn.executemany(_insert_sql("bet_slips", BET_SLIP_COLUMNS + ("updated_at",)), rows)
    return len(kept)


def get_bet_slips(conn: sqlite3.Connection, race_id: str) -> list[dict]:
    """そのレースの買い目。`[{"group": 1, "bet": "umaren", "combos": [...], "amount": 100}]`。"""
    rows = conn.execute(
        "SELECT group_id, bet_type, combo, amount_yen, kind FROM bet_slips WHERE race_id = ? "
        "ORDER BY group_id, seq",
        (race_id,),
    )
    groups: dict[int, dict] = {}
    for row in rows:
        group = groups.setdefault(row["group_id"], {
            "group": row["group_id"], "bet": row["bet_type"],
            "combos": [], "amount": row["amount_yen"], "kind": row["kind"],
        })
        group["combos"].append(row["combo"])
    return list(groups.values())


def bet_slips_updated_at(conn: sqlite3.Connection, race_id: str) -> str | None:
    """そのレースの買い目を最後に保存した時刻（画面が自分の保存と見分けるのに使う）。"""
    row = conn.execute(
        "SELECT MAX(updated_at) AS at FROM bet_slips WHERE race_id = ?", (race_id,)
    ).fetchone()
    return row["at"] if row else None


def clear_bet_slips(conn: sqlite3.Connection, race_id: str) -> int:
    """そのレースの買い目を消す。"""
    with conn:
        cursor = conn.execute("DELETE FROM bet_slips WHERE race_id = ?", (race_id,))
    return cursor.rowcount


# --- 種牡馬リーディング（JRA公式） -------------------------------------------------

SIRE_LEADING_COLUMNS = (
    "year", "kind", "sire_name", "rank", "birth_year", "coat_color", "birthplace",
    "n_horses", "n_winners", "n_starts", "n_wins", "prize_yen", "prize_per_start",
    "prize_per_horse", "win_rate", "ei", "as_of",
)


def save_sire_leading(conn: sqlite3.Connection, page) -> int:
    """種牡馬リーディング1ページぶんを保存する（同じ年・種類・種牡馬なら上書き）。

    当年は集計途中なので、取り直すたびに数字が伸びる。`as_of`（◯月◯日現在）を
    一緒に残しておくと、画面でいつ時点の数字かを出せる。
    """
    if page.year is None or not page.rows:
        return 0
    now = now_str()
    rows = [
        (page.year, page.kind, r.sire_name, r.rank, r.birth_year, r.coat_color, r.birthplace,
         r.n_horses, r.n_winners, r.n_starts, r.n_wins, r.prize_yen, r.prize_per_start,
         r.prize_per_horse, r.win_rate, r.ei, page.as_of, now)
        for r in page.rows
    ]
    columns = ", ".join((*SIRE_LEADING_COLUMNS, "fetched_at"))
    placeholders = ", ".join("?" * (len(SIRE_LEADING_COLUMNS) + 1))
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in (*SIRE_LEADING_COLUMNS, "fetched_at")
        if c not in ("year", "kind", "sire_name")
    )
    with conn:
        conn.executemany(
            f"INSERT INTO sire_leading ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(year, kind, sire_name) DO UPDATE SET {updates}",
            rows,
        )
    return len(rows)


def save_jra_sire_links(conn: sqlite3.Connection, kind: str, links: dict[int, dict[int, str]]) -> int:
    """リーディングのページを開くトークンを `{年: {ページ: cname}}` の形で保存する。

    末尾2文字はチェックサムなので組み立てられない（オッズ・出馬表と同じ理由）。
    いちどためておけば、2回目からは入口を辿らずに年度を直接開ける。
    """
    now = now_str()
    rows = [
        (kind, year, page, cname, now)
        for year, per_page in links.items()
        for page, cname in per_page.items()
    ]
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT INTO jra_sire_links (kind, year, page, cname, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, year, page) DO UPDATE SET "
            "cname = excluded.cname, updated_at = excluded.updated_at",
            rows,
        )
    return len(rows)


def get_jra_sire_links(conn: sqlite3.Connection, kind: str) -> dict[int, dict[int, str]]:
    """ためてあるトークン `{年: {ページ: cname}}`。"""
    links: dict[int, dict[int, str]] = {}
    for row in conn.execute(
        "SELECT year, page, cname FROM jra_sire_links WHERE kind = ?", (kind,)
    ):
        links.setdefault(row["year"], {})[row["page"]] = row["cname"]
    return links


# --- 血統（JRA公式の競走馬検索・詳細ページ） ---------------------------------------

# 詳細ページから入れる列（`save_horse_profiles` の HORSE_PROFILE_COLUMNS に足すもの）
HORSE_DETAIL_COLUMNS = ("birth_date", "sire_no", "broodmare_sire_no", "trainer_name", "sex")


def horses_without_pedigree(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """父がまだ入っていない馬（**実際に出走した馬だけ**）。

    名前順に返すのは、頭文字でまとめて検索するため。生年は出走時の年齢から出す
    （JRAの馬齢は暦年なので、`レースの年 - 馬齢` が生年になる）。
    同じ馬が複数年走っていれば同じ値になるはずなので、最小値を採る。
    """
    sql = """
        SELECT h.horse_id, h.horse_name, h.sex,
               MIN(CAST(substr(ra.race_date, 1, 4) AS INTEGER) - e.age) AS birth_year
          FROM horses h
          JOIN entries e ON e.horse_id = h.horse_id
          JOIN races   ra ON ra.race_id = e.race_id
         WHERE (h.sire IS NULL OR h.sire = '')
           AND h.horse_name IS NOT NULL AND h.horse_name <> ''
      GROUP BY h.horse_id
      ORDER BY h.horse_name
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql)]


def count_pedigree(conn: sqlite3.Connection) -> tuple[int, int]:
    """(父が入っている馬, 出走した馬の総数)。取り込みの進み具合の表示に使う。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN h.sire IS NOT NULL AND h.sire <> '' THEN 1 ELSE 0 END) AS done "
        "  FROM horses h WHERE EXISTS (SELECT 1 FROM entries e WHERE e.horse_id = h.horse_id)"
    ).fetchone()
    return (row["done"] or 0, row["total"] or 0)


def save_jra_horse_links(conn: sqlite3.Connection, links: dict[str, tuple[str, str]]) -> int:
    """詳細ページのトークンを `{horse_id: (cname, 血統登録番号)}` の形で保存する。"""
    if not links:
        return 0
    now = now_str()
    with conn:
        conn.executemany(
            "INSERT INTO jra_horse_links (horse_id, cname, horse_no, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(horse_id) DO UPDATE SET cname = excluded.cname, "
            "horse_no = excluded.horse_no, updated_at = excluded.updated_at",
            [(h, c, n, now) for h, (c, n) in links.items()],
        )
    return len(links)


def get_jra_horse_links(conn: sqlite3.Connection, horse_ids: list[str]) -> dict[str, str]:
    """ためてあるトークン `{horse_id: cname}`。"""
    if not horse_ids:
        return {}
    marks = ", ".join("?" * len(horse_ids))
    rows = conn.execute(
        f"SELECT horse_id, cname FROM jra_horse_links WHERE horse_id IN ({marks})", horse_ids
    )
    return {r["horse_id"]: r["cname"] for r in rows}


def save_jra_horse_search(conn: sqlite3.Connection, prefix: str, n_hits: int, capped: bool) -> None:
    """検索し終えた頭文字を覚えておく（同じ頭を二度引かないため）。"""
    with conn:
        conn.execute(
            "INSERT INTO jra_horse_search (prefix, n_hits, capped, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(prefix) DO UPDATE SET n_hits = excluded.n_hits, "
            "capped = excluded.capped, updated_at = excluded.updated_at",
            (prefix, n_hits, int(capped), now_str()),
        )


def searched_prefixes(conn: sqlite3.Connection) -> dict[str, bool]:
    """検索済みの頭文字 `{頭文字: 上限に達したか}`。"""
    return {r["prefix"]: bool(r["capped"]) for r in
            conn.execute("SELECT prefix, capped FROM jra_horse_search")}


def save_horse_pedigree(conn: sqlite3.Connection, horse_id: str, detail) -> int:
    """競走馬の詳細ページから読んだ血統を horses に入れる（1頭ぶん）。

    出馬表からの `save_horse_profiles` と違って**馬IDが分かっている**ので、
    馬番や馬名で突き合わせる必要が無い。値がNULLの列は既存の値を残す。
    """
    profile = detail.profile
    columns = (*HORSE_PROFILE_COLUMNS, *HORSE_DETAIL_COLUMNS)
    values = (
        profile.sire, profile.dam, profile.broodmare_sire, profile.owner, profile.breeder,
        detail.birth_date, detail.sire_no, detail.broodmare_sire_no, detail.trainer_name,
        detail.sex,
    )
    updates = ", ".join(f"{c} = COALESCE(excluded.{c}, {c})" for c in columns)
    with conn:
        conn.execute(
            f"INSERT INTO horses (horse_id, horse_name, {', '.join(columns)}, updated_at) "
            f"VALUES (?, ?, {', '.join('?' * len(columns))}, ?) "
            "ON CONFLICT(horse_id) DO UPDATE SET "
            f"horse_name = COALESCE(excluded.horse_name, horse_name), {updates}, "
            "updated_at = excluded.updated_at",
            (horse_id, profile.horse_name or None, *values, now_str()),
        )
    return 1


# --- 5代血統表（netkeibaの血統表） -------------------------------------------------

def save_horse_pedigree_tree(conn: sqlite3.Connection, horse_id: str, cells: list) -> int:
    """1頭ぶんの5代血統表を保存する（**その馬の行を消してから入れ直す**）。

    パーサを直して62マスの中身が変わったときに、古い行が残らないようにする
    （entries/results と同じ考え方）。祖先そのものは `pedigree_horses` に入れる。
    """
    if not cells:
        return 0
    now = now_str()
    with conn:
        conn.executemany(
            "INSERT INTO pedigree_horses (horse_no, name, country, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(horse_no) DO UPDATE SET name = excluded.name, "
            "country = excluded.country, updated_at = excluded.updated_at",
            [(c.horse_no, c.name, c.country, now) for c in cells],
        )
        conn.execute("DELETE FROM horse_ancestors WHERE horse_id = ?", (horse_id,))
        conn.executemany(
            "INSERT INTO horse_ancestors (horse_id, path, generation, ancestor_no) "
            "VALUES (?, ?, ?, ?)",
            [(horse_id, c.path, c.generation, c.horse_no) for c in cells],
        )
    return len(cells)


def horses_without_bloodline(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """5代血統表がまだ入っていない馬（**実際に出走した馬だけ**）。

    血統表が入っているかは `horse_ancestors` に netkeiba の行があるかで見る
    （手元のデータで組んだ local の行は欠けがあるので、入っていないものとして数える）。取り込みは
    1頭ずつなので、途中で止めても次はここから続きが始まる。

    **新しい世代から取る**（`horse_id` は先頭4桁が生年なので降順＝新しい順）。
    全部で21時間かかるので、途中で止めたときに手元に残るのが
    一口出資で見たい直近の世代になるようにする。
    """
    sql = """
        SELECT h.horse_id, h.horse_name
          FROM horses h
         WHERE EXISTS (SELECT 1 FROM entries e WHERE e.horse_id = h.horse_id)
           AND NOT EXISTS (SELECT 1 FROM horse_ancestors a
                            WHERE a.horse_id = h.horse_id AND a.source = 'netkeiba')
      ORDER BY h.horse_id DESC
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql)]


def count_local_bloodline(conn: sqlite3.Connection) -> tuple[int, float]:
    """(手元のデータで組んだ5代血統表がある馬, 1頭あたりの平均マス数)。"""
    row = conn.execute(
        "SELECT COUNT(DISTINCT horse_id) AS horses, COUNT(*) AS cells FROM horse_ancestors WHERE source = 'local'"
    ).fetchone()
    horses = row["horses"] or 0
    return horses, (row["cells"] / horses if horses else 0.0)


def count_bloodline(conn: sqlite3.Connection) -> tuple[int, int]:
    """(netkeiba の5代血統表が入っている馬, 出走した馬の総数)。進み具合の表示に使う。"""
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN EXISTS (SELECT 1 FROM horse_ancestors a "
        "                              WHERE a.horse_id = h.horse_id AND a.source = 'netkeiba') "
        "           THEN 1 ELSE 0 END) AS done "
        "  FROM horses h "
        " WHERE EXISTS (SELECT 1 FROM entries e WHERE e.horse_id = h.horse_id)"
    ).fetchone()
    return (row["done"] or 0, row["total"] or 0)
