import re
from datetime import date

import pytest

from keiba_data import config, db
from keiba_data.html_store import HtmlStore
from keiba_data.http import RequestBudgetExceeded
from keiba_data.updater import Updater
from tests.conftest import load_fixture

DAY = date(2026, 9, 13)
TODAY = date(2026, 9, 17)
RESULT_FIXTURE = load_fixture("result_202606040411_g2.html")
RACE_LIST = load_fixture("race_list_20260913.html")
CALENDAR = load_fixture("calendar_202609.html")


class FakeClient:
    """URLに応じてフィクスチャを返す偽のHTTPクライアント。

    結果ページはどのrace_idでもセントライト記念のHTMLのrace_id部分を差し替えて返す。
    """

    def __init__(self, max_requests=None, missing_race_ids=()):
        self.n_requests = 0
        self.urls: list[str] = []
        self.max_requests = max_requests
        self.missing_race_ids = set(missing_race_ids)

    def get_text(self, url, *, encoding):
        if self.max_requests is not None and self.n_requests >= self.max_requests:
            raise RequestBudgetExceeded("上限")
        self.n_requests += 1
        self.urls.append(url)
        if "calendar.html" in url:
            return CALENDAR
        if "race_list_sub.html" in url:
            return RACE_LIST if "20260913" in url else "<html></html>"
        race_id = url.rstrip("/").rsplit("/", 1)[-1]
        if race_id in self.missing_race_ids:
            return "<html><body>no data</body></html>"
        return _result_html_for(race_id)


def _result_html_for(race_id: str) -> str:
    """フィクスチャの開催回・日目・race_idを差し替える（場・日付・R番号はフィクスチャのまま）。"""
    return RESULT_FIXTURE.replace("202606040411", race_id)


@pytest.fixture
def store(tmp_path):
    return HtmlStore(tmp_path / "html")


def _only_day_13(conn):
    """テストを1開催日に絞る: 他の日を完了済みにしておく。"""
    for d in (5, 6, 12, 19, 20, 21, 27):
        db.update_kaisai_day(conn, date(2026, 9, d), 0, 0, completed=True)


def test_backfill_saves_races_and_is_idempotent(conn, store):
    client = FakeClient()
    updater = Updater(conn, client, store, today=TODAY)
    updater.sync_calendar(DAY, DAY)
    _only_day_13(conn)
    n_before = client.n_requests
    updater.run_backfill(DAY, DAY)

    assert updater.n_races_saved == 24
    assert client.n_requests - n_before == 1 + 1 + 24  # カレンダー + レース一覧 + 結果24件
    counts = db.table_counts(conn)
    assert counts["races"] == 24
    assert counts["entries"] == 24 * 16
    day_row = conn.execute("SELECT * FROM kaisai_days WHERE kaisai_date = '2026-09-13'").fetchone()
    assert (day_row["n_races"], day_row["n_saved"], day_row["completed"]) == (24, 24, 1)
    assert len(list(store.keys("race_result"))) == 24

    # 2回目: 月は終わっていないのでカレンダーは再取得するが、完了済みの日・レースは取りに行かない
    client2 = FakeClient()
    updater2 = Updater(conn, client2, store, today=TODAY)
    updater2.run_backfill(DAY, DAY)
    assert updater2.n_races_saved == 0
    assert [u for u in client2.urls if "db.netkeiba.com" in u] == []
    assert db.table_counts(conn) == counts


def test_resume_after_request_budget(conn, store):
    updater = Updater(conn, FakeClient(), store, today=TODAY)
    updater.sync_calendar(DAY, DAY)
    _only_day_13(conn)

    client = FakeClient(max_requests=6)  # カレンダー1 + 一覧1 + 結果4件の後、5件目で止まる
    with pytest.raises(RequestBudgetExceeded):
        Updater(conn, client, store, today=TODAY).run_backfill(DAY, DAY)
    assert db.table_counts(conn)["races"] == 4  # カレンダー1 + 一覧1 + 結果4件
    assert conn.execute("SELECT completed FROM kaisai_days WHERE kaisai_date='2026-09-13'").fetchone()[0] == 0

    client = FakeClient()
    Updater(conn, client, store, today=TODAY).run_backfill(DAY, DAY)
    assert db.table_counts(conn)["races"] == 24
    result_urls = [u for u in client.urls if "db.netkeiba.com" in u]
    assert len(result_urls) == 20  # 保存済みの4件は取りに行かない


