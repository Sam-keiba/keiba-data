"""JRAの馬場情報ページのパーサ（scrapers/baba.py）。"""

from datetime import date

import pytest

from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.baba import latest_by_day, parse_cushion, parse_moisture
from tests.conftest import load_fixture

TODAY = date(2026, 9, 20)
CUSHION_HTML = load_fixture("baba_cushion_20260920.html")
MOISTURE_HTML = load_fixture("baba_moist_20260920.html")


def test_parse_cushion_reads_each_venue_and_time():
    measurements = parse_cushion(CUSHION_HTML, TODAY)
    today = {m.venue_name: m for m in measurements if m.date == "2026-09-20"}
    assert today["中山"].cushion_value == 10.0
    assert today["中山"].measured_at == "2026-09-20 07:00"
    assert today["阪神"].cushion_value == 8.6
    assert today["阪神"].measured_at == "2026-09-20 07:30"
    # 開催前日（金曜）の測定も含まれる
    assert any(m.date == "2026-09-18" for m in measurements)


def test_parse_moisture_assigns_turf_and_dirt():
    measurements = parse_moisture(MOISTURE_HTML, TODAY)
    nakayama = next(m for m in measurements if m.venue_name == "中山" and m.date == "2026-09-20")
    assert (nakayama.turf_moisture_goal, nakayama.turf_moisture_4corner) == (13.2, 13.1)
    assert (nakayama.dirt_moisture_goal, nakayama.dirt_moisture_4corner) == (6.3, 5.6)
    assert nakayama.cushion_value is None        # このページにクッション値は無い


def test_year_is_inferred_from_the_reference_date():
    """表記に年が無いので基準日から決める。基準日より大きく先の日付は前年とみなす。"""
    # 基準日が1月なら、9月の表記は前年
    january = parse_cushion(CUSHION_HTML, date(2027, 1, 10))
    assert any(m.date.startswith("2026-09") for m in january)
    # 基準日が同じ月なら当年
    assert any(m.date == "2026-09-20" for m in parse_cushion(CUSHION_HTML, TODAY))


def test_latest_measurement_of_the_day_wins():
    measurements = parse_cushion(CUSHION_HTML, TODAY)
    latest = latest_by_day(measurements)
    assert latest[("中山", "2026-09-20")].cushion_value == 10.0
    assert len({k[1] for k in latest}) >= 9        # 直近3開催ぶんが入っている


def test_layout_change_is_reported():
    with pytest.raises(LayoutError):
        parse_cushion("<html><body>お知らせ</body></html>", TODAY)
    with pytest.raises(LayoutError):
        parse_moisture("<html><body>お知らせ</body></html>", TODAY)
    # 競馬場のブロックはあるが値が無い（ページの作りが変わった場合）
    with pytest.raises(LayoutError):
        parse_cushion('<div id="cushion_data_list"><div title="中山"></div></div>', TODAY)
