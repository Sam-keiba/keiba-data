"""JRA公式の競走馬検索・競走馬の詳細ページのパース（scrapers/jra_horse.py）と、
血統の取り込み（updater.fetch_horse_pedigree）。"""

from datetime import date

import pytest

from keiba_data import db
from keiba_data.html_store import HtmlStore
from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.jra_horse import (
    build_search_cname,
    parse_horse_profile,
    parse_horse_search,
)
from keiba_data.updater import Updater
from tests.conftest import load_fixture

SEARCH_CNAME = "pw02uliD10000"
LIMIT = 200


@pytest.fixture
def store(tmp_path):
    return HtmlStore(tmp_path / "html")


def contrail_search():
    return load_fixture("jra_horse_search_contrail.html")


def kon_search():
    return load_fixture("jra_horse_search_kon.html")


def contrail_page():
    return load_fixture("jra_horse_contrail.html")


# --- 検索トークン -----------------------------------------------------------------

def test_build_search_cname_is_assembled_without_a_checksum():
    """ほかのJRAのページと違い、検索のトークンだけは自力で組める。"""
    assert build_search_cname("コントレイル", SEARCH_CNAME) == "pw02uliD10000コントレイル"


def test_build_search_cname_rejects_a_single_character():
    """JRAは全角カタカナ2文字以上しか受け付けない。"""
    with pytest.raises(ValueError):
        build_search_cname("コ", SEARCH_CNAME)


# --- 検索結果 ---------------------------------------------------------------------

def test_parse_horse_search_separates_horses_with_the_same_name():
    """同じ馬名の馬が複数いる。**生年（血統登録番号の先頭4桁）**で分けられる。"""
    page = parse_horse_search(contrail_search(), "コントレイル", LIMIT)
    assert not page.capped and not page.warnings
    assert [(h.name, h.sex, h.birth_year) for h in page.hits] == [
        ("コントレイル", "牡", 2017),
        ("コントレイル", "牝", 2010),
    ]
    first = page.hits[0]
    assert first.cname == "pw01dud002017101835/0D"    # 組み立てられないので拾う
    assert first.horse_no == "2017101835"
    assert (first.trainer_name, first.stable, first.is_retired) == ("矢作 芳人", "栗東", True)


def test_parse_horse_search_normalises_the_gelding_label():
    """検索結果は「せん」表記。DBの `horses.sex` に合わせて「セ」にそろえる。"""
    page = parse_horse_search(kon_search(), "コン", LIMIT)
    assert {h.sex for h in page.hits} <= {"牡", "牝", "セ"}
    assert any(h.sex == "セ" for h in page.hits)


def test_parse_horse_search_flags_the_cap():
    """200件ちょうどなら取りこぼしを疑う（頭を1文字伸ばして引き直す合図）。"""
    page = parse_horse_search(kon_search(), "コン", LIMIT)
    assert len(page.hits) == LIMIT and page.capped


def test_parse_horse_search_raises_on_a_parameter_error():
    """トークンの文字コードを間違えるとJRAは「パラメータエラー」を返す。"""
    with pytest.raises(LayoutError):
        parse_horse_search(
            "<html><head><title>パラメータエラー JRA</title></head><body></body></html>",
            "コン", LIMIT,
        )


def test_parse_horse_search_returns_nothing_for_a_page_without_rows():
    """1頭も当たらないのは普通のこと（例外にしない）。"""
    page = parse_horse_search("<html><body><table></table></body></html>", "ヌヌヌ", LIMIT)
    assert page.hits == [] and not page.capped


# --- 詳細ページ -------------------------------------------------------------------

def test_parse_horse_profile_reads_the_pedigree():
    detail = parse_horse_profile(contrail_page())
    profile = detail.profile
    assert profile.horse_name == "コントレイル"       # 「競走馬情報」と英名を落とす
    assert profile.sire == "ディープインパクト"
    assert profile.dam == "ロードクロサイト"           # 「産駒」のリンクを落とす
    assert profile.broodmare_sire == "Unbridled's Song"
    assert profile.owner == "前田 晋二"
    assert profile.breeder == "(株)ノースヒルズ"
    assert detail.birth_date == "2017-04-01"
    assert detail.sex == "牡"
    assert detail.sire_no == "2002100816"            # 父の血統登録番号
    assert detail.broodmare_sire_no is None          # 外国産の母の父はリンクが無い
    assert (detail.trainer_name, detail.stable) == ("矢作 芳人", "栗東")
    assert (detail.coat_color, detail.birthplace) == ("青鹿毛", "新冠町")


def test_parse_horse_profile_raises_without_a_profile_block():
    with pytest.raises(LayoutError):
        parse_horse_profile("<html><body><h1>なにもない</h1></body></html>")


# --- 取り込み ---------------------------------------------------------------------

