import pytest

from keiba_data import parsing
from keiba_data.race_id import compose_race_id, decode_race_id, is_jra_race_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2:14.0", 134.0), ("58.3", 58.3), ("1:12.6", 72.6), ("", None), (None, None), ("abc", None)],
)
def test_parse_time_to_seconds(text, expected):
    assert parsing.parse_time_to_seconds(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("508(+2)", (508, 2)), ("466(-12)", (466, -12)), ("480(0)", (480, 0)), ("計不", (None, None)), ("", (None, None))],
)
def test_parse_weight(text, expected):
    assert parsing.parse_weight(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # JRA公式の出馬表は「504kg (+6)」と書く（netkeibaは「504(+6)」）
        ("504kg (+6)", (504, 6)),
        ("462kg (-10)", (462, -10)),
        ("462kg (0)", (462, 0)),
        ("456kg (初出走)", (456, None)),   # 増減が無いだけ。増減0とは区別する
        ("480kg", (480, None)),
        ("504kg（+6）", (504, 6)),         # 全角括弧でも読む
        ("(2.1.0.11)", (None, None)),      # 発表前に出ている戦績を体重と間違えない
        ("1,541万円", (None, None)),       # 総賞金も同様
    ],
)
def test_parse_weight_of_the_jra_style(text, expected):
    assert parsing.parse_weight(text) == expected


def test_parse_sex_age():
    assert parsing.parse_sex_age("牡3") == ("牡", 3)
    assert parsing.parse_sex_age("セ7") == ("セ", 7)
    assert parsing.parse_sex_age("?") == (None, None)


def test_numbers_with_commas_and_placeholders():
    assert parsing.to_int("284,140") == 284140
    assert parsing.to_float("5,513.4") == 5513.4
    assert parsing.to_float("---") is None
    assert parsing.to_int(" ") is None


def test_decode_race_id():
    info = decode_race_id("202606040411")
    assert (info.year, info.venue_code, info.venue_name, info.kaiji, info.nichime, info.race_no) == (
        2026, "06", "中山", 4, 4, 11,
    )


def test_compose_race_id_round_trip():
    """JRA公式のページにはrace_idが無いので、開催情報から組み立てて突き合わせる。"""
    assert compose_race_id(2026, "06", 4, 5, 1) == "202606040501"
    assert compose_race_id(2026, "09", 4, 6, 12) == "202609040612"
    info = decode_race_id("202606040411")
    assert compose_race_id(
        info.year, info.venue_code, info.kaiji, info.nichime, info.race_no
    ) == "202606040411"
    with pytest.raises(ValueError):
        compose_race_id(2026, "44", 4, 5, 1)  # 地方競馬の場コード
    with pytest.raises(ValueError):
        compose_race_id(2026, "06", 4, 5, 13)  # 13Rは無い


def test_lap_rows_uses_cumulative_distance():
    """ラップの距離はスタートからの累計。先頭区間だけ端数になる（2500mなら100m）。"""
    warnings = []
    rows = parsing.lap_rows("202606040411", 2500, [7.0] + [12.0] * 12, warnings)
    assert [r["distance_m"] for r in rows][:3] == [100, 300, 500]
    assert rows[-1]["distance_m"] == 2500
    assert warnings == []
    short = parsing.lap_rows("x", 1200, [12.0] * 5, warnings)  # 本数が足りない
    assert len(short) == 5 and any("ラップ数" in w for w in warnings)


def test_decode_race_id_rejects_non_jra():
    with pytest.raises(ValueError):
        decode_race_id("202644091211")  # 地方競馬の場コード
    with pytest.raises(ValueError):
        decode_race_id("2026")
    assert is_jra_race_id("202609040401")
    assert not is_jra_race_id("202644091211")
