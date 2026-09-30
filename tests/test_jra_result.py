"""JRA公式のレース結果ページのパース（scrapers/jra_result.py）。"""

import re

import pytest

from keiba_data.db import RACE_COLUMNS
from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.jra_result import (
    Meeting,
    find_index_cname,
    find_past_search_cname,
    parse_meeting_results,
    parse_month_links,
    parse_shutuba_profiles,
    parse_race_links,
    parse_race_menu,
    parse_result_index,
)
from tests.conftest import load_fixture

NAKAYAMA = Meeting(
    cname="pw01ses01062026040620260920/2C",
    date="2026-09-20", venue_code="06", kaiji=4, nichime=6, venue_name="中山",
)
HANSHIN = Meeting(
    cname="pw01ses10092026040520260919/B1",
    date="2026-09-19", venue_code="09", kaiji=4, nichime=5, venue_name="阪神",
)


def index_html():
    return load_fixture("jra_result_index_20260920.html")


def nakayama_html():
    return load_fixture("jra_meeting_20260920_nakayama.html")


def test_find_index_cname():
    """トップページの「レース結果」への入口（pw01sli…）を拾う。"""
    html = """<a href="#" onClick="doAction('/JRADB/accessD.html','pw01dli00/F3');">出馬表</a>
              <a href="#" onClick="doAction('/JRADB/accessS.html','pw01sli00/AF');">レース結果</a>"""
    assert find_index_cname(html) == "pw01sli00/AF"
    assert find_index_cname("<a href='#'>リンクなし</a>") is None


def test_parse_result_index_reads_each_meeting():
    """開催選択ページから、開催ごとの日付・場・回・日目が取れる。"""
    meetings = parse_result_index(index_html())
    this_week = [m for m in meetings if m.date in {"2026-09-19", "2026-09-20"}]
    assert [(m.date, m.venue_code, m.venue_name, m.kaiji, m.nichime) for m in this_week] == [
        ("2026-09-19", "06", "中山", 4, 5),
        ("2026-09-19", "09", "阪神", 4, 5),
        ("2026-09-20", "06", "中山", 4, 6),
        ("2026-09-20", "09", "阪神", 4, 6),
    ]
    # cnameは末尾2文字がチェックサムで組み立てられないので、そのまま持ち回る
    assert this_week[0].cname == "pw01srl10062026040520260919/CA"
    assert this_week[0].key == "2026-09-19_06"


def test_parse_result_index_without_links():
    with pytest.raises(LayoutError, match="開催"):
        parse_result_index("<html><body>ただいま準備中です</body></html>")


def test_parse_race_menu_finds_the_all_races_page():
    cname = parse_race_menu(load_fixture("jra_race_menu_20260920_nakayama.html"))
    assert cname == "pw01ses01062026040620260920/2C"
    assert parse_race_menu("<html></html>") is None


def test_parse_meeting_results_reads_race_and_laps():
    races = parse_meeting_results(nakayama_html(), NAKAYAMA)
    assert [r.race["race_no"] for r in races] == [1, 2, 11]
    assert all(r.warnings == [] for r in races)

    first = races[0].race
    assert set(RACE_COLUMNS) <= set(first)  # そのままDBに入れられるキーがそろっている
    assert first == {
        "race_id": "202606040601", "race_date": "2026-09-20", "venue_code": "06",
        "kaiji": 4, "nichime": 6, "race_no": 1, "race_name": "2歳未勝利", "grade": None,
        "post_time": "10:00", "age_condition": "2歳", "class_condition": "未勝利",
        "race_conditions": "混,指,馬齢", "condition_raw": "2歳 未勝利 （混合）［指定］ 馬齢",
        "n_runners": 15, "weather": "雨", "going_turf": None, "going_dirt": "不良",
        "surface": "dirt", "direction": "right", "distance_m": 1800, "course_detail": None,
        "winner_corner": "2-2-2-1",
    }

    laps = races[0].laps
    assert len(laps) == 9  # 1800m = 200m × 9
    assert laps[0] == {"race_id": "202606040601", "seq": 1, "distance_m": 200, "lap_sec": 12.7}
    assert [lap["distance_m"] for lap in laps] == [200 * i for i in range(1, 10)]


def test_graded_turf_race():
    """芝の重賞: グレード（GⅡ→GII）・外回り・芝の馬場状態が取れる。"""
    race = [r for r in parse_meeting_results(nakayama_html(), NAKAYAMA) if r.race["race_no"] == 11][0]
    assert race.race["grade"] == "GII"
    assert race.race["race_name"] == "産経賞オールカマー"
    assert (race.race["surface"], race.race["course_detail"]) == ("turf", "外")
    assert (race.race["going_turf"], race.race["going_dirt"]) == ("重", None)
    assert race.race["class_condition"] == "オープン"
    assert len(race.laps) == 11  # 2200m


def test_fillies_only_race_keeps_the_netkeiba_wording():
    """「牝」は括弧の外なので、netkeibaと同じく class_condition 側に付く。"""
    race = [r for r in parse_meeting_results(nakayama_html(), NAKAYAMA) if r.race["race_no"] == 2][0]
    assert race.race["class_condition"] == "未勝利 牝"
    assert race.race["race_conditions"] == "指,馬齢"


