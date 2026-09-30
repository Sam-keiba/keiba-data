"""JRA公式サイトのオッズページから、**単勝から3連単までの7券種**を読む。

出典: https://www.jra.go.jp/ （robots.txtは全許可）

netkeibaのオッズページは走行データと同じく取りに行かない方針なので、オッズはJRA公式から取る。
JRAは**素のHTML**でオッズを出しているのでJavaScriptの実行が要らず、
しかも**3連単3,360点でも1ページに全部入っている**（16頭立てで691KB）。

## 辿り方（結果ページとまったく同じ作り。すべてPOST・cp932）
1. トップページ（GET）の `doAction('/JRADB/accessO.html','pw15oli00/6D')` → **開催選択**
2. 開催選択の `pw15orl0…` → **レース選択**
3. レース選択の1行に**そのレースの7券種ぶんのリンクが全部並んでいる**:

       pw151ouS3 06 2026 04 08 01 20260926 Z /7B
       └券種    └場 └年  └回 └日目└R └日付      └チェックサム

   券種は `pw15` の次の1桁: 1=単勝・複勝 3=枠連 4=馬連 5=ワイド 6=馬単 7=3連複 8=3連単
   （2は「単勝・複勝（人気順）」などで使われるため空けてある）。
   `cname` の末尾2文字はチェックサムで**組み立てられないので必ずリンクを辿る**。

4. オッズのページ自身にも7券種ぶんのリンクが載っているので、いちど入口を手に入れれば
   券種の切り替えは**1リクエスト**で済む（`OddsPage.links`）。

## 券種ごとのHTML（実物で確認済み）
- **単勝・複勝**（1ページで両方）… `table.tanpuku` の1行が1頭
  （`td.num` 馬番 / `td.odds_tan` 単勝 / `td.odds_fuku` 複勝＝幅）
- **枠連** … `table.waku` が枠ごとに並ぶ。軸は `<caption class="waku3">`
- **馬連・ワイド・馬単** … `table.umaren` / `table.wide` / `table.umatan`。
  軸は `<caption>` の番号、行は `<th>相手</th><td>オッズ</td>`。馬単は caption が1着
- **3連複** … `table.fuku3`、caption が `1-2`（1頭目-2頭目）、行は3頭目
- **3連単** … `ul.tan3_list > li` ごとに `div.p_line`「1着 1」「2着 2」＋`table.tan3`、行は3着
- 複勝とワイドだけ `5.3 - 7.0` の**幅**。自分自身との組み合わせは空セル
- 更新時刻は `div.cell.time`。発走前は「12時24分現在オッズ」、発走後は「最終オッズ」
- **発売されない券種はリンクが無い**（少頭数の枠連、前日の時点で未発売の券種など）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations, permutations

from bs4 import BeautifulSoup
from bs4.element import Tag

from keiba_data import config, parsing
from keiba_data.race_id import compose_race_id
from keiba_data.scrapers import LayoutError, jra_result

# --- 券種 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class BetType:
    """1つの券種。読み方の違いをここ1か所に持たせる。"""

    key: str  # DBと画面で使う名前
    label: str  # 「3連単」
    digit: str  # cnameの `pw15` の次の1桁
    legs: int  # 1組の頭数（単勝1・馬連2・3連単3）
    ordered: bool = False  # 着順を見るか（馬単・3連単）
    ranged: bool = False  # オッズが幅か（複勝・ワイド）
    by_frame: bool = False  # 馬番ではなく枠番か（枠連）
    table_class: str = ""  # オッズ表の目印になるclass


BET_TYPES: tuple[BetType, ...] = (
    BetType("tansho", "単勝", "1", 1, table_class="tanpuku"),
    BetType("fukusho", "複勝", "1", 1, ranged=True, table_class="tanpuku"),
    BetType("wakuren", "枠連", "3", 2, by_frame=True, table_class="waku"),
    BetType("umaren", "馬連", "4", 2, table_class="umaren"),
    BetType("wide", "ワイド", "5", 2, ranged=True, table_class="wide"),
    BetType("umatan", "馬単", "6", 2, ordered=True, table_class="umatan"),
    BetType("sanrenpuku", "3連複", "7", 3, table_class="fuku3"),
    BetType("sanrentan", "3連単", "8", 3, ordered=True, table_class="tan3"),
)
BET_TYPE_BY_KEY: dict[str, BetType] = {b.key: b for b in BET_TYPES}
# 1つのページから読める券種（単複だけ1ページで2券種ぶん取れる）
BET_TYPES_BY_DIGIT: dict[str, tuple[str, ...]] = {}
for _bet in BET_TYPES:
    BET_TYPES_BY_DIGIT[_bet.digit] = BET_TYPES_BY_DIGIT.get(_bet.digit, ()) + (_bet.key,)

# 1レースぶんのcname。末尾は 場(2) 年(4) 回(2) 日目(2) R(2) 日付(8) Z(+99) / チェックサム(2)
_RACE_CNAME_RE = re.compile(
    r"pw15(\d)ou\w*?(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})Z\d*/[0-9A-Za-z]{2}$"
)
# 「12時24分現在オッズ」「最終オッズ」。発走時刻の欄と見分けるために「オッズ」を必須にする
_ODDS_LABEL_RE = re.compile(r"(?:\d{1,2}時\s*\d{1,2}分現在|最終|確定)[^ ]*オッズ")
# 「1着 7」「2着 12」
_PLACE_RE = re.compile(r"(\d+)着\s*(\d+)")
# 枠連の軸（`<caption class="waku3">`）
_WAKU_CLASS_RE = re.compile(r"^waku(\d+)$")


def _text(tag: Tag | None) -> str:
    return parsing.normalize_space(tag.get_text(" ", strip=True)) if tag else ""


# --- 入口をたどる ---------------------------------------------------------------------


def find_index_cname(html: str) -> str | None:
    """トップページからオッズの開催選択ページを開くトークンを拾う（`pw15oli00/6D`）。"""
    return jra_result.find_index_cname(html, "oli")


def parse_odds_index(html: str) -> list[jra_result.Meeting]:
    """オッズの開催選択ページから開催の一覧を読む。"""
    return jra_result.parse_meeting_index(html, "orl")


def parse_race_links(
    html: str, meeting: jra_result.Meeting | None = None
) -> dict[str, dict[str, str]]:
    """オッズのページから `{race_id: {券種: cname}}` を集める。

    レース選択ページなら開催の全レースぶん、券種のページならそのレースぶんが取れる
    （どのオッズページにも7券種ぶんのリンクが載っている）。

    `meeting` を渡すと、開催（場・回・日目・日付）の食い違うトークンを取り違え防止に捨てる。
    発売されていない券種はリンクが無いので、券種が欠けることは正常。
    """
    found = set(jra_result.ACTION_RE.findall(html)) | set(
        re.findall(r"CNAME=([^\"'&\s]+)", html)
    )
    links: dict[str, dict[str, str]] = {}
    for cname in found:
        matched = _RACE_CNAME_RE.search(cname)
        if matched is None:
            continue
        digit, venue_code, year, kaiji, nichime, race_no, yyyymmdd = matched.groups()
        bet_keys = BET_TYPES_BY_DIGIT.get(digit)
        if not bet_keys or venue_code not in config.VENUE_CODES:
            continue
        date = f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
        if meeting is not None and (venue_code, int(kaiji), int(nichime), date) != (
            meeting.venue_code, meeting.kaiji, meeting.nichime, meeting.date
        ):
            continue
        try:
            race_id = compose_race_id(
                int(year), venue_code, int(kaiji), int(nichime), int(race_no)
            )
        except ValueError:
            continue
        for key in bet_keys:
            links.setdefault(race_id, {})[key] = cname
    return links


# --- オッズを読む ---------------------------------------------------------------------


@dataclass
class OddsPage:
    """1レース1券種ぶんのオッズ。"""

    race_id: str
    bet_type: str
    rows: list[dict] = field(default_factory=list)  # combo / odds_low / odds_high
    odds_label: str | None = None  # 「12時24分現在オッズ」「最終オッズ」
    links: dict[str, str] = field(default_factory=dict)  # このページに載っていた券種のcname
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)


def parse_odds_label(html_or_soup: str | BeautifulSoup) -> str | None:
    """「12時24分現在オッズ」（発走前）または「最終オッズ」（発走後）。"""
    soup = _soup(html_or_soup)
    for cell in soup.select("div.cell.time, .cell.time"):
        text = _text(cell)
        if _ODDS_LABEL_RE.search(text):
            return text
    return None


def _soup(html_or_soup: str | BeautifulSoup) -> BeautifulSoup:
    if isinstance(html_or_soup, BeautifulSoup):
        return html_or_soup
    return BeautifulSoup(html_or_soup, "lxml")


def _odds_cell(cell: Tag | None) -> tuple[bool, float | None, float | None]:
    """オッズのマスを (発売されているか, 下限, 上限) に読む。

    - `"5.3 - 7.0"` → (True, 5.3, 7.0) … 複勝・ワイドは幅
    - `"32.2"` → (True, 32.2, None)
    - `<td class="zero">票数なし</td>` → **(True, None, None)**
      発売はされていて票が入っていないだけなので、組み合わせとしては残す
    - 空（自分自身との組み合わせ）や「取消」→ (False, None, None)
    """
    if cell is None:
        return False, None, None
    text = parsing.normalize_space(cell.get_text(" ", strip=True)).replace(",", "")
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if numbers:
        return True, float(numbers[0]), float(numbers[1]) if len(numbers) > 1 else None
    if "zero" in (cell.get("class") or []) or "票数なし" in text:
        return True, None, None
    return False, None, None


def _axis_rows(table: Tag) -> list[tuple[int, Tag]]:
    """軸ごとの表から (相手の番号, オッズのマス) を読む。"""
    rows: list[tuple[int, Tag]] = []
    for tr in table.find_all("tr"):
        head, cell = tr.find("th"), tr.find("td")
        if head is None or cell is None:
            continue
        number = parsing.to_int(_text(head))
        if number is None:
            continue
        rows.append((number, cell))
    return rows


def _combo(numbers: list[int], bet: BetType) -> str:
    """`"3-7-11"`。着順を見ない券種は小さい順にそろえる（並べ方の違いで別物にしない）。"""
    if not bet.ordered:
        numbers = sorted(numbers)
    return "-".join(str(n) for n in numbers)


def _single_rows(soup: BeautifulSoup, bet: BetType) -> list[dict]:
    """単勝・複勝（1つの表に1頭1行）。"""
    cell_class = "odds_fuku" if bet.key == "fukusho" else "odds_tan"
    rows: list[dict] = []
    for tr in soup.select("table.tanpuku tr"):
        umaban = parsing.to_int(_text(tr.select_one("td.num")))
        if umaban is None:
            continue
        sold, low, high = _odds_cell(tr.select_one(f"td.{cell_class}"))
        if not sold:
            continue  # 取消・除外
        rows.append({"combo": str(umaban), "odds_low": low, "odds_high": high})
    return rows


def _frame_rows(soup: BeautifulSoup, bet: BetType) -> list[dict]:
    """枠連（軸の枠番は `<caption class="waku3">` にある）。"""
    rows: list[dict] = []
    for table in soup.select("table.waku"):
        caption = table.caption
        axis = None
        for name in (caption.get("class") or []) if caption else []:
            if matched := _WAKU_CLASS_RE.match(name):
                axis = int(matched.group(1))
        if axis is None:
            continue
        for number, cell in _axis_rows(table):
            sold, low, high = _odds_cell(cell)
            if not sold:
                continue
            rows.append({"combo": _combo([axis, number], bet), "odds_low": low, "odds_high": high})
    return rows


def _pair_rows(soup: BeautifulSoup, bet: BetType) -> list[dict]:
    """馬連・ワイド・馬単（軸は `<caption>` の番号。馬単の軸は1着）。"""
    rows: list[dict] = []
    for table in soup.select(f"table.{bet.table_class}"):
        axis = parsing.to_int(_text(table.caption))
        if axis is None:
            continue
        for number, cell in _axis_rows(table):
            sold, low, high = _odds_cell(cell)
            if not sold:
                continue
            rows.append({"combo": _combo([axis, number], bet), "odds_low": low, "odds_high": high})
    return rows


def _trio_rows(soup: BeautifulSoup, bet: BetType) -> list[dict]:
    """3連複（caption が `1-2`、行が3頭目）。"""
    rows: list[dict] = []
    for table in soup.select("table.fuku3"):
        pair = [int(n) for n in re.findall(r"\d+", _text(table.caption))]
        if len(pair) != 2:
            continue
        for number, cell in _axis_rows(table):
            sold, low, high = _odds_cell(cell)
            if not sold:
                continue
            rows.append({
                "combo": _combo([*pair, number], bet), "odds_low": low, "odds_high": high,
            })
    return rows


def _trifecta_rows(soup: BeautifulSoup, bet: BetType) -> list[dict]:
    """3連単（`li` ごとに「1着 N」「2着 M」＋3着の表）。"""
    rows: list[dict] = []
    for item in soup.select("ul.tan3_list > li"):
        table = item.select_one("table.tan3")
        if table is None:
            continue
        places: dict[int, int] = {}
        for line in item.select("div.p_line"):
            if matched := _PLACE_RE.search(_text(line)):
                places[int(matched.group(1))] = int(matched.group(2))
        if 1 not in places or 2 not in places:
            continue
        for number, cell in _axis_rows(table):
            sold, low, high = _odds_cell(cell)
            if not sold:
                continue
            rows.append({
                "combo": _combo([places[1], places[2], number], bet),
                "odds_low": low, "odds_high": high,
            })
    return rows


# 券種ごとの読み方。ここに無い券種（馬連・ワイド・馬単）は軸ごとの2頭の表。
_READERS = {
    "tansho": _single_rows,
    "fukusho": _single_rows,
    "wakuren": _frame_rows,
    "sanrenpuku": _trio_rows,
    "sanrentan": _trifecta_rows,
}


def _rows_for(soup: BeautifulSoup, bet: BetType) -> list[dict]:
    return _READERS.get(bet.key, _pair_rows)(soup, bet)


def expected_count(bet: BetType, numbers: set[int]) -> int | None:
    """出ている馬番の数から点数の理論値を出す（枠連は枠の中の頭数で変わるので出さない）。"""
    n = len(numbers)
    if bet.by_frame or bet.legs == 1 or n < bet.legs:
        return None
    if bet.ordered:
        return len(list(permutations(range(n), bet.legs)))
    return len(list(combinations(range(n), bet.legs)))


def parse_odds(html: str, bet_type: str, race_id: str) -> OddsPage:
    """1レース1券種ぶんのオッズを読む。

    単勝と複勝は同じページから取れるので、同じHTMLを `bet_type` だけ変えて2回渡す。
    点数が理論値と合わなければ `warnings` に入れる（取りこぼしにすぐ気づけるように）。
    """
    bet = BET_TYPE_BY_KEY.get(bet_type)
    if bet is None:
        raise ValueError(f"知らない券種です: {bet_type!r}")
    soup = _soup(html)
    rows = _rows_for(soup, bet)
    page = OddsPage(
        race_id=race_id,
        bet_type=bet.key,
        rows=rows,
        odds_label=parse_odds_label(soup),
        links=parse_race_links(html).get(race_id, {}),
    )
    if not rows:
        raise LayoutError(f"{bet.label}のオッズが1件も読めませんでした（{race_id}）")

    numbers = {int(n) for row in rows for n in row["combo"].split("-")}
    expected = expected_count(bet, numbers)
    if expected is not None and expected != len(rows):
        page.warnings.append(
            f"{bet.label}の点数が理論値と違います: {len(rows)}点"
            f"（{len(numbers)}頭なら{expected}点）"
        )
    return page
