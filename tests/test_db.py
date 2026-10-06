from keiba_data import db, race_names
from keiba_data.scrapers.race_result import parse_race_result
from tests.conftest import load_fixture


def _page(fixture="result_202606040411_g2.html", race_id="202606040411"):
    return parse_race_result(load_fixture(fixture), race_id)


def test_save_race_page(conn):
    db.save_race_page(conn, _page())
    counts = db.table_counts(conn)
    assert counts["races"] == 1
    assert counts["entries"] == counts["results"] == 16
    assert counts["payouts"] == 12
    assert counts["race_laps"] == 11
    assert counts["horses"] == 16

    row = conn.execute(
        """
        SELECT r.race_name, h.horse_name, j.jockey_name, t.trainer_name, t.stable, res.win_odds
        FROM results res
        JOIN races r USING (race_id)
        JOIN entries e USING (race_id, umaban)
        JOIN horses h ON h.horse_id = e.horse_id
        JOIN jockeys j ON j.jockey_id = e.jockey_id
        JOIN trainers t ON t.trainer_id = e.trainer_id
        WHERE res.finish_position = 1
        """
    ).fetchone()
    assert tuple(row) == ("第80回朝日セントライト記念", "ジャスティンシカゴ", "原優介", "宮田敬介", "美浦", 50.6)


def test_save_twice_does_not_duplicate(conn):
    db.save_race_page(conn, _page())
    first = db.table_counts(conn)
    fetched_at = conn.execute("SELECT fetched_at FROM races").fetchone()[0]

    db.save_race_page(conn, _page())
    assert db.table_counts(conn) == first
    assert conn.execute("SELECT fetched_at FROM races").fetchone()[0] == fetched_at


def test_masters_are_shared_across_races(conn):
    db.save_race_page(conn, _page())
    db.save_race_page(conn, _page("result_202406010209_dead_heat.html", "202406010209"))
    assert db.table_counts(conn)["races"] == 2
    assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 10


def test_resave_replaces_child_rows(conn):
    page = _page()
    db.save_race_page(conn, page)
    page.payouts = page.payouts[:1]
    db.save_race_page(conn, page)
    assert db.table_counts(conn)["payouts"] == 1


# --- 未開催レース ------------------------------------------------------------


def _shutuba(race_id="202606040611"):
    from keiba_data.scrapers.shutuba import parse_shutuba

    return parse_shutuba(load_fixture("shutuba_202606040611.html"), race_id, "2026-09-20")


def test_save_upcoming_shutuba(conn):
    db.save_upcoming_shutuba(conn, _shutuba())
    counts = db.table_counts(conn)
    assert counts["upcoming_races"] == 1
    assert counts["upcoming_entries"] == 13
    row = conn.execute("SELECT * FROM upcoming_races").fetchone()
    assert (row["race_name"], row["grade"], row["entry_status"]) == ("産経賞オールカマー", "GII", "entries")
    # 確定データ側には何も入らない
    assert counts["races"] == 0 and counts["entries"] == 0


def test_save_upcoming_twice_does_not_duplicate(conn):
    db.save_upcoming_shutuba(conn, _shutuba())
    first = db.table_counts(conn)
    db.save_upcoming_shutuba(conn, _shutuba())
    assert db.table_counts(conn) == first


def test_resave_reflects_scratched_horse_and_jockey_change(conn):
    """再取得で出走取消・騎手変更が反映されること（古い行が残らないこと）。"""
    db.save_upcoming_shutuba(conn, _shutuba())
    page = _shutuba()
    page.entries = page.entries[:-1]  # 1頭取消で12頭になった想定
    page.entries[0]["jockey_id"], page.entries[0]["jockey_name"] = ("01179", "ムルザバエフ")
    page.race["n_entries"] = 12
    db.save_upcoming_shutuba(conn, page)

    assert db.table_counts(conn)["upcoming_entries"] == 12
    assert conn.execute("SELECT n_entries FROM upcoming_races").fetchone()[0] == 12
    row = conn.execute("SELECT jockey_name FROM upcoming_entries WHERE umaban = 1").fetchone()
    assert row["jockey_name"] == "ムルザバエフ"
    assert conn.execute("SELECT COUNT(*) FROM upcoming_entries WHERE umaban = 13").fetchone()[0] == 0


def test_save_race_list_then_shutuba_keeps_details(conn):
    """レース一覧だけ先に保存 → 出馬表を保存、の順で情報が増えること。"""
    from datetime import date

    from keiba_data.scrapers.race_list import parse_race_list_details

    races = parse_race_list_details(load_fixture("race_list_20260920.html"))
    db.save_upcoming_race_list(conn, date(2026, 9, 20), races)
    assert db.table_counts(conn)["upcoming_races"] == 24
    row = conn.execute("SELECT * FROM upcoming_races WHERE race_id = '202606040611'").fetchone()
    assert (row["entry_status"], row["race_name"], row["surface"], row["distance_m"]) == ("list_only", "オールカマー", "turf", 2200)

    db.save_upcoming_shutuba(conn, _shutuba())
    row = conn.execute("SELECT * FROM upcoming_races WHERE race_id = '202606040611'").fetchone()
    assert (row["entry_status"], row["race_name"], row["grade"]) == ("entries", "産経賞オールカマー", "GII")

    # 一覧を取り直しても、出馬表取得済みの状態は戻らない
    db.save_upcoming_race_list(conn, date(2026, 9, 20), races)
    row = conn.execute("SELECT * FROM upcoming_races WHERE race_id = '202606040611'").fetchone()
    assert row["entry_status"] == "entries"
    assert row["grade"] == "GII"  # 一覧に無い情報が消えない