def test_saved_html_is_reused_without_network(conn, store, tmp_path):
    updater = Updater(conn, FakeClient(), store, today=TODAY)
    updater.sync_calendar(DAY, DAY)
    _only_day_13(conn)
    updater.run_backfill(DAY, DAY)

    fresh = db.connect(tmp_path / "fresh.db")
    client = FakeClient()
    Updater(fresh, client, store, today=TODAY).run_reparse()
    assert client.n_requests == 0
    assert db.table_counts(fresh)["races"] == 24


def test_unavailable_result_on_race_day_is_not_completed(conn, store):
    client = FakeClient(missing_race_ids={"202609040412"})
    updater = Updater(conn, client, store, today=DAY)  # 当日（最終レースが未確定）
    updater.sync_calendar(DAY, DAY)
    _only_day_13(conn)
    updater.run_update()

    assert db.table_counts(conn)["races"] == 23
    assert updater.n_warnings == 0  # 当日は結果未掲載でも警告にしない
    assert conn.execute("SELECT completed FROM kaisai_days WHERE kaisai_date='2026-09-13'").fetchone()[0] == 0

    # 翌週のupdateで残りの1レースだけを取りに行く
    client = FakeClient()
    Updater(conn, client, store, today=TODAY).run_update()
    assert db.table_counts(conn)["races"] == 24
    assert [u for u in client.urls if "db.netkeiba.com" in u] == [config.RACE_RESULT_URL.format(race_id="202609040412")]


def test_structure_change_is_warned_and_logged(conn, store):
    class BrokenClient(FakeClient):
        def get_text(self, url, *, encoding):
            html = super().get_text(url, encoding=encoding)
            return html.replace(">調教師<", ">trainer<") if "db.netkeiba.com" in url else html

    updater = Updater(conn, BrokenClient(), store, today=TODAY)
    updater.sync_calendar(DAY, DAY)
    _only_day_13(conn)
    updater.run_backfill(DAY, DAY)

    assert db.table_counts(conn)["races"] == 0
    assert updater.n_warnings == 24
    assert conn.execute("SELECT COUNT(*) FROM parse_warnings").fetchone()[0] == 24
    assert list(store.keys("race_result")) == []  # パースできなかったHTMLは保存しない


# --- 未開催レースの出馬表 -----------------------------------------------------

UPCOMING_DAY = date(2026, 9, 20)
RACE_LIST_20 = load_fixture("race_list_20260920.html")
SHUTUBA = load_fixture("shutuba_202606040611.html")


class FakeShutubaClient:
    """レース一覧と出馬表を返す偽クライアント。出馬表は騎手名を差し替えられる。"""

    def __init__(self, jockey_name="横山 典弘", missing_race_ids=()):
        self.n_requests = 0
        self.urls: list[str] = []
        self.jockey_name = jockey_name
        self.missing_race_ids = set(missing_race_ids)

    def get_text(self, url, *, encoding):
        self.n_requests += 1
        self.urls.append(url)
        if "race_list_sub.html" in url:
            return RACE_LIST_20 if "20260920" in url else "<html></html>"
        if "shutuba.html" in url:
            race_id = url.rsplit("race_id=", 1)[1]
            if race_id in self.missing_race_ids:
                return "<html><body>準備中</body></html>"
            return SHUTUBA.replace("202606040611", race_id).replace("横山 典弘", self.jockey_name)
        raise AssertionError(f"想定外のURL: {url}")


def test_fetch_upcoming_day_saves_list_and_entries(conn, store):
    client = FakeShutubaClient()
    updater = Updater(conn, client, store, today=date(2026, 9, 19))
    n = updater.fetch_upcoming_day(UPCOMING_DAY)

    assert n == 24
    counts = db.table_counts(conn)
    assert counts["upcoming_races"] == 24
    assert counts["upcoming_entries"] == 24 * 13
    assert counts["races"] == 0  # 確定データ側は触らない
    assert client.n_requests == 1 + 24
    assert updater.n_warnings == 0
    assert list(store.keys("race_result")) == []  # 出馬表HTMLは保存しない


