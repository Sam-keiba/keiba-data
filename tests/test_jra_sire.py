"""JRA公式の種牡馬リーディングのパース（scrapers/jra_sire.py）と、その集計（sire_data.py）。"""

from datetime import date

import pytest

from keiba_data.db import get_jra_sire_links, save_sire_leading
from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.jra_sire import (
    BMS_MARKER,
    KIND_ALL,
    find_index_cname,
    parse_sire_leading,
)
from keiba_data import sire_data
from keiba_data.html_store import HtmlStore
from keiba_data.updater import Updater
from tests.conftest import load_fixture


@pytest.fixture
def store(tmp_path):
    return HtmlStore(tmp_path / "html")


def index_html():
    return load_fixture("jra_leading_index_20260928.html")


def leading_html(year: int = 2026):
    return load_fixture(f"jra_sire_leading_{year}p1.html")


def test_find_index_cname():
    """リーディング情報のページから、種牡馬リーディングへの入口を拾える。"""
    assert find_index_cname(index_html()) == "pt03hld0099993101/A1"


def test_find_index_cname_for_bms_is_absent_from_this_page():
    """ブルードメアサイヤーは静的なリンク（bm2025.html）なので、この入口には無い。"""
    assert find_index_cname(index_html(), BMS_MARKER) is None


def test_find_index_cname_returns_none_when_missing():
    assert find_index_cname("<html><body>なにもない</body></html>") is None


def test_parse_sire_leading_reads_the_first_row():
    page = parse_sire_leading(leading_html())
    assert (page.year, page.kind, page.as_of) == (2026, KIND_ALL, "2026-09-27")
    assert len(page.rows) == 20          # 1ページ20頭（上位100頭が5ページに分かれる）

    kizuna = page.rows[0]
    assert kizuna.sire_name == "キズナ"       # 「キズナ（2010年）」から生年を切り離す
    assert kizuna.birth_year == 2010
    assert (kizuna.rank, kizuna.coat_color, kizuna.birthplace) == (1, "青鹿毛", "新冠町")
    assert (kizuna.n_horses, kizuna.n_winners) == (276, 93)
    assert (kizuna.n_starts, kizuna.n_wins) == (952, 117)
    assert kizuna.prize_yen == 3_021_929_000    # 「3,021,929,000円」
    assert kizuna.prize_per_horse == 10_949_018
    assert (kizuna.win_rate, kizuna.ei) == (0.337, 1.86)


def test_parse_sire_leading_collects_the_tokens_it_cannot_build():
    """年度・ページャ・種類のcnameは末尾がチェックサムなので、ページの値をそのまま持つ。"""
    page = parse_sire_leading(leading_html())
    assert page.year_cnames[2025] == "pt03hld0020253101/CB"
    assert min(page.year_cnames) == 2009 and max(page.year_cnames) == 2026
    # このフィクスチャは年度セレクタ経由（…0020263101…）で取ったページなので、
    # ページャのトークンにもその年が入っている
    assert page.page_cnames[2] == "pt03hld0020263102/88"
    assert sorted(page.page_cnames) == [2, 3, 4, 5]
    assert page.kind_cnames["two"] == "pt03hld0020261101/FD"


def test_ei_is_the_prize_per_horse_divided_by_the_field():
    """E・Iは「1頭平均賞金 ÷ 全出走馬の1頭平均賞金」。逆算した分母は全行でほぼ揃う。"""
    rows = [r for r in parse_sire_leading(leading_html()).rows if r.ei and r.prize_per_horse]
    denominators = [r.prize_per_horse / r.ei for r in rows]
    # E・Iが小数2桁に丸められているぶんの誤差だけ（数%以内）に収まる
    assert max(denominators) / min(denominators) < 1.05


def test_parse_sire_leading_raises_on_a_page_without_the_table():
    with pytest.raises(LayoutError):
        parse_sire_leading("<html><body><h2>2026年度 リーディングサイヤー（全馬）</h2></body></html>")


def test_parse_sire_leading_raises_without_a_heading():
    with pytest.raises(LayoutError):
        parse_sire_leading("<html><body><table class='lead'><thead></thead></table></body></html>")


# --- 保存と集計 -------------------------------------------------------------------

def _load(conn, year: int) -> int:
    return save_sire_leading(conn, parse_sire_leading(leading_html(year)))


def test_save_sire_leading_is_idempotent(conn):
    assert _load(conn, 2026) == 20
    assert _load(conn, 2026) == 20          # 同じ年・同じ種牡馬なら上書き
    (count,) = conn.execute("SELECT COUNT(*) FROM sire_leading").fetchone()
    assert count == 20


def test_field_prize_per_horse_is_the_denominator_of_ei(conn):
    _load(conn, 2026)
    field = sire_data.field_prize_per_horse(conn, 2026)
    row = conn.execute(
        "SELECT prize_per_horse, ei FROM sire_leading WHERE sire_name = 'キズナ'"
    ).fetchone()
    assert field == pytest.approx(row["prize_per_horse"] / row["ei"], rel=0.02)


def test_cumulative_aei_matches_the_single_year_value(conn):
    """1年ぶんしか無ければ、通算AEIはその年のE・Iとほぼ同じになるはず。"""
    _load(conn, 2026)
    rows = sire_data.sire_years(conn, "キズナ", [2026])
    assert sire_data.cumulative_aei(conn, rows) == pytest.approx(1.86, rel=0.02)