def test_save_upcoming_before_draw(conn):
    """枠順確定前は entry_status='registered' になり、確定後に 'entries' へ進む。"""
    from keiba_data.scrapers.shutuba import parse_shutuba

    before = parse_shutuba(load_fixture("shutuba_202609040703_before_draw.html"), "202609040703", "2026-09-21")
    db.save_upcoming_shutuba(conn, before)
    row = conn.execute("SELECT entry_status FROM upcoming_races").fetchone()
    assert row["entry_status"] == "registered"
    assert db.table_counts(conn)["upcoming_entries"] == 13
    assert conn.execute("SELECT COUNT(*) FROM upcoming_entries WHERE umaban IS NULL").fetchone()[0] == 13

    # 枠順が確定したものとして同じrace_idで上書きする
    after = parse_shutuba(load_fixture("shutuba_202606040611.html"), "202609040703", "2026-09-21")
    db.save_upcoming_shutuba(conn, after)
    row = conn.execute("SELECT entry_status FROM upcoming_races").fetchone()
    assert row["entry_status"] == "entries"
    assert conn.execute("SELECT COUNT(*) FROM upcoming_entries WHERE umaban IS NULL").fetchone()[0] == 0
    assert db.table_counts(conn)["upcoming_entries"] == 13


def test_upsert_track_conditions_keeps_existing_values(conn):
    """当日の馬場情報（使用コースや開催日目を持たない）でPDF由来の値を消さない。"""
    from keiba_data.cushion import TrackCondition

    pdf = TrackCondition(
        venue_code="06", date="2026-09-05", nichime=1, day_label="第1日",
        is_pre_meeting_day=False, course_setting="B", cushion_value=8.0,
        turf_moisture_goal=12.0, turf_moisture_4corner=12.5,
        dirt_moisture_goal=5.0, dirt_moisture_4corner=5.5, source_pdf="archive.pdf",
    )
    db.upsert_track_conditions(conn, [pdf])

    html = TrackCondition(
        venue_code="06", date="2026-09-05", nichime=None, day_label=None,
        is_pre_meeting_day=False, course_setting=None, cushion_value=8.5,
        turf_moisture_goal=None, turf_moisture_4corner=None,
        dirt_moisture_goal=None, dirt_moisture_4corner=None,
        source_pdf="https://www.jra.go.jp/keiba/baba/_data_cushion.html",
        measured_at="2026-09-05 07:00",
    )
    db.upsert_track_conditions(conn, [html], keep_existing=True)

    row = dict(conn.execute("SELECT * FROM track_conditions WHERE venue_code='06' AND date='2026-09-05'").fetchone())
    assert row["cushion_value"] == 8.5            # 新しい値は入る
    assert row["measured_at"] == "2026-09-05 07:00"
    assert row["course_setting"] == "B"           # PDF由来の値は消さない
    assert row["nichime"] == 1
    assert row["turf_moisture_goal"] == 12.0


# --- JRA公式由来の保存（レース情報とラップだけ） -------------------------------

JRA_RACE = {
    "race_id": "202606040601", "race_date": "2026-09-20", "venue_code": "06",
    "kaiji": 4, "nichime": 6, "race_no": 1, "race_name": "2歳未勝利", "grade": None,
    "surface": "dirt", "direction": "right", "distance_m": 1800, "course_detail": None,
    "weather": "雨", "going_turf": None, "going_dirt": "不良", "post_time": "10:00",
    "age_condition": "2歳", "class_condition": "未勝利", "race_conditions": "混,指,馬齢",
    "condition_raw": "2歳 未勝利 （混合）［指定］ 馬齢", "n_runners": 15,
}
JRA_LAPS = [
    {"race_id": "202606040601", "seq": i, "distance_m": 200 * i, "lap_sec": 12.0}
    for i in range(1, 10)
]


def test_save_jra_race(conn):
    db.save_jra_race(conn, JRA_RACE, JRA_LAPS)
    counts = db.table_counts(conn)
    assert counts["races"] == 1 and counts["race_laps"] == 9
    assert counts["entries"] == counts["results"] == 0  # 馬ごとの結果は入れない
    row = conn.execute("SELECT * FROM races WHERE race_id = '202606040601'").fetchone()
    assert (row["surface"], row["distance_m"], row["class_condition"]) == ("dirt", 1800, "未勝利")
    # netkeiba の結果より先に JRA だけで入った行にも、付記の無いレース名が入る
    assert row["race_name_plain"] == race_names.plain_scrape_name(JRA_RACE["race_name"])


def test_jra_race_is_not_counted_as_fetched(conn):
    """JRA由来の行は「結果まで取得済み」に数えない（netkeibaが後から取りに行けるように）。"""
    db.save_jra_race(conn, JRA_RACE, JRA_LAPS)
    race_id = JRA_RACE["race_id"]
    assert db.race_ids_with_results(conn, [race_id]) == set()
    assert db.saved_race_ids(conn, [race_id]) == {race_id}  # 出馬表側の判定には使う
    assert db.latest_race_date(conn) is None  # updateの再開点も動かさない

    db.save_race_page(conn, _page(race_id=race_id))
    assert db.race_ids_with_results(conn, [race_id]) == {race_id}
    assert db.latest_race_date(conn) is not None


def test_jra_save_does_not_wipe_netkeiba_data(conn):
    """netkeibaの結果が入ったあとにJRAを取り直しても、馬ごとの結果を消さない。"""
    race_id = JRA_RACE["race_id"]
    db.save_race_page(conn, _page(race_id=race_id))
    before = db.table_counts(conn)

    db.save_jra_race(conn, {**JRA_RACE, "race_name": None}, JRA_LAPS)
    after = db.table_counts(conn)
    assert (after["entries"], after["results"], after["payouts"]) == (
        before["entries"], before["results"], before["payouts"]
    )
    row = conn.execute("SELECT race_name, weather FROM races WHERE race_id = ?", (race_id,)).fetchone()
    assert row["race_name"]      # Noneで渡した列は既存の値が残る
    assert row["weather"] == "雨"  # 値がある列は新しいほうで上書きされる
    assert after["race_laps"] == 9