def test_refetch_upcoming_race_overwrites(conn, store):
    updater = Updater(conn, FakeShutubaClient(), store, today=date(2026, 9, 19))
    updater.fetch_upcoming_day(UPCOMING_DAY)
    before = db.table_counts(conn)

    # 騎手が乗り替わった状態で1レースだけ取り直す
    client = FakeShutubaClient(jockey_name="C.ルメール")
    Updater(conn, client, store, today=date(2026, 9, 19)).fetch_upcoming_race("202606040611", UPCOMING_DAY)

    assert db.table_counts(conn) == before  # 行は増えない
    assert client.n_requests == 1
    row = conn.execute(
        "SELECT jockey_name FROM upcoming_entries WHERE race_id='202606040611' AND umaban=1"
    ).fetchone()
    assert row["jockey_name"] == "C.ルメール"


def test_unpublished_shutuba_is_skipped_without_warning(conn, store):
    client = FakeShutubaClient(missing_race_ids={"202606040601"})
    updater = Updater(conn, client, store, today=date(2026, 9, 19))
    n = updater.fetch_upcoming_day(UPCOMING_DAY)

    assert n == 23
    assert updater.n_warnings == 0  # 未公開は構造変化ではない
    assert db.table_counts(conn)["upcoming_races"] == 24  # 一覧からの行は入る
    row = conn.execute("SELECT entry_status FROM upcoming_races WHERE race_id='202606040601'").fetchone()
    assert row["entry_status"] == "list_only"


def test_race_list_not_published_yet(conn, store):
    updater = Updater(conn, FakeShutubaClient(), store, today=date(2026, 9, 19))
    assert updater.fetch_upcoming_day(date(2026, 9, 27)) == 0
    assert updater.n_warnings == 0
    assert db.table_counts(conn)["upcoming_races"] == 0


def test_finished_races_are_not_refetched_as_entries(conn, store):
    """結果が確定済みのレースは出馬表を取りに行かない。"""
    from keiba_data.scrapers.race_result import parse_race_result

    # 9/20の1Rが既に確定済みだった、という状態を作る
    db.save_race_page(conn, parse_race_result(load_fixture("result_202606040411_g2.html"), "202606040601"))

    client = FakeShutubaClient()
    Updater(conn, client, store, today=date(2026, 9, 19)).fetch_upcoming_day(UPCOMING_DAY)
    assert not any("race_id=202606040601" in u for u in client.urls)


def test_fetch_track_conditions_saves_todays_baba(conn, tmp_path):
    """JRAの馬場情報ページから、当日を含む直近の馬場情報を取り込む。"""
    class BabaClient:
        n_requests = 0

        def get_text(self, url, *, encoding):
            BabaClient.n_requests += 1
            assert encoding == "shift_jis"
            name = "baba_cushion_20260920.html" if "cushion" in url else "baba_moist_20260920.html"
            return load_fixture(name)

        def post_text(self, url, data, *, encoding):
            """当日の馬場状態・天候のJSON（全場ぶんが1回で返る）。"""
            BabaClient.n_requests += 1
            assert data == {"CNAME": config.BABA_CONDITION_CNAME}
            return load_fixture("baba_condition_20260920.json")

    updater = Updater(conn, BabaClient(), HtmlStore(tmp_path / "html"), today=date(2026, 9, 20))
    saved = updater.fetch_track_conditions(date(2026, 9, 20))
    assert saved >= 20 and updater.n_warnings == 0

    row = dict(conn.execute(
        "SELECT * FROM track_conditions WHERE venue_code='06' AND date='2026-09-20'"
    ).fetchone())
    assert row["cushion_value"] == 10.0                 # クッション値のページ
    assert row["turf_moisture_goal"] == 13.2            # 含水率のページ（同じ行にまとまる）
    assert row["dirt_moisture_goal"] == 6.3
    assert row["measured_at"] == "2026-09-20 07:00"
    # 当日の馬場状態・天候（JSON由来。まだ1レースも終わっていない時間帯でも分かる）
    assert (row["going_turf"], row["going_dirt"], row["weather"]) == ("重", "不良", "雨")
    assert "jra.go.jp" in row["source_pdf"]             # 取得元が分かる
    # 取得したHTMLは保存しておく（あとで作り直せるように）
    assert (tmp_path / "html").exists()


# --- JRA公式からの当日・前日の結果 ---------------------------------------------