def test_jump_race_has_no_laps_and_no_warning():
    """障害レースはハロンタイムが無い。これは正常なので警告にしない。"""
    races = parse_meeting_results(load_fixture("jra_meeting_20260919_hanshin.html"), HANSHIN)
    jump = [r for r in races if r.race["race_no"] == 4][0]
    assert jump.race["surface"] == "jump"
    assert jump.race["grade"] == "JGIII"
    assert jump.race["age_condition"] == "3歳以上"  # 「障害3歳以上」から年齢だけ取る
    assert jump.race["distance_m"] == 3140
    assert jump.laps == []
    assert jump.warnings == []


def test_race_without_a_result_is_skipped():
    """まだ行われていないレース（着順が無い）は返さない。"""
    html = nakayama_html().replace('class="place"', 'class="place_not_yet"')
    assert parse_meeting_results(html, NAKAYAMA) == []


def test_missing_lap_table_is_a_warning():
    html = nakayama_html().replace("ハロンタイム", "ハロン時計")
    races = parse_meeting_results(html, NAKAYAMA)
    assert races[0].laps == []
    assert any("ハロンタイム" in w for w in races[0].warnings)


def test_layout_change_is_reported():
    html = nakayama_html().replace('class="race_result_unit"', 'class="race_result_block"')
    with pytest.raises(LayoutError, match="race_result_unit"):
        parse_meeting_results(html, NAKAYAMA)


def test_page_that_disagrees_with_the_link_is_warned():
    """ページの開催（4回中山6日）とリンクの開催がずれたら警告する。"""
    other = Meeting(NAKAYAMA.cname, "2026-09-19", "06", 4, 5, "中山")
    races = parse_meeting_results(nakayama_html(), other)
    assert any("日付" in w for w in races[0].warnings)
    assert any("開催回" in w for w in races[0].warnings)


# --- レースごとの結果ページへのリンク（馬柱の映像リンク用） ---------------------


def test_parse_race_links_from_this_weeks_menu():
    """今週の開催は doAction 形式。12レース分のトークンが race_id と対応づく。"""
    links = parse_race_links(load_fixture("jra_race_menu_20260920_nakayama.html"), NAKAYAMA)
    assert len(links) == 12
    assert links["202606040601"] == "pw01sde0106202604060120260920/40"
    assert links["202606040611"] == "pw01sde0106202604061120260920/92"


def test_parse_race_links_from_a_past_meeting():
    """過去の開催は href="…?CNAME=…" 形式（GETでそのまま開ける）。"""
    html = (
        '<a href="/JRADB/accessS.html?CNAME=pw01sde1004202602030120260801/5E">1R</a>'
        '<a href="/JRADB/accessS.html?CNAME=pw01sde1004202602030220260801/13">2R</a>'
    )
    meeting = Meeting("x", "2026-08-01", "04", 2, 3, "新潟")
    assert parse_race_links(html, meeting) == {
        "202604020301": "pw01sde1004202602030120260801/5E",
        "202604020302": "pw01sde1004202602030220260801/13",
    }


def test_parse_race_links_drops_other_meetings():
    """別の開催のトークンが混じっていても取り違えない。"""
    html = '<a href="?CNAME=pw01sde1004202602030120260801/5E">1R</a>'
    assert parse_race_links(html, NAKAYAMA) == {}       # 中山6日目のページではない
    assert parse_race_links("<html></html>", NAKAYAMA) == {}


def test_find_past_search_cname():
    html = """<a href="#" onclick="doAction('/JRADB/accessS.html', 'pw01skl00999999/B3');">過去のレース結果</a>"""
    assert find_past_search_cname(html) == "pw01skl00999999/B3"
    assert find_past_search_cname("<a href='#'>なし</a>") is None


def test_parse_month_links_uses_the_tokens_on_the_page():
    """過去レース結果検索ページが持っている月ごとのトークン表を読む。"""
    html = """
    <script>
    objParam["2609"]="54";objParam["2608"]="6D";objParam["2301"]="27";objParam["2699"]="FF";
    var yearMonth = "202609";
    </script>
    """
    links = parse_month_links(html)
    assert links["2026-09"] == "pw01skl00202609/54"   # 当月以降は pw01skl00
    assert links["2026-08"] == "pw01skl10202608/6D"   # それより前は pw01skl10
    assert links["2023-01"] == "pw01skl10202301/27"
    assert "2026-99" not in str(links)                # 月として有り得ない値は捨てる
    assert parse_month_links("<html></html>") == {}


# --- 出馬表（血統・馬主・生産牧場） -------------------------------------------


def shutuba_html():
    return load_fixture("jra_shutuba_202606040601.html")