def test_jra_save_without_laps_keeps_existing_laps(conn):
    """障害レースなどラップが無いときに、既にあるラップを消してしまわない。"""
    race_id = JRA_RACE["race_id"]
    db.save_jra_race(conn, JRA_RACE, JRA_LAPS)
    db.save_jra_race(conn, JRA_RACE, [])
    assert db.table_counts(conn)["race_laps"] == 9


def test_save_jra_race_links(conn):
    """JRA公式のレースページへのリンクは races.jra_cname に入り、netkeibaの保存で消えない。"""
    race_id = JRA_RACE["race_id"]
    db.save_jra_race(conn, JRA_RACE, JRA_LAPS)
    saved = db.save_jra_race_links(conn, {
        race_id: "pw01sde0106202604060120260920/40",
        "209999999999": "pw01sde…/00",   # racesに無いレースは黙って飛ばす
    })
    assert saved == 1
    cname = conn.execute("SELECT jra_cname FROM races WHERE race_id = ?", (race_id,)).fetchone()[0]
    assert cname == "pw01sde0106202604060120260920/40"

    db.save_race_page(conn, _page(race_id=race_id))   # あとからnetkeibaの結果を入れても
    assert conn.execute(
        "SELECT jra_cname FROM races WHERE race_id = ?", (race_id,)
    ).fetchone()[0] == cname
    assert db.save_jra_race_links(conn, {}) == 0


def test_races_without_jra_link(conn):
    race_id = JRA_RACE["race_id"]
    db.save_jra_race(conn, JRA_RACE, JRA_LAPS)
    assert db.races_without_jra_link(conn, "2026-09-01", "2026-09-30") == {("2026-09-20", "06")}
    db.save_jra_race_links(conn, {race_id: "pw01sde…/40"})
    assert db.races_without_jra_link(conn, "2026-09-01", "2026-09-30") == set()
    assert db.races_without_jra_link(conn, "2020-01-01", "2020-12-31") == set()


# --- 血統・馬主・生産牧場（JRA公式の出馬表から） -------------------------------


class _Profile:
    """jra_result.HorseProfile と同じ形（テスト用の軽い代用）。"""

    def __init__(self, umaban, horse_name, sire=None, dam=None,
                 broodmare_sire=None, owner=None, breeder=None,
                 horse_weight=None, weight_diff=None, jockey_id=None, kinryo_mark=None):
        self.umaban, self.horse_name = umaban, horse_name
        self.jockey_id, self.kinryo_mark = jockey_id, kinryo_mark
        self.sire, self.dam, self.broodmare_sire = sire, dam, broodmare_sire
        self.owner, self.breeder = owner, breeder
        self.horse_weight, self.weight_diff = horse_weight, weight_diff


def _upcoming_entry(conn, race_id, umaban, horse_id, horse_name):
    conn.execute(
        "INSERT INTO upcoming_races (race_id, race_date, venue_code, entry_status, "
        "fetched_at, updated_at) VALUES (?, '2026-09-20', '06', 'ok', '', '') "
        "ON CONFLICT(race_id) DO NOTHING",
        (race_id,),
    )
    conn.execute(
        "INSERT INTO upcoming_entries (race_id, seq, umaban, horse_id, horse_name) "
        "VALUES (?, ?, ?, ?, ?)",
        (race_id, umaban or 1, umaban, horse_id, horse_name),
    )
    conn.commit()


def test_save_horse_profiles_matches_by_umaban(conn):
    _upcoming_entry(conn, "202606040601", 1, "H1", "イカルステソーロ")
    saved = db.save_horse_profiles(conn, "202606040601", [
        _Profile(1, "イカルステソーロ", sire="フィエールマン", dam="ライトファンタスティック",
                 broodmare_sire="Acclamation", owner="了德寺(株)", breeder="リョーケンファーム"),
        _Profile(2, "いない馬", sire="ディープ"),     # 出馬表に無い馬は飛ばす
    ])
    assert saved == 1
    row = conn.execute("SELECT * FROM horses WHERE horse_id = 'H1'").fetchone()
    assert (row["sire"], row["broodmare_sire"]) == ("フィエールマン", "Acclamation")
    assert (row["owner_name"], row["breeder"]) == ("了德寺(株)", "リョーケンファーム")


def test_save_horse_profiles_matches_by_name_before_the_draw(conn):
    """枠順確定前は馬番が無いので、馬名で突き合わせる。"""
    _upcoming_entry(conn, "202606040602", None, "H2", "テストウマ")
    assert db.save_horse_profiles(conn, "202606040602", [
        _Profile(None, "テストウマ", sire="キズナ"),
    ]) == 1
    assert conn.execute("SELECT sire FROM horses WHERE horse_id = 'H2'").fetchone()[0] == "キズナ"


def test_save_horse_profiles_keeps_existing_values(conn):
    _upcoming_entry(conn, "202606040603", 3, "H3", "テストウマ")
    db.save_horse_profiles(conn, "202606040603", [_Profile(3, "テストウマ", sire="キズナ", owner="A")])
    db.save_horse_profiles(conn, "202606040603", [_Profile(3, "テストウマ", owner="B")])
    row = conn.execute("SELECT sire, owner_name FROM horses WHERE horse_id = 'H3'").fetchone()
    assert (row["sire"], row["owner_name"]) == ("キズナ", "B")   # 空の列は既存値を残す