class FakeHorseClient:
    """検索と詳細ページを返す偽のHTTPクライアント。"""

    def __init__(self):
        self.n_requests = 0
        self.searched: list[str] = []
        self.pages: list[str] = []

    def post_text(self, url, data, *, encoding):
        self.n_requests += 1
        word = data["cname"].decode("cp932").removeprefix(SEARCH_CNAME)
        self.searched.append(word)
        if word == "コン":
            return kon_search()              # 200件（上限）
        if "コントレイル".startswith(word) or word == "コントレイル":
            return contrail_search()
        return "<html><body><table></table></body></html>"

    def get_text(self, url, *, encoding):
        self.n_requests += 1
        self.pages.append(url)
        return contrail_page()


def _seed(conn, horse_id: str, name: str, sex: str, age: int, race_date: str = "2026-04-05"):
    """出走した馬を1頭だけ作る（血統の取り込みは「出走した馬」だけを対象にする）。"""
    conn.execute("INSERT OR IGNORE INTO venues (venue_code, venue_name) VALUES ('05', '東京')")
    race_id = f"2026050101{horse_id[-2:]}"
    conn.execute(
        "INSERT OR IGNORE INTO races (race_id, race_date, venue_code, kaiji, nichime, race_no,"
        " fetched_at, updated_at) VALUES (?, ?, '05', 1, 1, 1, '', '')", (race_id, race_date))
    conn.execute(
        "INSERT OR IGNORE INTO horses (horse_id, horse_name, sex, updated_at) VALUES (?, ?, ?, '')",
        (horse_id, name, sex))
    conn.execute(
        "INSERT OR IGNORE INTO entries (race_id, umaban, horse_id, age) VALUES (?, 1, ?, ?)",
        (race_id, horse_id, age))
    conn.commit()


def _updater(conn, store):
    client = FakeHorseClient()
    return Updater(conn, client, store, today=date(2026, 9, 28)), client


def test_fetch_horse_pedigree_fills_the_sire(conn, store):
    _seed(conn, "1001", "コントレイル", "牡", 9)      # 2026-9=2017年生
    updater, client = _updater(conn, store)

    assert updater.fetch_horse_pedigree() == 1
    row = conn.execute("SELECT * FROM horses WHERE horse_id = '1001'").fetchone()
    assert row["sire"] == "ディープインパクト"
    assert row["broodmare_sire"] == "Unbridled's Song"
    assert row["breeder"] == "(株)ノースヒルズ"
    assert row["birth_date"] == "2017-04-01"
    # 詳細ページは検索結果のトークンから開く（組み立てない）
    assert client.pages == ["https://www.jra.go.jp/JRADB/accessU.html?CNAME=pw01dud002017101835/0D"]


def test_fetch_horse_pedigree_picks_the_right_horse_of_the_same_name(conn, store):
    """同名の2頭のうち、**生年が合うほう**を選ぶ（抹消馬の馬齢は当てにならない）。"""
    _seed(conn, "1002", "コントレイル", "牝", 16)     # 2026-16=2010年生のほう
    updater, client = _updater(conn, store)
    updater.fetch_horse_pedigree()
    # 2017年生の牡（…2017101835…）ではなく、2010年生の牝のページを開いている
    assert "pw01dud002010100166" in client.pages[0]
    assert "2017101835" not in client.pages[0]


def test_fetch_horse_pedigree_digs_deeper_when_the_search_is_capped(conn, store):
    """200件で頭打ちの頭文字は、1文字伸ばして引き直す。"""
    _seed(conn, "1003", "コントレイル", "牡", 9)
    updater, client = _updater(conn, store)
    updater.fetch_horse_pedigree()
    assert client.searched[0] == "コン"              # まず2文字
    assert "コント" in client.searched               # 上限だったので3文字へ
    assert conn.execute(
        "SELECT capped FROM jra_horse_search WHERE prefix = 'コン'").fetchone()["capped"] == 1


def test_fetch_horse_pedigree_resumes_without_searching_again(conn, store):
    """2回目は父が入っている馬を飛ばすので、検索もページ取得も起きない。"""
    _seed(conn, "1004", "コントレイル", "牡", 9)
    updater, client = _updater(conn, store)
    updater.fetch_horse_pedigree()
    before = client.n_requests

    updater2, client2 = _updater(conn, store)
    assert updater2.fetch_horse_pedigree() == 0
    assert client2.n_requests == 0 and before > 0


def test_fetch_horse_pedigree_reuses_the_saved_html(conn, store):
    """保存済みHTMLがあれば取りに行かない（パーサを直したあとの取り直しが無料になる）。"""
    _seed(conn, "1005", "コントレイル", "牡", 9)
    updater, client = _updater(conn, store)
    updater.fetch_horse_pedigree()
    conn.execute("UPDATE horses SET sire = NULL WHERE horse_id = '1005'")
    conn.commit()

    updater2, client2 = _updater(conn, store)
    assert updater2.fetch_horse_pedigree() == 1
    assert client2.pages == []                       # 詳細ページは取りに行っていない
    assert client2.searched == []                    # トークンもDBに残っている


def test_count_pedigree_reports_progress(conn, store):
    _seed(conn, "1006", "コントレイル", "牡", 9)
    assert db.count_pedigree(conn) == (0, 1)
    updater, _ = _updater(conn, store)
    updater.fetch_horse_pedigree()
    assert db.count_pedigree(conn) == (1, 1)