JRA_INDEX = load_fixture("jra_result_index_20260920.html")
JRA_MENU = load_fixture("jra_race_menu_20260920_nakayama.html")
JRA_NAKAYAMA = load_fixture("jra_meeting_20260920_nakayama.html")
JRA_HANSHIN = load_fixture("jra_meeting_20260919_hanshin.html")


class FakeJraClient:
    """JRA公式の3段階（トップ→開催選択→レース選択→結果一覧）を模した偽クライアント。

    `serve` に無い競馬場はページが取れなかったことにして、開催ごとに飛ばせることを確かめる。
    """

    _TAIL = re.compile(r"(\d{2})(\d{4})(\d{2})(\d{2})(\d{8})/\w{2}$")

    def __init__(self, serve: dict[str, str], menu: str | None = None):
        self.n_requests = 0
        self.cnames: list[str] = []
        self.serve = serve  # 場コード -> 結果一覧のHTML
        self.menu = menu    # レース選択ページ（本物のフィクスチャを使いたいとき）

    def get_text(self, url, *, encoding):
        self.n_requests += 1
        assert encoding == "cp932"
        return "<a href='#' onClick=\"doAction('/JRADB/accessS.html','pw01sli00/AF');\">レース結果</a>"

    def post_text(self, url, data, *, encoding):
        self.n_requests += 1
        assert encoding == "cp932"
        cname = data["cname"]
        self.cnames.append(cname)
        if "sli" in cname:
            return JRA_INDEX
        matched = self._TAIL.search(cname)
        venue_code = matched.group(1)
        if venue_code not in self.serve:
            return None  # この開催はページが取れなかったことにする
        if "srl" in cname:
            if self.menu is not None:
                return self.menu
            # レース選択ページ（同じ開催の結果一覧へのリンクを持つ）
            return (
                "<a href='#' onclick=\"doAction('/JRADB/accessS.html', "
                f"'pw01ses1{matched.group(0)}');\">レース結果一覧</a>"
            )
        return self.serve[venue_code]


def test_fetch_jra_results_saves_races_and_laps(conn, store):
    client = FakeJraClient({"06": JRA_NAKAYAMA})
    updater = Updater(conn, client, store, today=date(2026, 9, 20))

    saved = updater.fetch_jra_results([date(2026, 9, 20)])

    assert saved == 3  # フィクスチャは3レースぶん
    counts = db.table_counts(conn)
    assert counts["races"] == 3
    assert counts["race_laps"] == 9 + 8 + 11
    assert counts["entries"] == counts["results"] == counts["horses"] == 0  # 馬ごとの結果は入れない
    assert updater.n_warnings == 0

    row = conn.execute("SELECT * FROM races WHERE race_id = '202606040611'").fetchone()
    assert (row["race_date"], row["venue_code"], row["surface"], row["grade"]) == (
        "2026-09-20", "06", "turf", "GII",
    )
    # トップ→開催選択→（中山: レース選択→結果一覧）→（阪神: レース選択で取得できず）
    assert client.n_requests == 5
    # 取得したHTMLは保存してあるので、あとから作り直せる
    assert store.get("jra_result_list", "2026-09-20_06")


def test_fetch_jra_results_keeps_jump_races_without_laps(conn, store):
    client = FakeJraClient({"09": JRA_HANSHIN})
    updater = Updater(conn, client, store, today=date(2026, 9, 20))

    assert updater.fetch_jra_results([date(2026, 9, 19)]) == 2
    assert updater.n_warnings == 0
    jump = conn.execute("SELECT * FROM races WHERE race_id = '202609040504'").fetchone()
    assert jump["surface"] == "jump"
    assert conn.execute(
        "SELECT count(*) FROM race_laps WHERE race_id = '202609040504'"
    ).fetchone()[0] == 0


def test_fetch_jra_results_without_a_meeting(conn, store):
    """その日に開催が無ければ、結果一覧までは取りに行かない。"""
    client = FakeJraClient({"06": JRA_NAKAYAMA})
    updater = Updater(conn, client, store, today=date(2026, 9, 20))
    assert updater.fetch_jra_results([date(2026, 9, 21)]) == 0
    assert client.n_requests == 2  # トップと開催選択だけ
    assert db.table_counts(conn)["races"] == 0


def test_fetch_jra_results_reports_layout_change(conn, store):
    client = FakeJraClient({"06": JRA_NAKAYAMA.replace("race_result_unit", "race_result_block")})
    updater = Updater(conn, client, store, today=date(2026, 9, 20))
    assert updater.fetch_jra_results([date(2026, 9, 20)]) == 0
    assert updater.n_warnings == 1
    assert conn.execute("SELECT count(*) FROM parse_warnings").fetchone()[0] == 1