# --- 馬体重（JRA公式の出馬表から） ---------------------------------------------


def test_save_upcoming_weights_matches_by_umaban(conn):
    _upcoming_entry(conn, "202606040601", 1, "H1", "イカルステソーロ")
    _upcoming_entry(conn, "202606040601", 2, "H2", "テストウマ")
    saved = db.save_upcoming_weights(conn, "202606040601", [
        _Profile(1, "イカルステソーロ", horse_weight=480, weight_diff=-2),
        _Profile(2, "テストウマ", horse_weight=456),            # 初出走は増減なし
        _Profile(3, "いない馬", horse_weight=500, weight_diff=4),
    ])
    assert saved == 2
    rows = {r["umaban"]: r for r in conn.execute(
        "SELECT umaban, horse_weight, weight_diff FROM upcoming_entries"
    )}
    assert (rows[1]["horse_weight"], rows[1]["weight_diff"]) == (480, -2)
    assert (rows[2]["horse_weight"], rows[2]["weight_diff"]) == (456, None)


def test_save_upcoming_weights_matches_by_name_before_the_draw(conn):
    _upcoming_entry(conn, "202606040602", None, "H2", "テストウマ")
    assert db.save_upcoming_weights(conn, "202606040602", [
        _Profile(None, "テストウマ", horse_weight=470, weight_diff=0),
    ]) == 1
    row = conn.execute("SELECT horse_weight, weight_diff FROM upcoming_entries").fetchone()
    assert (row["horse_weight"], row["weight_diff"]) == (470, 0)


def test_save_upcoming_weights_leaves_unpublished_weights_alone(conn):
    """当日発表前は馬体重が無いので届かない。前に入れた値を消さないこと。"""
    _upcoming_entry(conn, "202606040603", 1, "H1", "テストウマ")
    db.save_upcoming_weights(conn, "202606040603", [_Profile(1, "テストウマ", horse_weight=480, weight_diff=2)])
    assert db.save_upcoming_weights(conn, "202606040603", [_Profile(1, "テストウマ")]) == 0
    row = conn.execute("SELECT horse_weight, weight_diff FROM upcoming_entries").fetchone()
    assert (row["horse_weight"], row["weight_diff"]) == (480, 2)


def test_resaving_the_netkeiba_shutuba_keeps_the_weight(conn):
    """netkeibaの出馬表は出走馬を入れ直すが、JRAから取った馬体重は残す。

    馬体重はnetkeibaの出馬表では埋まらないので、引き継がないと
    「最新に更新」を押すたびに消えてしまう。
    """
    db.save_upcoming_shutuba(conn, _shutuba())
    umaban = conn.execute("SELECT umaban FROM upcoming_entries ORDER BY seq").fetchone()[0]
    horse_name = conn.execute(
        "SELECT horse_name FROM upcoming_entries WHERE umaban = ?", (umaban,)
    ).fetchone()[0]
    db.save_upcoming_weights(conn, "202606040611", [
        _Profile(umaban, horse_name, horse_weight=508, weight_diff=2),
    ])

    db.save_upcoming_shutuba(conn, _shutuba())
    row = conn.execute(
        "SELECT horse_weight, weight_diff FROM upcoming_entries WHERE umaban = ?", (umaban,)
    ).fetchone()
    assert (row["horse_weight"], row["weight_diff"]) == (508, 2)
    # 馬体重が無い馬はNULLのまま（勝手に埋まらない）
    assert conn.execute(
        "SELECT COUNT(*) FROM upcoming_entries WHERE horse_weight IS NULL"
    ).fetchone()[0] == 12


# --- 減量騎手の印（JRA公式の出馬表から） -------------------------------------------


def test_save_upcoming_kinryo_marks_only_for_the_same_jockey(conn):
    """印は騎手に付く。netkeibaとJRAで騎手が食い違う行（取り直しの途中）には書かない。"""
    db.save_upcoming_shutuba(conn, _shutuba())
    rows = conn.execute("SELECT umaban, horse_name, jockey_id FROM upcoming_entries ORDER BY seq").fetchall()
    first, second = rows[0], rows[1]
    saved = db.save_upcoming_kinryo_marks(conn, "202606040611", [
        _Profile(first["umaban"], first["horse_name"], jockey_id=first["jockey_id"], kinryo_mark="▲"),
        _Profile(second["umaban"], second["horse_name"], jockey_id="99999", kinryo_mark="☆"),
    ])
    assert saved == 1
    marks = dict(conn.execute("SELECT umaban, kinryo_mark FROM upcoming_entries"))
    assert marks[first["umaban"]] == "▲" and marks[second["umaban"]] is None

    # netkeibaの出馬表を取り直しても、同じ騎手なら印は残る
    db.save_upcoming_shutuba(conn, _shutuba())
    marks = dict(conn.execute("SELECT umaban, kinryo_mark FROM upcoming_entries"))
    assert marks[first["umaban"]] == "▲"


def test_saving_the_result_copies_the_mark_of_the_same_jockey(conn):
    """netkeibaの結果ページには印が無いので、出馬表（upcoming_entries）の印を写す。"""
    page = _page()
    by_umaban = {e["umaban"]: e for e in page.entries}
    conn.execute("INSERT INTO upcoming_races (race_id, race_date, venue_code, entry_status, fetched_at, "
                 "updated_at) VALUES ('202606040411', '2026-09-14', '06', 'entries', '', '')")
    for seq, (umaban, jockey_id, mark) in enumerate([
        (1, by_umaban[1]["jockey_id"], "▲"),
        (2, "99999", "☆"),                       # 出馬表の後で乗り替わった → 写さない
    ], 1):
        conn.execute("INSERT INTO upcoming_entries (race_id, seq, umaban, horse_id, jockey_id, kinryo_mark) "
                     "VALUES ('202606040411', ?, ?, ?, ?, ?)",
                     (seq, umaban, by_umaban[umaban]["horse_id"], jockey_id, mark))
    conn.commit()

    db.save_race_page(conn, page)
    marks = dict(conn.execute("SELECT umaban, kinryo_mark FROM entries"))
    assert marks[1] == "▲"
    assert marks[2] is None
    assert sum(m is not None for m in marks.values()) == 1


