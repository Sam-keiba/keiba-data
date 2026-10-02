"""JRAの調教師名鑑（正式名・読み・生年月日・免許取得年）の読み方と取り込み。"""

import pytest

from keiba_data import db
from keiba_data.html_store import HtmlStore
from keiba_data.scrapers import LayoutError, jra_trainer
from keiba_data.updater import Updater
from tests.conftest import load_fixture

INDEX = load_fixture("jra_trainer_index.html")
LIST_NA = load_fixture("jra_trainer_list_na.html")
PROFILE = load_fixture("jra_trainer_01137.html")


def test_index_links_to_the_ten_rows():
    cnames = jra_trainer.parse_list_cnames(INDEX)
    assert len(cnames) == 10                       # あ行〜わ行
    assert cnames[0] == "pw05cnlH1A/BF"


def test_list_gives_the_trainer_code_that_matches_trainer_id():
    """リンクの `pw05cmk0` の直後の5桁が、DBの trainer_id と同じ調教師コード。"""
    links = jra_trainer.parse_trainer_links(LIST_NA)
    first = links[0]
    assert (first.trainer_id, first.cname, first.name) == ("01137", "pw05cmk001137/D0", "中内田 充正")
    assert all(len(link.trainer_id) == 5 for link in links)
    assert len({link.trainer_id for link in links}) == len(links)


def test_profile():
    profile = jra_trainer.parse_trainer_profile(PROFILE, "01137")
    assert profile.full_name == "中内田 充正"
    assert profile.kana == "ナカウチダ ミツマサ"
    assert profile.birth_date == "1978-12-18"
    assert profile.license_year == 2012
    assert profile.stable == "栗東"


def test_profile_layout_change():
    with pytest.raises(LayoutError):
        jra_trainer.parse_trainer_profile("<html><body><h1>別のページ</h1></body></html>", "01137")


class FakeMeikanClient:
    """入口はGET、一覧と個人ページはPOST（cnameで出し分け）。"""

    def __init__(self):
        self.n_requests = 0
        self.posted: list[str] = []

    def get_text(self, url, *, encoding):
        self.n_requests += 1
        return INDEX

    def post_text(self, url, data, *, encoding):
        self.n_requests += 1
        cname = data["cname"]
        self.posted.append(cname)
        if cname.startswith("pw05cnl"):
            return LIST_NA if cname == "pw05cnlH1N/D4" else "<html><body></body></html>"
        return PROFILE.replace("中内田 充正", f"調教師 {cname[8:13]}") if cname != "pw05cmk001137/D0" else PROFILE


def test_fetch_trainer_meikan_fills_trainers_without_touching_the_netkeiba_name(conn, tmp_path):
    with conn:
        conn.execute("INSERT INTO trainers (trainer_id, trainer_name, stable, updated_at) "
                     "VALUES ('01137', '中内田充', '栗東', '2026-01-01')")
    client = FakeMeikanClient()
    updater = Updater(conn, client, HtmlStore(tmp_path / "html"))
    saved = updater.fetch_trainer_meikan()

    assert len(saved) == 10                                     # な行の10人（他の行は空）
    assert client.n_requests == 1 + 10 + 10                     # 入口 + 行10個 + 1人1回
    row = conn.execute("SELECT * FROM trainers WHERE trainer_id = '01137'").fetchone()
    assert (row["trainer_name"], row["full_name"], row["kana"], row["birth_date"], row["license_year"]) == (
        "中内田充", "中内田 充正", "ナカウチダ ミツマサ", "1978-12-18", 2012)
    assert row["meikan_updated_at"] is not None
    # まだ1走もしていない人も行ごと入る（netkeibaの名前は空のまま）
    fresh = conn.execute("SELECT trainer_name, full_name FROM trainers WHERE trainer_id = '01069'").fetchone()
    assert fresh["trainer_name"] is None and fresh["full_name"] == "調教師 01069"
    assert db.table_counts(conn)["trainers"] == 10