def test_fetch_jra_results_also_saves_race_links(conn, store):
    """レース選択ページに載っている「レースごとのJRAページ」へのリンクも一緒に保存する。

    このページは結果を取るのにどのみち開くので、**リクエストは増えない**。
    """
    client = FakeJraClient({"06": JRA_NAKAYAMA}, menu=JRA_MENU)
    updater = Updater(conn, client, store, today=date(2026, 9, 20))

    updater.fetch_jra_results([date(2026, 9, 20)])

    assert client.n_requests == 5  # 今までと同じ
    rows = conn.execute(
        "SELECT race_id, jra_cname FROM races WHERE jra_cname IS NOT NULL ORDER BY race_id"
    ).fetchall()
    assert [r["race_id"] for r in rows] == ["202606040601", "202606040602", "202606040611"]
    assert rows[0]["jra_cname"] == "pw01sde0106202604060120260920/40"


class FakeJraLinkClient:
    """過去分のリンク集め（トップ→過去レース結果検索→月→開催）を模した偽クライアント。"""

    def __init__(self):
        self.n_requests = 0
        self.cnames: list[str] = []

    def get_text(self, url, *, encoding):
        self.n_requests += 1
        return "<a href='#' onclick=\"doAction('/JRADB/accessS.html','pw01skl00999999/B3');\">過去</a>"

    def post_text(self, url, data, *, encoding):
        self.n_requests += 1
        cname = data["cname"]
        self.cnames.append(cname)
        if "skl00999999" in cname:   # 過去レース結果検索（月ごとのトークン表を持つ）
            return '<script>objParam["2609"]="54";var yearMonth = "202609";</script>'
        if "skl" in cname:           # 月ページ（その月の開催一覧）
            return (
                "<a href='#' onclick=\"doAction('/JRADB/accessS.html', "
                "'pw01srl00062026040620260920/1B');\">4回中山6日</a>"
            )
        return JRA_MENU              # 開催のレース選択ページ


def test_fetch_jra_race_links_fills_past_meetings(conn, store):
    db.save_jra_race(conn, {
        "race_id": "202606040601", "race_date": "2026-09-20", "venue_code": "06",
        "kaiji": 4, "nichime": 6, "race_no": 1,
    }, [])
    client = FakeJraLinkClient()
    updater = Updater(conn, client, store, today=date(2026, 9, 20))

    saved = updater.fetch_jra_race_links(date(2026, 9, 1))

    assert saved == 1
    assert conn.execute(
        "SELECT jra_cname FROM races WHERE race_id = '202606040601'"
    ).fetchone()[0] == "pw01sde0106202604060120260920/40"
    # トップ・検索・月・開催の4リクエスト
    assert client.n_requests == 4
    # 埋まったあとは何も取りに行かない（途中で止めても続きから進む）
    assert updater.fetch_jra_race_links(date(2026, 9, 1)) == 0
    assert client.n_requests == 4


# --- オッズ（JRA公式） ---------------------------------------------------------

ODDS_RACE_ID = "202606040801"
ODDS_MENU = load_fixture("jra_odds_menu_20260926_nakayama.html")
# オッズの開催選択ページ（実物は開催のリンクが並ぶだけなので、要るところだけ組む）
ODDS_INDEX = (
    "<a href='#' onclick=\"doAction('/JRADB/accessO.html',"
    "'pw15orl00062026040820260926/A1');\">4回中山8日</a>"
)
ODDS_PAGES = {
    "1": load_fixture(f"jra_odds_tansho_{ODDS_RACE_ID}.html"),
    "3": load_fixture(f"jra_odds_wakuren_{ODDS_RACE_ID}.html"),
    "4": load_fixture(f"jra_odds_umaren_{ODDS_RACE_ID}.html"),
    "5": load_fixture(f"jra_odds_wide_{ODDS_RACE_ID}.html"),
    "6": load_fixture(f"jra_odds_umatan_{ODDS_RACE_ID}.html"),
    "7": load_fixture(f"jra_odds_sanrenpuku_{ODDS_RACE_ID}.html"),
    "8": load_fixture(f"jra_odds_sanrentan_{ODDS_RACE_ID}.html"),
}