# --- 減量騎手の印（JRA公式の出馬表から） -------------------------------------------


def test_save_upcoming_kinryo_marks_only_for_the_same_jockey(conn):
    """印は騎手に付く。netkeibaとJRAで騎手が食い違う行（取り直しの途中）には書かない。"""
    db.save_upcoming_shutuba(conn, _shutuba())
    rows = conn.execute("SELECT umaban, horse_name, jockey_id FROM upcoming_entries ORDER BY seq").fetchall()
    first, second = rows[0], rows[1]
    saved = db.save_upcoming_kinryo_marks(conn, "202606040611", [
        _Profile(first["umaban"], first["horse_name"], jockey_id=first["jockey_id"], kinryo_mark="▲"),
        _Profile(second["umaban"], second["horse_name"], jockey_id="99999", kinryo_mark="☆"),
    ])
    assert saved == 1
    marks = dict(conn.execute("SELECT umaban, kinryo_mark FROM upcoming_entries"))
    assert marks[first["umaban"]] == "▲" and marks[second["umaban"]] is None

    # netkeibaの出馬表を取り直しても、同じ騎手なら印は残る
    db.save_upcoming_shutuba(conn, _shutuba())
    marks = dict(conn.execute("SELECT umaban, kinryo_mark FROM upcoming_entries"))
    assert marks[first["umaban"]] == "▲"


def test_saving_the_result_copies_the_mark_of_the_same_jockey(conn):
    """netkeibaの結果ページには印が無いので、出馬表（upcoming_entries）の印を写す。"""
    page = _page()
    by_umaban = {e["umaban"]: e for e in page.entries}
    conn.execute("INSERT INTO upcoming_races (race_id, race_date, venue_code, entry_status, fetched_at, "
                 "updated_at) VALUES ('202606040411', '2026-09-14', '06', 'entries', '', '')")
    for seq, (umaban, jockey_id, mark) in enumerate([
        (1, by_umaban[1]["jockey_id"], "▲"),
        (2, "99999", "☆"),                       # 出馬表の後で乗り替わった → 写さない
    ], 1):
        conn.execute("INSERT INTO upcoming_entries (race_id, seq, umaban, horse_id, jockey_id, kinryo_mark) "
                     "VALUES ('202606040411', ?, ?, ?, ?, ?)",
                     (seq, umaban, by_umaban[umaban]["horse_id"], jockey_id, mark))
    conn.commit()

    db.save_race_page(conn, page)
    marks = dict(conn.execute("SELECT umaban, kinryo_mark FROM entries"))
    assert marks[1] == "▲"
    assert marks[2] is None
    assert sum(m is not None for m in marks.values()) == 1


# --- 予想ボード ---------------------------------------------------------------------


def _marker(horse_id, **kwargs):
    row = {"horse_id": horse_id, "tier": "B", "position": 0.5, "lane_offset": 0.5,
           "comment": "", "is_manual": False, "is_excluded": False}
    row.update(kwargs)
    return row


def test_save_board_only_keeps_what_was_touched(conn):
    """自動仮配置は開くたびに作り直すので、DBに残すのは手を入れた結果だけ。"""
    saved = db.save_board(conn, "R1", [
        _marker("h1", tier="A", position=0.8, is_manual=True),
        _marker("h2", comment="映像で高評価"),      # 動かしていないがコメントがある
        _marker("h3"),                              # 手つかず → 保存しない
    ])
    assert saved == 2
    rows = {r["horse_id"]: r for r in conn.execute("SELECT * FROM board_horses")}
    assert set(rows) == {"h1", "h2"}
    assert (rows["h1"]["tier"], rows["h1"]["position"], rows["h1"]["is_manual"]) == ("A", 0.8, 1)
    assert rows["h2"]["comment"] == "映像で高評価" and rows["h2"]["is_manual"] == 0


def test_save_board_overwrites_the_previous_placement(conn):
    db.save_board(conn, "R1", [_marker("h1", tier="A", is_manual=True)])
    db.save_board(conn, "R1", [_marker("h1", tier="C", position=0.2, is_manual=True)])
    row = conn.execute("SELECT tier, position FROM board_horses WHERE horse_id = 'h1'").fetchone()
    assert (row["tier"], row["position"]) == ("C", 0.2)
    assert conn.execute("SELECT COUNT(*) FROM board_horses").fetchone()[0] == 1


def test_putting_a_horse_back_removes_its_row(conn):
    """手を入れたあとで元に戻した馬は、行ごと消して自動配置に返す。"""
    db.save_board(conn, "R1", [_marker("h1", tier="A", is_manual=True)])
    db.save_board(conn, "R1", [_marker("h1")])           # is_manual なし・コメントなし
    assert conn.execute("SELECT COUNT(*) FROM board_horses").fetchone()[0] == 0


def test_boards_do_not_mix_between_races(conn):
    db.save_board(conn, "R1", [_marker("h1", tier="A", is_manual=True)])
    db.save_board(conn, "R2", [_marker("h1", tier="D", is_manual=True)])
    rows = {r["race_id"]: r["tier"] for r in conn.execute("SELECT race_id, tier FROM board_horses")}
    assert rows == {"R1": "A", "R2": "D"}

    assert db.clear_board(conn, "R1") == 1
    assert [r["race_id"] for r in conn.execute("SELECT race_id FROM board_horses")] == ["R2"]


