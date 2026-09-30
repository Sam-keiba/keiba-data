"""JRA公式のオッズページのパーサ（単勝〜3連単）。

フィクスチャは実物のHTML（2026-09-26 中山8日目1R＝13頭立て・最終オッズ、
および 2026-09-27 中山9日目11R＝16頭立て・発走前）。
"""

import pathlib
from itertools import combinations, permutations

import pytest

from keiba_data.scrapers import LayoutError, jra_odds

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
RACE_ID = "202606040801"  # 2026 中山 4回8日目 1R（13頭立て）
BEFORE_RACE_ID = "202606040911"  # 2026 中山 4回9日目 11R（16頭立て・発走前）
N = 13  # 1Rの出走頭数
N_FRAMES = 8


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def page(bet_type: str, fixture: str | None = None):
    return jra_odds.parse_odds(load(f"jra_odds_{fixture or bet_type}_{RACE_ID}.html"), bet_type, RACE_ID)


def odds_of(result, combo: str):
    return next(row for row in result.rows if row["combo"] == combo)


# --- 券種の表 ---------------------------------------------------------------------------


def test_every_bet_type_has_one_place_to_describe_it():
    """券種ごとの違い（頭数・着順を見るか・幅か）は BET_TYPES だけに書く。"""
    assert [b.key for b in jra_odds.BET_TYPES] == [
        "tansho", "fukusho", "wakuren", "umaren", "wide", "umatan", "sanrenpuku", "sanrentan",
    ]
    assert [b.label for b in jra_odds.BET_TYPES] == [
        "単勝", "複勝", "枠連", "馬連", "ワイド", "馬単", "3連複", "3連単",
    ]
    ordered = {b.key for b in jra_odds.BET_TYPES if b.ordered}
    assert ordered == {"umatan", "sanrentan"}          # 着順を見るのはこの2つだけ
    ranged = {b.key for b in jra_odds.BET_TYPES if b.ranged}
    assert ranged == {"fukusho", "wide"}               # 幅で出るのはこの2つだけ


def test_tansho_and_fukusho_come_from_the_same_page():
    """単勝と複勝は1ページで両方取れるので、リクエストは1回で済む。"""
    assert jra_odds.BET_TYPES_BY_DIGIT["1"] == ("tansho", "fukusho")
    assert jra_odds.BET_TYPES_BY_DIGIT["8"] == ("sanrentan",)


# --- リンク集め -------------------------------------------------------------------------


def test_the_race_menu_holds_every_bet_type_of_every_race():
    links = jra_odds.parse_race_links(load("jra_odds_menu_20260926_nakayama.html"))
    assert len(links) == 12                                  # 1R〜12R
    assert links[RACE_ID]["umaren"] == "pw154ouS306202604080120260926Z/07"
    # 3連複だけトークンの末尾に `99` が付く
    assert links[RACE_ID]["sanrenpuku"].endswith("Z99/F5")
    # 単勝と複勝は同じトークン
    assert links[RACE_ID]["tansho"] == links[RACE_ID]["fukusho"]


def test_a_race_without_a_bet_type_on_sale_simply_lacks_it():
    """少頭数のレースは枠連が発売されない。欠けても壊れないこと。"""
    links = jra_odds.parse_race_links(load("jra_odds_menu_20260926_nakayama.html"))
    assert "wakuren" not in links["202606040804"]
    assert "umaren" in links["202606040804"]


def test_links_of_another_meeting_are_thrown_away():
    """取り違え防止。開催が食い違うトークンは捨てる。"""
    from keiba_data.scrapers.jra_result import Meeting

    html = load("jra_odds_menu_20260926_nakayama.html")
    other = Meeting("x", "2026-09-27", "09", 4, 8)          # 場も日付も違う
    assert jra_odds.parse_race_links(html, other) == {}
    same = Meeting("x", "2026-09-26", "06", 4, 8)
    assert len(jra_odds.parse_race_links(html, same)) == 12


def test_each_odds_page_carries_the_links_of_the_other_bet_types():
    """いちど入口を手に入れれば、券種の切り替えは1リクエストで済む。"""
    result = page("umaren")
    assert sorted(result.links) == sorted(b.key for b in jra_odds.BET_TYPES)
    assert result.links["sanrentan"].startswith("pw158ou")


# --- 点数（理論値と合うこと） -------------------------------------------------------------


@pytest.mark.parametrize(
    ("bet_type", "fixture", "expected"),
    [
        ("tansho", None, N),
        ("fukusho", "tansho", N),
        ("umaren", None, len(list(combinations(range(N), 2)))),            # 78
        ("wide", None, len(list(combinations(range(N), 2)))),              # 78
        ("umatan", None, len(list(permutations(range(N), 2)))),            # 156
        ("sanrenpuku", None, len(list(combinations(range(N), 3)))),        # 286
        ("sanrentan", None, len(list(permutations(range(N), 3)))),         # 1716
    ],
)
def test_the_number_of_combinations_matches_the_theory(bet_type, fixture, expected):
    result = page(bet_type, fixture)
    assert result.count == expected
    assert result.warnings == []