class FakeOddsClient:
    """オッズの3段階（トップ→開催選択→レース選択→券種ごとのページ）を模した偽クライアント。"""

    def __init__(self, serve: dict[str, str] | None = None):
        self.n_requests = 0
        self.cnames: list[str] = []
        self.serve = ODDS_PAGES if serve is None else serve

    def get_text(self, url, *, encoding):
        self.n_requests += 1
        return "<a href='#' onclick=\"doAction('/JRADB/accessO.html','pw15oli00/6D');\">オッズ</a>"

    def post_text(self, url, data, *, encoding):
        self.n_requests += 1
        assert url == config.JRA_ODDS_URL
        cname = data["cname"]
        self.cnames.append(cname)
        if "oli" in cname:
            return ODDS_INDEX
        if "orl" in cname:
            return ODDS_MENU
        return self.serve.get(cname[4])   # pw15**1**ou… の券種の1桁


def _odds_updater(conn, store, client=None):
    return Updater(conn, client or FakeOddsClient(), store, today=date(2026, 9, 26))


def test_fetch_odds_takes_every_bet_type(conn, store):
    client = FakeOddsClient()
    saved = _odds_updater(conn, store, client).fetch_odds(ODDS_RACE_ID)

    assert saved == {
        "tansho": 13, "fukusho": 13, "wakuren": 33, "umaren": 78,
        "wide": 78, "umatan": 156, "sanrenpuku": 286, "sanrentan": 1716,
    }
    # トップ＋開催選択＋レース選択の3回と、券種のページ7回（単複は1ページ）
    assert client.n_requests == 10
    assert conn.execute("SELECT COUNT(*) FROM odds").fetchone()[0] == sum(saved.values())


def test_fetch_odds_needs_only_one_request_per_bet_type_the_second_time(conn, store):
    """トークンをためてあれば、券種の切り替えは1リクエストで済む。"""
    _odds_updater(conn, store).fetch_odds(ODDS_RACE_ID)

    client = FakeOddsClient()
    _odds_updater(conn, store, client).fetch_odds(ODDS_RACE_ID, ["umaren"])
    assert client.n_requests == 1
    assert client.cnames == ["pw154ouS306202604080120260926Z/07"]


def test_fetch_odds_remembers_when_the_odds_are_from(conn, store):
    _odds_updater(conn, store).fetch_odds(ODDS_RACE_ID, ["umaren"])
    row = conn.execute("SELECT odds_label, n_combos FROM odds_updates").fetchone()
    assert (row["odds_label"], row["n_combos"]) == ("最終オッズ", 78)


def test_fetch_odds_keeps_the_html_so_it_can_be_reparsed(conn, store):
    _odds_updater(conn, store).fetch_odds(ODDS_RACE_ID, ["umaren"])
    assert store.get("jra_odds_umaren", ODDS_RACE_ID)


def test_fetch_odds_skips_a_bet_type_that_is_not_on_sale(conn, store):
    """発売されていない券種はリンクが無い（少頭数の枠連など）。黙って飛ばす。"""
    client = FakeOddsClient()
    saved = _odds_updater(conn, store, client).fetch_odds("202606040804", ["wakuren", "umaren"])
    assert "wakuren" not in saved and "umaren" in saved


def test_fetch_odds_of_a_meeting_without_odds_saves_nothing(conn, store):
    client = FakeOddsClient()
    # 阪神（場コード09）はオッズの開催選択ページに出ていない
    assert _odds_updater(conn, store, client).fetch_odds("202609040801") == {}
    assert conn.execute("SELECT COUNT(*) FROM odds").fetchone()[0] == 0


def test_fetch_odds_refreshes_the_tokens_from_the_page_it_read(conn, store):
    """どのオッズページにも7券種ぶんのリンクが載っているので、そこで拾い直す。

    馬連のトークンしか持っていなくても、馬連を取りに行ったついでに
    残りの券種のトークンもそろう（次からはレース選択を辿らなくて済む）。
    """
    db.save_jra_odds_links(
        conn, {ODDS_RACE_ID: {"umaren": "pw154ouS306202604080120260926Z/07"}}
    )
    client = FakeOddsClient()
    _odds_updater(conn, store, client).fetch_odds(ODDS_RACE_ID, ["umaren"])

    assert client.n_requests == 1                # レース選択を辿らずに済んでいる
    assert len(db.get_jra_odds_links(conn, ODDS_RACE_ID)) == 8


