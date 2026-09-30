from datetime import date

import pytest

from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.calendar import parse_calendar
from keiba_data.scrapers.race_list import parse_race_list, parse_race_list_details
from keiba_data.scrapers.race_result import parse_race_result
from keiba_data.scrapers import shutuba
from keiba_data.scrapers.shutuba import parse_shutuba
from tests.conftest import load_fixture


def test_parse_calendar():
    days = parse_calendar(load_fixture("calendar_202609.html"))
    assert days == [
        date(2026, 9, d) for d in (5, 6, 12, 13, 19, 20, 21, 27)
    ]


def test_parse_calendar_layout_change():
    with pytest.raises(LayoutError):
        parse_calendar("<html><body><table><tr><td>1</td></tr></table></body></html>")


def test_parse_race_list():
    race_ids = parse_race_list(load_fixture("race_list_20260913.html"))
    assert len(race_ids) == 24
    assert race_ids[0] == "202606040401"
    assert race_ids[-1] == "202609040412"


def test_parse_race_list_empty():
    assert parse_race_list("<html><body></body></html>") == []


def test_parse_graded_race():
    page = parse_race_result(load_fixture("result_202606040411_g2.html"), "202606040411")
    assert page is not None
    assert page.warnings == []
    race = page.race
    assert race["race_name"] == "第80回朝日セントライト記念"
    assert race["grade"] == "GII"
    assert race["race_date"] == "2026-09-13"
    assert (race["surface"], race["direction"], race["distance_m"], race["course_detail"]) == ("turf", "right", 2200, "外")
    assert (race["weather"], race["going_turf"], race["going_dirt"], race["post_time"]) == ("曇", "稍重", None, "15:45")
    assert (race["age_condition"], race["class_condition"], race["race_conditions"]) == ("3歳", "オープン", "国際,指,馬齢")
    assert race["n_runners"] == len(page.entries) == 16

    winner_entry = page.entries[0]
    assert winner_entry == {
        "race_id": "202606040411", "umaban": 7, "waku": 4,
        "horse_id": "2023106924", "horse_name": "ジャスティンシカゴ", "sex": "牡", "age": 3, "kinryo": 57.0,
        "jockey_id": "01184", "jockey_name": "原優介",
        "trainer_id": "01175", "trainer_name": "宮田敬介", "trainer_stable": "美浦",
        "owner_id": "195031", "owner_name": "三木正浩",
        "horse_weight": 508, "weight_diff": 2,
    }
    winner = page.results[0]
    assert (winner["finish_position"], winner["time_sec"], winner["corner_passing"], winner["last_3f"]) == (1, 134.0, "2-3-4-3", 34.2)
    assert (winner["win_odds"], winner["popularity"], winner["prize_man_yen"]) == (50.6, 12, 5513.4)

    payouts = {(p["bet_type"], p["combination"]): (p["payout_yen"], p["popularity"]) for p in page.payouts}
    assert payouts[("単勝", "7")] == (5060, 12)
    assert payouts[("三連単", "7-6-2")] == (284140, 741)
    assert len([k for k in payouts if k[0] == "ワイド"]) == 3

    assert len(page.laps) == 11
    assert page.laps[0] == {"race_id": "202606040411", "seq": 1, "distance_m": 200, "lap_sec": 12.3}
    assert page.laps[-1]["distance_m"] == 2200
    assert round(sum(lap["lap_sec"] for lap in page.laps), 1) == winner["time_sec"]


def test_parse_2023_race():
    page = parse_race_result(load_fixture("result_202306010111.html"), "202306010111")
    assert page.warnings == []
    assert page.race["grade"] == "GIII"
    assert page.race["race_conditions"] == "国際,特指,ハンデ"
    assert page.entries[0]["jockey_name"] == "戸崎圭太"