def test_save_board_ignores_rows_without_a_horse(conn):
    assert db.save_board(conn, "R1", [{"tier": "A", "is_manual": True}]) == 0


def test_blank_comments_are_not_stored(conn):
    db.save_board(conn, "R1", [_marker("h1", comment="   ", is_manual=True)])
    assert conn.execute("SELECT comment FROM board_horses").fetchone()[0] is None


# --- 予想印 ---------------------------------------------------------------------


def test_marks_are_saved_and_overwritten(conn):
    db.save_mark(conn, "R1", "h1", "◎")
    db.save_mark(conn, "R1", "h2", "▲")
    db.save_mark(conn, "R1", "h1", "○")
    assert db.get_marks(conn, "R1") == {"h1": "○", "h2": "▲"}


def test_removing_a_mark_deletes_its_row(conn):
    db.save_mark(conn, "R1", "h1", "◎")
    db.save_mark(conn, "R1", "h1", None)
    db.save_mark(conn, "R1", "h2", "--")          # 外す印は保存しない
    db.save_mark(conn, "R1", "h3", "?")           # 知らない印も保存しない
    assert db.get_marks(conn, "R1") == {}
    assert conn.execute("SELECT COUNT(*) FROM horse_marks").fetchone()[0] == 0


def test_the_erase_mark_is_kept(conn):
    """「消」は印を外すのではなく、買わない馬の印として残す。"""
    db.save_mark(conn, "R1", "h1", "消")
    assert db.get_marks(conn, "R1") == {"h1": "消"}


def test_marks_do_not_mix_between_races(conn):
    db.save_mark(conn, "R1", "h1", "◎")
    db.save_mark(conn, "R2", "h1", "✓")
    assert db.get_marks(conn, "R1") == {"h1": "◎"}
    assert db.get_marks(conn, "R2") == {"h1": "✓"}


def test_winner_corner_survives_a_netkeiba_import(conn):
    """勝ち馬の通過順位（JRA由来）は、netkeibaの結果を入れ直しても消えない。

    jra_cname と同じく RACE_COLUMNS の外の列にしてあるため。
    """
    page = _page()
    db.save_jra_race(conn, {**page.race, "winner_corner": "1-1-1-1"}, page.laps)
    assert conn.execute(
        "SELECT winner_corner FROM races WHERE race_id = ?", (page.race["race_id"],)
    ).fetchone()[0] == "1-1-1-1"

    db.save_race_page(conn, page)          # netkeibaの結果を入れ直す
    assert conn.execute(
        "SELECT winner_corner FROM races WHERE race_id = ?", (page.race["race_id"],)
    ).fetchone()[0] == "1-1-1-1"


def test_the_jra_winner_survives_a_netkeiba_import(conn):
    """勝ち馬の馬番・枠・馬名・上り3F（JRA由来）も、netkeibaの結果を入れ直しても消えない。"""
    page = _page()
    winner = {"winner_umaban": 8, "winner_waku": 5, "winner_name": "ウィンターブリーズ", "winner_last_3f": 37.5}
    db.save_jra_race(conn, {**page.race, **winner}, page.laps)
    db.save_race_page(conn, page)
    row = conn.execute(
        "SELECT winner_umaban, winner_waku, winner_name, winner_last_3f FROM races WHERE race_id = ?",
        (page.race["race_id"],),
    ).fetchone()
    assert tuple(row) == (8, 5, "ウィンターブリーズ", 37.5)


def test_save_jra_race_without_a_winner_corner(conn):
    """通過順位が読めなくても、レースとラップの取り込みは止めない。"""
    page = _page()
    db.save_jra_race(conn, page.race, page.laps)
    row = conn.execute(
        "SELECT winner_corner FROM races WHERE race_id = ?", (page.race["race_id"],)
    ).fetchone()
    assert row[0] is None


def test_a_horse_that_is_only_excluded_is_saved(conn):
    """消しただけの馬（動かしてもいない・コメントも無い）も残すこと。

    以前は「動かした or コメントがある」だけを残していたので、消した馬が保存されなかった。
    """
    assert db.save_board(conn, "R1", [_marker("h1", is_excluded=True)]) == 1
    row = conn.execute("SELECT is_excluded, is_manual FROM board_horses").fetchone()
    assert (row["is_excluded"], row["is_manual"]) == (1, 0)


def test_un_excluding_a_horse_removes_its_row(conn):
    db.save_board(conn, "R1", [_marker("h1", is_excluded=True)])
    db.save_board(conn, "R1", [_marker("h1")])          # 消しを戻した
    assert conn.execute("SELECT COUNT(*) FROM board_horses").fetchone()[0] == 0


def test_excluded_is_kept_alongside_the_placement(conn):
    """動かしたうえで消した馬は、位置も消しの印も両方残る。"""
    db.save_board(conn, "R1", [
        _marker("h1", tier="A", position=0.8, is_manual=True, is_excluded=True),
    ])
    row = conn.execute("SELECT tier, position, is_excluded FROM board_horses").fetchone()
    assert (row["tier"], row["position"], row["is_excluded"]) == ("A", 0.8, 1)