def test_fetch_odds_reports_a_layout_change(conn, store):
    client = FakeOddsClient({"4": "<html><body>なにもない</body></html>"})
    assert _odds_updater(conn, store, client).fetch_odds(ODDS_RACE_ID, ["umaren"]) == {}
    row = conn.execute("SELECT message FROM parse_warnings").fetchone()
    assert "読めませんでした" in row["message"]


# --- 出馬表（JRA公式。血統・馬主と馬体重） -------------------------------------

ENTRY_RACE_ID = "202606040601"
JRA_SHUTUBA = load_fixture("jra_shutuba_202606040601.html")
JRA_SHUTUBA_BEFORE_WEIGHT = load_fixture("jra_shutuba_before_weight.html")
# 開催選択・レース選択ページ（実物はリンクが並ぶだけなので、要るところだけ組む）
ENTRY_INDEX = (
    "<a href='#' onclick=\"doAction('/JRADB/accessD.html',"
    "'pw01drl00062026040620260920/AA');\">4回中山6日</a>"
)
ENTRY_MENU = "\n".join(
    f"<a href='/JRADB/accessD.html?CNAME=pw01dde010620260406{r:02d}20260920/84'>{r}R</a>"
    for r in range(1, 13)
)


class FakeEntryClient:
    """出馬表の3段階（トップ→開催選択→レース選択→各レースの出馬表）を模した偽クライアント。"""

    def __init__(self, shutuba: str | None = None, broken_cnames=()):
        self.n_requests = 0
        self.cnames: list[str] = []
        self.shutuba = JRA_SHUTUBA if shutuba is None else shutuba
        self.broken = set(broken_cnames)   # 古くなったトークン（JRAが作り直したあと）

    def get_text(self, url, *, encoding):
        self.n_requests += 1
        if url == config.JRA_TOP_URL:
            return "<a href='#' onclick=\"doAction('/JRADB/accessD.html','pw01dli00/F3');\">出馬表</a>"
        cname = url.rsplit("CNAME=", 1)[-1]
        self.cnames.append(cname)
        if cname in self.broken:
            return "<html><body>ページが見つかりません</body></html>"
        return self.shutuba

    def post_text(self, url, data, *, encoding):
        self.n_requests += 1
        assert url == config.JRA_ENTRY_URL
        cname = data["cname"]
        self.cnames.append(cname)
        if "dli" in cname:
            return ENTRY_INDEX
        if "drl" in cname:
            return ENTRY_MENU
        return self.shutuba


def _entry_updater(conn, store, client=None):
    return Updater(conn, client or FakeEntryClient(), store, today=date(2026, 9, 20))


def _upcoming_entries(conn, race_id=ENTRY_RACE_ID, n=15):
    """突き合わせ先の出走馬（netkeibaの出馬表で入る側）を用意する。"""
    from keiba_data.scrapers.jra_result import parse_shutuba_profiles

    names = [p.horse_name for p in parse_shutuba_profiles(JRA_SHUTUBA)]
    conn.execute(
        "INSERT INTO upcoming_races (race_id, race_date, venue_code, entry_status, "
        "fetched_at, updated_at) VALUES (?, '2026-09-20', '06', 'entries', '', '')",
        (race_id,),
    )
    conn.executemany(
        "INSERT INTO upcoming_entries (race_id, seq, umaban, horse_id, horse_name) "
        "VALUES (?, ?, ?, ?, ?)",
        [(race_id, i, i, f"H{i}", names[i - 1]) for i in range(1, n + 1)],
    )
    conn.commit()


def test_fetch_jra_entry_race_saves_the_horse_weight(conn, store):
    """「このレースを最新に更新」から呼ぶ道。馬体重はJRA公式にしか無い。"""
    _upcoming_entries(conn)
    client = FakeEntryClient()
    saved = _entry_updater(conn, store, client).fetch_jra_entry_race(ENTRY_RACE_ID)

    assert saved == 15
    rows = {r["umaban"]: r for r in conn.execute(
        "SELECT umaban, horse_weight, weight_diff FROM upcoming_entries"
    )}
    assert (rows[1]["horse_weight"], rows[1]["weight_diff"]) == (480, -2)
    assert (rows[3]["horse_weight"], rows[3]["weight_diff"]) == (470, -10)
    assert all(r["horse_weight"] for r in rows.values())
    # 血統も今までどおり入る
    assert conn.execute("SELECT sire FROM horses WHERE horse_id = 'H1'").fetchone()[0] == "フィエールマン"
    # トップ＋開催選択＋レース選択＋出馬表の4リクエスト
    assert client.n_requests == 4
    assert store.get("jra_shutuba", ENTRY_RACE_ID)