def test_parse_shutuba_profiles():
    """JRA公式の出馬表から、netkeibaには無い血統・馬主・生産牧場を読む。"""
    profiles = parse_shutuba_profiles(shutuba_html())
    assert len(profiles) == 15
    first = profiles[0]
    assert (first.umaban, first.horse_name) == (1, "イカルステソーロ")
    assert (first.sire, first.dam, first.broodmare_sire) == (
        "フィエールマン", "ライトファンタスティック", "Acclamation",
    )
    assert first.owner == "了德寺健二ホールディングス(株)"
    assert first.breeder == "リョーケンファーム(株)"
    # 外国産馬（父・母・母父が英字）も読める
    foreign = profiles[1]
    assert (foreign.sire, foreign.broodmare_sire) == ("Drain the Clock", "Macho Uno")
    assert [p.umaban for p in profiles] == list(range(1, 16))


def test_parse_shutuba_profiles_reads_the_horse_weight():
    """馬体重は当日発表。JRA公式の出馬表にしか無いので、ここから読む。"""
    profiles = parse_shutuba_profiles(shutuba_html())
    assert [(p.horse_weight, p.weight_diff) for p in profiles[:4]] == [
        (480, -2), (480, -2), (470, -10), (478, 12),
    ]
    # 斤量（55.0）やレース表題の「馬齢」を体重と間違えていないこと
    assert all(400 <= p.horse_weight <= 600 for p in profiles)


def test_parse_shutuba_profiles_before_the_weights_are_published():
    """発表前は馬体重の欄そのものが無い（代わりに戦績と総賞金が出る）。

    そのときは None＝取れないのが正常。戦績「(0.0.0.0)」を拾ってしまわないこと。
    """
    profiles = parse_shutuba_profiles(load_fixture("jra_shutuba_before_weight.html"))
    assert len(profiles) == 14
    assert all(p.horse_weight is None and p.weight_diff is None for p in profiles)
    assert profiles[0].sire and profiles[0].owner      # 血統・馬主はいつでも読める


def test_parse_shutuba_profiles_without_a_broodmare_sire():
    """母の父が書かれていない馬でも落ちない。"""
    html = shutuba_html().replace('<span class="bloodmare">', '<span class="other">')
    profiles = parse_shutuba_profiles(html)
    assert profiles[0].broodmare_sire is None
    assert profiles[0].dam == "ライトファンタスティック"    # 母は読める


def test_parse_shutuba_profiles_layout_change():
    html = shutuba_html().replace('class="horse"', 'class="horse_cell"')
    with pytest.raises(LayoutError, match="出馬表"):
        parse_shutuba_profiles(html)


def test_entry_index_and_race_links():
    """出馬表側の開催選択・レース選択も、結果と同じ仕組みで読める。"""
    html = """<a href="#" onClick="doAction('/JRADB/accessD.html','pw01dli00/F3');">出馬表</a>"""
    assert find_index_cname(html, marker="dli") == "pw01dli00/F3"

    menu = '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604060120260920/84">1R</a>'
    meeting = Meeting("x", "2026-09-20", "06", 4, 6, "中山")
    assert parse_race_links(menu, meeting, kind="dde") == {
        "202606040601": "pw01dde0106202604060120260920/84",
    }


def test_winner_corner_is_read_for_every_race():
    """勝ち馬のコーナー通過順位（開催の勝ちタイム一覧で脚質を出すのに使う）。

    馬ごとの結果はDBに入れない方針なので、勝ち馬の通過順位だけをレースの列として持つ。
    """
    races = parse_meeting_results(
        load_fixture("jra_meeting_20260920_nakayama.html"), MEETING
    )
    corners = [r.race["winner_corner"] for r in races]
    assert all(c is None or re.fullmatch(r"\d+(-\d+)*", c) for c in corners), corners
    assert sum(c is not None for c in corners) >= len(races) - 1   # ほぼ全レースで読める


def test_a_race_without_a_result_row_has_no_winner_corner():
    """着順1の行が無い（まだ確定していない）ブロックでも落ちない。"""
    from bs4 import BeautifulSoup

    from keiba_data.scrapers.jra_result import _winner_corner

    unit = BeautifulSoup("<div><tr><td class='place'>取消</td></tr></div>", "lxml")
    assert _winner_corner(unit) is None
    assert _winner_corner(BeautifulSoup("<div></div>", "lxml")) is None


def test_winner_corner_is_read_for_every_race():
    """勝ち馬のコーナー通過順位（開催の勝ちタイム一覧で脚質を出すのに使う）。

    馬ごとの結果はDBに入れない方針なので、勝ち馬の通過順位だけをレースの列として持つ。
    """
    races = parse_meeting_results(nakayama_html(), NAKAYAMA)
    corners = [r.race["winner_corner"] for r in races]
    assert all(c is None or re.fullmatch(r"\d+(-\d+)*", c) for c in corners), corners
    assert all(c is not None for c in corners)


def test_a_race_without_a_result_row_has_no_winner_corner():
    """着順1の行が無い（取消ばかり・まだ確定していない）ブロックでも落ちない。"""
    from bs4 import BeautifulSoup

    from keiba_data.scrapers.jra_result import _winner_corner

    unit = BeautifulSoup("<div><tr><td class='place'>取消</td></tr></div>", "lxml")
    assert _winner_corner(unit) is None
    assert _winner_corner(BeautifulSoup("<div></div>", "lxml")) is None