def test_only_a_moved_horse_remembers_its_place(conn):
    """位置を覚えるのは**手で動かした馬だけ**。

    消しただけ・コメントだけの馬にも位置を残すと、次に開いたときに
    「保存済みの引き伸ばし後の値」と「他の馬の生の値」が混ざり、
    そこからもう一度引き伸ばされて全馬の位置がずれる。
    """
    db.save_board(conn, "R1", [
        _marker("h1", tier="A", position=0.9, lane_offset=0.3, is_excluded=True),
        _marker("h2", tier="A", position=0.8, lane_offset=0.3, comment="メモ"),
        _marker("h3", tier="A", position=0.7, lane_offset=0.3, is_manual=True),
    ])
    rows = {r["horse_id"]: r for r in conn.execute("SELECT * FROM board_horses")}

    for horse in ("h1", "h2"):                      # 消しただけ／コメントだけ
        assert rows[horse]["position"] is None, horse
        assert rows[horse]["lane_offset"] is None, horse
        assert rows[horse]["tier"] is None, horse
    assert rows["h1"]["is_excluded"] == 1           # 消しの印は残る
    assert rows["h2"]["comment"] == "メモ"          # コメントも残る

    # 手で動かした馬は今までどおり位置を覚える
    assert (rows["h3"]["tier"], rows["h3"]["position"], rows["h3"]["lane_offset"]) == ("A", 0.7, 0.3)


# --- オッズ（JRA公式） ---------------------------------------------------------


def _odds_page(bet_type="umaren", rows=None, label="12時31分現在オッズ"):
    from keiba_data.scrapers.jra_odds import OddsPage

    return OddsPage(
        race_id="R1", bet_type=bet_type, odds_label=label,
        rows=rows if rows is not None else [
            {"combo": "1-2", "odds_low": 41.3, "odds_high": None},
            {"combo": "1-3", "odds_low": 12.8, "odds_high": None},
        ],
    )


def test_odds_are_saved_with_the_time_they_are_from(conn):
    assert db.save_odds(conn, _odds_page()) == 2
    rows = {r["combo"]: r for r in conn.execute("SELECT * FROM odds")}
    assert rows["1-2"]["odds_low"] == 41.3
    update = conn.execute("SELECT * FROM odds_updates").fetchone()
    assert (update["bet_type"], update["odds_label"], update["n_combos"]) == (
        "umaren", "12時31分現在オッズ", 2,
    )


def test_a_range_keeps_both_ends(conn):
    """複勝とワイドは「12.8 - 15.1」の幅なので、上限も残す。"""
    db.save_odds(conn, _odds_page("wide", [{"combo": "1-2", "odds_low": 12.8, "odds_high": 15.1}]))
    row = conn.execute("SELECT odds_low, odds_high FROM odds").fetchone()
    assert (row["odds_low"], row["odds_high"]) == (12.8, 15.1)


def test_taking_the_odds_again_replaces_them_rather_than_piling_up(conn):
    """オッズは**最新だけ**を残す（履歴は持たない）。"""
    db.save_odds(conn, _odds_page())
    db.save_odds(conn, _odds_page(rows=[{"combo": "1-2", "odds_low": 39.9, "odds_high": None}],
                                  label="12時45分現在オッズ"))
    rows = conn.execute("SELECT combo, odds_low FROM odds").fetchall()
    assert [(r["combo"], r["odds_low"]) for r in rows] == [("1-2", 39.9)]   # 1-3 は残らない
    assert conn.execute("SELECT odds_label FROM odds_updates").fetchone()[0] == "12時45分現在オッズ"


def test_each_bet_type_is_kept_separately(conn):
    db.save_odds(conn, _odds_page("umaren"))
    db.save_odds(conn, _odds_page("umatan"))
    assert conn.execute("SELECT COUNT(*) FROM odds").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM odds_updates").fetchone()[0] == 2


def test_a_combination_nobody_bet_on_is_kept_with_an_empty_odds(conn):
    db.save_odds(conn, _odds_page(rows=[{"combo": "1-2", "odds_low": None, "odds_high": None}]))
    assert conn.execute("SELECT odds_low FROM odds").fetchone()[0] is None


def test_the_odds_tokens_are_kept_per_bet_type(conn):
    """トークンの末尾はチェックサムで組み立てられないので、拾ったものをためる。"""
    assert db.save_jra_odds_links(conn, {"R1": {"umaren": "pw154a/07", "wide": "pw155a/8B"}}) == 2
    assert db.get_jra_odds_links(conn, "R1") == {"umaren": "pw154a/07", "wide": "pw155a/8B"}
    assert db.get_jra_odds_links(conn, "R2") == {}


def test_a_newer_token_overwrites_the_old_one(conn):
    db.save_jra_odds_links(conn, {"R1": {"umaren": "old/07"}})
    db.save_jra_odds_links(conn, {"R1": {"umaren": "new/08"}})
    assert db.get_jra_odds_links(conn, "R1") == {"umaren": "new/08"}


def test_no_tokens_is_not_an_error(conn):
    assert db.save_jra_odds_links(conn, {}) == 0


def test_the_entry_page_tokens_are_kept_per_race(conn):
    """出馬表（馬体重の取得元）のトークンも、オッズと同じ理由でためる。"""
    assert db.save_jra_entry_links(conn, {"R1": "pw01dde01a/84", "R2": "pw01dde01b/85"}) == 2
    assert db.get_jra_entry_link(conn, "R1") == "pw01dde01a/84"
    assert db.get_jra_entry_link(conn, "R3") is None
    db.save_jra_entry_links(conn, {"R1": "pw01dde01new/86"})
    assert db.get_jra_entry_link(conn, "R1") == "pw01dde01new/86"
    assert db.save_jra_entry_links(conn, {}) == 0


# --- 買い目 -----------------------------------------------------------------------------


def _slip(bet="umaren", combos=("1-2",), amount=100):
    return {"bet": bet, "combos": list(combos), "amount": amount}