def test_fetch_jra_entry_race_needs_only_one_request_the_second_time(conn, store):
    """トークンをためてあるので、2回目は出馬表の1リクエストで済む。"""
    _upcoming_entries(conn)
    _entry_updater(conn, store).fetch_jra_entry_race(ENTRY_RACE_ID)
    assert db.get_jra_entry_link(conn, ENTRY_RACE_ID) == "pw01dde010620260406012026 0920/84".replace(" ", "")

    client = FakeEntryClient()
    assert _entry_updater(conn, store, client).fetch_jra_entry_race(ENTRY_RACE_ID) == 15
    assert client.n_requests == 1


def test_fetch_jra_entry_race_collects_the_tokens_of_the_whole_meeting(conn, store):
    """1レース取りに行くついでに、その開催の全レースぶんのトークンをためる。"""
    _upcoming_entries(conn)
    _entry_updater(conn, store).fetch_jra_entry_race(ENTRY_RACE_ID)
    assert conn.execute("SELECT COUNT(*) FROM jra_entry_links").fetchone()[0] == 12


def test_fetch_jra_entry_race_before_the_weights_are_published(conn, store):
    """発走の1時間ほど前までは馬体重が載っていない。血統だけ入れて、体重はNULLのまま。"""
    _upcoming_entries(conn)
    client = FakeEntryClient(JRA_SHUTUBA_BEFORE_WEIGHT)
    saved = _entry_updater(conn, store, client).fetch_jra_entry_race(ENTRY_RACE_ID)

    assert saved == 14                            # 血統は読める
    assert conn.execute(
        "SELECT COUNT(*) FROM upcoming_entries WHERE horse_weight IS NOT NULL"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM parse_warnings").fetchone()[0] == 0   # 警告にしない


def test_fetch_jra_entry_race_walks_again_when_the_token_is_stale(conn, store):
    """ためてあるトークンでページが読めなければ、辿り直して取り直す。

    JRAがトークンを作り直すと、ためてある方では読めなくなる。
    そのときは開催選択から辿って拾い直す（構造変化の警告は出さない）。
    """
    _upcoming_entries(conn)
    stale = "pw01dde0106202604060120260920/ZZ"       # チェックサムが変わった状態
    db.save_jra_entry_links(conn, {ENTRY_RACE_ID: stale})
    client = FakeEntryClient(broken_cnames=[stale])

    assert _entry_updater(conn, store, client).fetch_jra_entry_race(ENTRY_RACE_ID) == 15
    # 古いトークンで1回 → トップ・開催選択・レース選択 → 拾い直したトークンで1回
    assert client.n_requests == 5
    assert db.get_jra_entry_link(conn, ENTRY_RACE_ID) != stale
    assert conn.execute("SELECT COUNT(*) FROM parse_warnings").fetchone()[0] == 0


def test_fetch_jra_entry_race_of_a_meeting_without_entries(conn, store):
    """出馬表が出ていない開催（阪神＝場コード09）では何もしない。"""
    client = FakeEntryClient()
    assert _entry_updater(conn, store, client).fetch_jra_entry_race("202609040801") == 0
    assert conn.execute("SELECT COUNT(*) FROM jra_entry_links").fetchone()[0] == 0


def test_fetch_jra_entries_saves_the_weights_too(conn, store):
    """CLIの `keiba jra-entries`（開催まるごと）でも馬体重が入る。"""
    _upcoming_entries(conn)
    client = FakeEntryClient()
    saved = _entry_updater(conn, store, client).fetch_jra_entries([date(2026, 9, 20)])

    assert saved == 15            # 出走馬をDBに入れてあるのは1レースぶんだけ
    assert conn.execute(
        "SELECT COUNT(*) FROM upcoming_entries WHERE horse_weight IS NOT NULL"
    ).fetchone()[0] == 15
    assert conn.execute("SELECT COUNT(*) FROM jra_entry_links").fetchone()[0] == 12