@pytest.mark.parametrize(
    ("fixture", "race_id", "status"),
    [
        ("result_202406010103_torikeshi.html", "202406010103", "取消"),
        ("result_202406010410_jogai.html", "202406010410", "除外"),
        ("result_202406010201_chushi.html", "202406010201", "中止"),
    ],
)
def test_parse_non_finishers(fixture, race_id, status):
    page = parse_race_result(load_fixture(fixture), race_id)
    assert page.warnings == []
    special = [r for r in page.results if r["finish_status"] == status]
    assert len(special) == 1
    assert special[0]["finish_position"] is None
    if status in ("取消", "除外"):
        assert page.race["n_runners"] == len(page.results) - 1
        assert special[0]["win_odds"] is None
    else:
        assert page.race["n_runners"] == len(page.results)


def test_parse_dead_heat():
    page = parse_race_result(load_fixture("result_202406010209_dead_heat.html"), "202406010209")
    dead_heat = [r for r in page.results if r["margin"] == "同着"]
    assert dead_heat
    positions = [r["finish_position"] for r in page.results]
    assert positions.count(dead_heat[0]["finish_position"]) == 2


def test_parse_jump_race():
    page = parse_race_result(load_fixture("result_202406010204_jump.html"), "202406010204")
    assert page.warnings == []
    race = page.race
    assert (race["surface"], race["distance_m"], race["course_detail"]) == ("jump", 2880, "芝 ダート")
    assert (race["age_condition"], race["class_condition"]) == ("4歳以上", "未勝利")
    assert page.laps == []


def test_parse_page_without_result_returns_none():
    assert parse_race_result("<html><body><p>該当するデータはありません</p></body></html>", "202606040411") is None


def test_missing_column_raises_layout_error():
    html = load_fixture("result_202606040411_g2.html").replace(">調教師<", ">トレーナー<")
    with pytest.raises(LayoutError, match="調教師"):
        parse_race_result(html, "202606040411")


def test_changed_meta_produces_warnings():
    html = load_fixture("result_202606040411_g2.html").replace('class="smalltxt"', 'class="small_text"')
    page = parse_race_result(html, "202606040411")
    assert any("smalltxt" in w for w in page.warnings)
    assert page.race["race_date"] is None


# --- 出馬表（未開催レース） ---------------------------------------------------


def test_parse_shutuba():
    page = parse_shutuba(load_fixture("shutuba_202606040611.html"), "202606040611", "2026-09-20")
    assert page is not None
    assert page.warnings == []
    race = page.race
    assert race["race_name"] == "産経賞オールカマー"
    assert race["grade"] == "GII"
    assert race["race_date"] == "2026-09-20"
    assert (race["surface"], race["direction"], race["distance_m"]) == ("turf", "right", 2200)
    assert race["course_detail"] == "外 C"
    assert race["post_time"] == "15:45"
    assert (race["age_condition"], race["class_condition"]) == ("3歳以上", "オープン")
    assert race["race_conditions"] == "国際,指,別定"
    assert race["n_entries"] == 13

    assert len(page.entries) == 13
    assert page.entries[0] == {
        "race_id": "202606040611", "seq": 1, "umaban": 1, "waku": 1,
        "horse_id": "2021102800", "horse_name": "キャントウェイト", "sex": "牡", "age": 5,
        "kinryo": 57.0, "jockey_id": "00660", "jockey_name": "横山 典弘",
        "trainer_id": "01024", "trainer_name": "萱野 浩二", "stable": "美浦",
        "horse_weight": None, "weight_diff": None, "status": None,  # 馬体重は当日発表
    }
    assert [e["umaban"] for e in page.entries] == list(range(1, 14))
    assert page.entries[-1]["jockey_name"] == "武 豊"


def test_parse_shutuba_ignores_ai_lap_table():
    """同じページにある「AI予測ラップ」の表（同じShutuba_Tableクラス）を拾わないこと。"""
    html = load_fixture("shutuba_202606040611.html")
    assert html.count("Shutuba_Table") >= 2
    page = parse_shutuba(html, "202606040611", "2026-09-20")
    assert all(e["horse_name"] for e in page.entries)


def test_parse_shutuba_not_published_returns_none():
    assert parse_shutuba("<html><body><p>準備中</p></body></html>", "202606040611") is None


def test_parse_shutuba_missing_race_name_raises():
    html = load_fixture("shutuba_202606040611.html").replace('class="RaceName"', 'class="RaceTitle"')
    with pytest.raises(LayoutError):
        parse_shutuba(html, "202606040611", "2026-09-20")