def test_the_bet_slip_goes_there_and_comes_back(conn):
    assert db.save_bet_slips(conn, "R1", [
        _slip("tansho", ["7"], 500), _slip("umaren", ["1-2", "1-3"], 100),
    ]) == 2
    assert db.get_bet_slips(conn, "R1") == [
        {"group": 1, "bet": "tansho", "combos": ["7"], "amount": 500, "kind": None},
        {"group": 2, "bet": "umaren", "combos": ["1-2", "1-3"], "amount": 100, "kind": None},
    ]


def test_saving_again_replaces_the_whole_slip(conn):
    """画面から届くのは「いま積まれているすべて」なので、消した買い目を残さない。"""
    db.save_bet_slips(conn, "R1", [_slip(), _slip("wide", ["2-3"])])
    db.save_bet_slips(conn, "R1", [_slip("wide", ["2-3"])])
    assert [g["bet"] for g in db.get_bet_slips(conn, "R1")] == ["wide"]


def test_the_same_group_number_twice_does_not_break_the_save(conn):
    """番号は画面の中の通し番号なので、重なって届いても受け付ける（振り直す）。"""
    db.save_bet_slips(conn, "R1", [
        {"group": 9, **_slip("tansho", ["7"])}, {"group": 9, **_slip("umaren", ["1-2"])},
    ])
    assert [g["group"] for g in db.get_bet_slips(conn, "R1")] == [1, 2]


def test_bet_slips_do_not_mix_between_races(conn):
    db.save_bet_slips(conn, "R1", [_slip("tansho", ["7"])])
    db.save_bet_slips(conn, "R2", [_slip("wide", ["2-3"])])
    assert [g["bet"] for g in db.get_bet_slips(conn, "R1")] == ["tansho"]
    assert db.clear_bet_slips(conn, "R1") == 1
    assert db.get_bet_slips(conn, "R1") == []
    assert [g["bet"] for g in db.get_bet_slips(conn, "R2")] == ["wide"]


def test_a_bet_slip_that_makes_no_sense_is_dropped(conn):
    """ブラウザから届く値なので、知らない券種や組み合わせの形が違うものは捨てる。"""
    assert db.save_bet_slips(conn, "R1", [
        _slip("nonsense", ["1-2"]),                  # 知らない券種
        _slip("umaren", ["1;DROP TABLE odds"]),      # 組み合わせの形が違う
        _slip("umaren", []),                         # 空
    ]) == 0
    assert db.get_bet_slips(conn, "R1") == []


def test_the_amount_is_kept_as_a_whole_number_of_yen(conn):
    db.save_bet_slips(conn, "R1", [_slip(amount=-100), _slip("wide", ["2-3"], None)])
    assert [g["amount"] for g in db.get_bet_slips(conn, "R1")] == [0, 0]


def test_the_same_combination_twice_is_kept_once(conn):
    db.save_bet_slips(conn, "R1", [_slip("umaren", ["1-2", "1-2", "1-3"])])
    assert db.get_bet_slips(conn, "R1")[0]["combos"] == ["1-2", "1-3"]


def test_when_the_slip_was_saved_is_remembered(conn):
    """画面が「自分が出したばかりの保存」と見分けるのに使う。"""
    assert db.bet_slips_updated_at(conn, "R1") is None
    db.save_bet_slips(conn, "R1", [_slip()])
    assert db.bet_slips_updated_at(conn, "R1")


def test_the_way_of_buying_is_kept_with_the_slip(conn):
    """ながし・ボックスは点数だけでは分からないので、買い方も残す。"""
    db.save_bet_slips(conn, "R1", [
        {**_slip("umaren", ["1-2", "1-3"]), "kind": "ながし"},
        {**_slip("wide", ["2-3"]), "kind": "ボックス"},
        _slip("tansho", ["7"]),                       # 買い方が無くても保存できる
    ])
    assert [g["kind"] for g in db.get_bet_slips(conn, "R1")] == ["ながし", "ボックス", None]


# --- 予想ボード・買い目の整形（SQLiteと共有ストアで共通） --------------------------


def test_board_record_keeps_only_touched_horses():
    assert db.board_record({"horse_id": "h1"}) is None                       # 手を入れていない
    assert db.board_record({"horse_id": None, "is_manual": True}) is None    # 馬IDが無い
    moved = db.board_record({"horse_id": "h1", "tier": "A", "position": 0.7,
                             "lane_offset": 1, "is_manual": True})
    assert moved == {"horse_id": "h1", "tier": "A", "position": 0.7, "lane_offset": 1,
                     "comment": None, "is_manual": 1, "is_excluded": 0}


def test_board_record_drops_the_position_unless_moved_by_hand():
    """消しただけ・コメントだけの馬は位置を残さない（残すと全馬の位置がずれる）。"""
    crossed = db.board_record({"horse_id": "h2", "tier": "B", "position": 0.3, "is_excluded": True})
    assert crossed["tier"] is None and crossed["position"] is None and crossed["is_excluded"] == 1
    noted = db.board_record({"horse_id": "h3", "tier": "C", "comment": "  掛かる  "})
    assert noted["comment"] == "掛かる" and noted["tier"] is None and noted["is_manual"] == 0


def test_normalize_slip_groups_checks_the_shape():
    groups = db.normalize_slip_groups([
        {"bet": "umaren", "combos": ["1-2", "1-2", "bad", "3-4"], "amount": 200, "kind": " ながし "},
        {"bet": "unknown", "combos": ["1"]},                  # 知らない券種
        {"bet": "tansho", "combos": []},                      # 組み合わせが空
        {"bet": "tansho", "combos": [7], "amount": -100},     # 負の金額は0
    ])
    assert groups == [
        {"bet": "umaren", "combos": ["1-2", "3-4"], "amount": 200, "kind": "ながし"},
        {"bet": "tansho", "combos": ["7"], "amount": 0, "kind": None},
    ]
    assert db.normalize_slip_groups(None) == []
