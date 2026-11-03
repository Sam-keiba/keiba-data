from datetime import date, datetime

from keiba_data.auto import due_reason

SAT, SUN = date(2026, 11, 7), date(2026, 11, 8)
WEEKEND = [SAT, SUN]


def at(day, hour):
    return datetime(2026, 11, day, hour)


def test_friday_evening_fetches_saturday_entries():
    assert due_reason(at(6, 18), WEEKEND, [], at(3, 18)) == "出馬表"


def test_friday_morning_is_too_early_for_entries():
    assert due_reason(at(6, 9), WEEKEND, [], at(3, 18)) is None


def test_second_run_after_the_same_cutoff_is_skipped():
    assert due_reason(at(6, 21), WEEKEND, [], at(6, 18)) is None


def test_saturday_evening_fetches_sunday_entries_even_after_a_morning_catch_up():
    assert due_reason(at(7, 9), WEEKEND, [], at(3, 18)) == "出馬表"   # 金曜を逃して土曜の朝に開いた
    assert due_reason(at(7, 18), WEEKEND, [], at(7, 9)) == "出馬表"   # 夕方は日曜の分


def test_sunday_evening_needs_nothing():
    assert due_reason(at(8, 18), WEEKEND, [SAT, SUN], at(7, 18)) is None


def test_monday_is_too_early_for_sunday_results():
    assert due_reason(at(9, 18), WEEKEND, [SUN], at(7, 18)) is None


def test_tuesday_fetches_sunday_results():
    assert due_reason(at(10, 18), WEEKEND, [SUN], at(7, 18)) == "2026-11-08 の結果"


def test_results_are_tried_once_a_day():
    assert due_reason(at(10, 21), WEEKEND, [SUN], at(10, 18)) is None
    assert due_reason(at(11, 18), WEEKEND, [SUN], at(10, 18)) is not None


def test_a_missed_tuesday_is_caught_up_later():
    assert due_reason(at(13, 8), WEEKEND, [SUN], at(7, 18)) is not None


def test_monday_holiday_keeps_entries_going_and_delays_results():
    holiday = [SAT, SUN, date(2026, 11, 9)]
    assert due_reason(at(8, 18), holiday, [], at(7, 18)) == "出馬表"


def test_a_day_that_never_completes_stops_triggering():
    assert due_reason(at(30, 18), WEEKEND, [SUN], at(29, 18)) is None


def test_first_run_with_no_history():
    assert due_reason(at(10, 18), WEEKEND, [SUN], None) is not None