def test_parse_shutuba_falls_back_to_title_date():
    page = parse_shutuba(load_fixture("shutuba_202606040611.html"), "202606040611")
    assert page.race["race_date"] == "2026-09-20"


def test_parse_race_list_details_upcoming_day():
    races = parse_race_list_details(load_fixture("race_list_20260920.html"))
    assert len(races) == 24
    assert races[0] == {
        "race_id": "202606040601", "race_no": 1, "race_name": "2歳未勝利",
        "post_time": "10:00", "course_text": "ダ1800m", "n_entries": 15,
    }
    all_kamma = next(r for r in races if r["race_id"] == "202606040611")
    assert (all_kamma["race_name"], all_kamma["post_time"], all_kamma["n_entries"]) == ("オールカマー", "15:45", 13)


def test_parse_race_list_details_on_finished_day():
    """結果が出た日の一覧（リンク先がresult.html）でも同じように取れること。"""
    races = parse_race_list_details(load_fixture("race_list_20260913.html"))
    assert len(races) == 24
    assert races[0]["race_no"] == 1


def test_parse_shutuba_before_draw():
    """枠順確定前（出走登録の段階）は枠番・馬番が空欄だが、出走予定馬は取得できる。"""
    page = parse_shutuba(load_fixture("shutuba_202609040703_before_draw.html"), "202609040703", "2026-09-21")
    assert page.warnings == []
    assert page.race["race_name"] == "2歳未勝利"
    assert (page.race["surface"], page.race["distance_m"], page.race["post_time"]) == ("turf", 1200, "11:05")
    assert len(page.entries) == 13

    first = page.entries[0]
    assert (first["seq"], first["umaban"], first["waku"]) == (1, None, None)
    assert (first["horse_name"], first["horse_id"]) == ("アトランタ", "2024106380")
    assert (first["sex"], first["age"], first["kinryo"]) == ("牡", 2, 55.0)
    assert (first["jockey_name"], first["jockey_id"]) == ("松山", "01126")
    assert (first["trainer_name"], first["stable"]) == ("高柳大", "栗東")
    assert [e["seq"] for e in page.entries] == list(range(1, 14))


def test_parse_shutuba_jump_race_is_not_dirt():
    """中山の障害レースは出馬表で「ダ2880m」と書かれるが、障害として扱う。"""
    page = parse_shutuba(load_fixture("shutuba_202606040701_jump.html"), "202606040701", "2026-09-21")
    assert page.race["race_name"] == "3歳以上障害未勝利"
    assert page.race["surface"] == "jump"
    assert page.race["distance_m"] == 2880
    assert page.race["direction"] == "right"


def test_shutuba_fills_the_condition_from_the_race_name():
    """枠順確定前は `.RaceData02` の中身が空で、条件が取れないことがある。

    `3歳以上1勝クラス` のようにレース名がそのまま条件のレースなら、そこから補う。
    """
    race = {"race_name": "3歳以上1勝クラス", "class_condition": None, "age_condition": None}
    shutuba._fill_condition_from_name(race)
    assert (race["class_condition"], race["age_condition"]) == ("1勝クラス", "3歳以上")

    zenkaku = {"race_name": "２歳新馬", "class_condition": None, "age_condition": None}
    shutuba._fill_condition_from_name(zenkaku)
    assert (zenkaku["class_condition"], zenkaku["age_condition"]) == ("新馬", "2歳")


def test_shutuba_keeps_the_values_read_from_the_page():
    """ページから読めた値のほうが正確なので、上書きしない。"""
    race = {"race_name": "3歳以上1勝クラス", "class_condition": "オープン", "age_condition": "3歳"}
    shutuba._fill_condition_from_name(race)
    assert (race["class_condition"], race["age_condition"]) == ("オープン", "3歳")


def test_shutuba_leaves_a_named_race_alone():
    race = {"race_name": "サフラン賞", "class_condition": None, "age_condition": None}
    shutuba._fill_condition_from_name(race)
    assert race["class_condition"] is None and race["age_condition"] is None