def test_cumulative_aei_spans_years(conn):
    _load(conn, 2025)
    _load(conn, 2026)
    years = sire_data.recent_years(conn, 5)
    assert years == [2025, 2026]
    rows = sire_data.sire_years(conn, "キズナ", years)
    assert [r.year for r in rows] == [2025, 2026]
    # 2年ぶんの通算は、その2年のE・I（1.94と1.86）の間に入る
    assert 1.80 < sire_data.cumulative_aei(conn, rows) < 2.00


def test_sire_metrics_places_the_sire_in_the_field(conn):
    _load(conn, 2026)
    metrics = {m.key: m for m in sire_data.sire_metrics(conn, "キズナ", [2026])}
    assert set(metrics) == {"win_rate", "starts_per_horse", "prize_per_horse", "aei", "n_horses"}
    # E・Iは定義上1.00が全種牡馬の平均。1頭平均賞金をその平均で割るとE・Iに戻る
    assert metrics["aei"].field_value == 1.0
    assert metrics["prize_per_horse"].ratio_to_field == pytest.approx(
        metrics["aei"].value, rel=0.02
    )
    # 1ページ目（上位20頭）の中でキズナは首位なので、段階は上のほう
    assert metrics["aei"].grade >= 7
    assert all(1 <= m.grade <= sire_data.GRADES for m in metrics.values())


def test_sire_metrics_is_empty_for_an_unknown_sire(conn):
    _load(conn, 2026)
    assert sire_data.sire_metrics(conn, "いない種牡馬", [2026]) == []


# --- 取り込み（updater.fetch_sire_leading） ----------------------------------------

class FakeSireClient:
    """リーディング情報のページと、年度ごとのリーディングを返す偽のHTTPクライアント。

    フィクスチャは2025年と2026年の1ページ目しかないので、2ページ目以降は空を返す
    （`_fetch_sire_year` が「100頭に満たない年」と同じ扱いで止まることの確認にもなる）。
    """

    def __init__(self):
        self.n_requests = 0
        self.cnames: list[str] = []

    def get_text(self, url, *, encoding):
        self.n_requests += 1
        return index_html()

    def post_text(self, url, data, *, encoding):
        self.n_requests += 1
        cname = data["cname"]
        self.cnames.append(cname)
        for year in (2025, 2026):
            # 年度セレクタのトークン（…00{year}3101…）と、入口のトークン（…99993101…）
            if f"{year}3101" in cname:
                return leading_html(year)
        if cname.endswith("3101/A1"):
            return leading_html(2026)
        return "<html><body>ページなし</body></html>"


def _updater(conn, store):
    client = FakeSireClient()
    return Updater(conn, client, store, today=date(2026, 9, 28)), client


def test_fetch_sire_leading_follows_the_entry_page_once(conn, store):
    updater, client = _updater(conn, store)
    saved = updater.fetch_sire_leading([2025, 2026])

    assert saved == 40                      # フィクスチャは1年20頭ぶん
    # 入口（GET 1 + POST 1）＋ 年ごとに 1ページ目と「2ページ目が空」の2回
    assert client.cnames[0].endswith("3101/A1")
    years = {r["year"] for r in conn.execute("SELECT DISTINCT year FROM sire_leading")}
    assert years == {2025, 2026}
    # 年度のトークンがDBに残るので、2回目は入口を辿らずに済む
    assert get_jra_sire_links(conn, KIND_ALL)[2025][1] == "pt03hld0020253101/CB"


def test_fetch_sire_leading_skips_finished_years(conn, store):
    """終わった年は数字が変わらないので、100頭そろっていれば取りに行かない。"""
    updater, client = _updater(conn, store)
    updater.fetch_sire_leading([2025])
    # フィクスチャは20頭ぶんなので「まだ足りない」と見なされ、次も取りに行く
    before = client.n_requests
    updater.fetch_sire_leading([2025])
    assert client.n_requests > before

    # 100頭そろっている状態を作ると飛ばす
    conn.executemany(
        "INSERT OR IGNORE INTO sire_leading (year, kind, sire_name, ei, fetched_at) "
        "VALUES (2025, 'all', ?, 1.0, '2026-01-01')",
        [(f"ダミー{i}",) for i in range(100)],
    )
    conn.commit()
    before = client.n_requests
    updater.fetch_sire_leading([2025])
    assert client.n_requests == before


def test_fetch_sire_leading_always_refetches_the_current_year(conn, store):
    """当年は集計途中なので、そろっていても取り直す。"""
    updater, client = _updater(conn, store)
    conn.executemany(
        "INSERT OR IGNORE INTO sire_leading (year, kind, sire_name, ei, fetched_at) "
        "VALUES (2026, 'all', ?, 1.0, '2026-01-01')",
        [(f"ダミー{i}",) for i in range(100)],
    )
    conn.commit()
    before = client.n_requests
    updater.fetch_sire_leading([2026])
    assert client.n_requests > before


def test_fetch_sire_leading_refuses_a_kind_it_cannot_reach(conn, store):
    """辿れない種類（ブルードメアサイヤー）の枠に、別の種類の数字を保存しない。"""
    updater, client = _updater(conn, store)
    assert updater.fetch_sire_leading([2026], kind="bms") == 0
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM sire_leading WHERE kind = 'bms'"
    ).fetchone()
    assert count == 0
    assert updater.n_warnings >= 1