def test_wakuren_covers_every_pair_of_frames_plus_the_doubles():
    """枠連はゾロ目があるので、枠の組み合わせより多くなる（同じ枠に2頭いる枠のぶん）。"""
    result = page("wakuren")
    pairs = len(list(combinations(range(N_FRAMES), 2)))       # 28
    doubles = [r["combo"] for r in result.rows if len(set(r["combo"].split("-"))) == 1]
    assert result.count == pairs + len(doubles)
    assert doubles and result.warnings == []                  # 理論値は出さない（警告も出ない）


def test_a_wrong_count_is_reported_as_a_warning(monkeypatch):
    """取りこぼしにすぐ気づけるよう、点数が理論値と違えば警告にする。"""
    real = jra_odds._pair_rows
    monkeypatch.setattr(jra_odds, "_pair_rows", lambda soup, bet: real(soup, bet)[:-1])
    result = page("umaren")
    assert result.count == 77
    assert "点数が理論値と違います" in result.warnings[0]


# --- 1つ1つの値 -------------------------------------------------------------------------


def test_tansho_is_a_single_number_per_horse():
    result = page("tansho")
    assert odds_of(result, "1") == {"combo": "1", "odds_low": 2.2, "odds_high": None}


def test_fukusho_and_wide_are_a_range():
    """複勝とワイドは「5.3 - 7.0」の幅で出るので、下限と上限の両方を持つ。"""
    assert odds_of(page("fukusho", "tansho"), "2") == {
        "combo": "2", "odds_low": 29.3, "odds_high": 75.4,
    }
    assert odds_of(page("wide"), "1-2") == {"combo": "1-2", "odds_low": 59.7, "odds_high": 74.2}


def test_a_pair_is_written_smallest_first():
    """並べ方の違いで別物にしないため、着順を見ない券種は小さい順にそろえる。"""
    result = page("umaren")
    assert all(
        [int(n) for n in row["combo"].split("-")] == sorted(
            int(n) for n in row["combo"].split("-")
        )
        for row in result.rows
    )


def test_umatan_and_sanrentan_keep_the_order():
    """馬単と3連単は着順が違えば別のオッズ。"""
    umatan = page("umatan")
    assert odds_of(umatan, "1-2")["odds_low"] != odds_of(umatan, "2-1")["odds_low"]
    tan3 = page("sanrentan")
    assert odds_of(tan3, "1-2-3")["odds_low"] != odds_of(tan3, "3-2-1")["odds_low"]


def test_the_same_horse_never_appears_twice_in_one_combination():
    for bet_type in ("umaren", "wide", "umatan", "sanrenpuku", "sanrentan"):
        for row in page(bet_type).rows:
            numbers = row["combo"].split("-")
            assert len(set(numbers)) == len(numbers), (bet_type, row)


def test_a_combination_nobody_bet_on_is_kept_without_odds():
    """`<td class="zero">票数なし</td>` は発売されていて票が入っていないだけ。

    組み合わせとしては存在するので、オッズを空にして残す（点数が狂わないように）。
    """
    result = page("sanrentan")
    empty = [row for row in result.rows if row["odds_low"] is None]
    assert len(empty) == 3
    assert odds_of(result, "11-2-8")["odds_low"] is None


# --- 更新時刻 ---------------------------------------------------------------------------


def test_a_finished_race_says_it_is_the_final_odds():
    assert page("umaren").odds_label == "最終オッズ"


def test_a_race_before_the_start_says_when_the_odds_are_from():
    """発走前は「12時24分現在オッズ」。発走時刻の欄と混ぜないこと。"""
    html = load("jra_odds_tansho_before_race.html")
    assert jra_odds.parse_odds_label(html) == "12時24分現在オッズ"
    result = jra_odds.parse_odds(html, "tansho", BEFORE_RACE_ID)
    assert result.count == 16 and result.warnings == []


def test_no_timestamp_does_not_break_anything():
    assert jra_odds.parse_odds_label("<html><body>なにもない</body></html>") is None


# --- 壊れたページ -----------------------------------------------------------------------


def test_an_empty_page_is_an_error_rather_than_silence():
    """サイトの作りが変わったことに気づけるよう、0件は例外にする。"""
    with pytest.raises(LayoutError):
        jra_odds.parse_odds("<html><body></body></html>", "umaren", RACE_ID)


def test_an_unknown_bet_type_is_refused():
    with pytest.raises(ValueError):
        jra_odds.parse_odds("<html></html>", "tansho3", RACE_ID)
