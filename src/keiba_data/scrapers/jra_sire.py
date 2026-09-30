"""JRA公式サイトの**種牡馬リーディング**を読む。

出典: https://www.jra.go.jp/datafile/leading/ （robots.txtは全許可）

一口出資の検討に使う種牡馬分析の土台。**E・I（アーニングインデックス＝AEI）**を
JRA公式が年度ごとに出しているので、そこから取る。netkeibaは使わない。

## 表の中身（実物で確認済み）

    順位 | 種牡馬名（生年） | 毛色 | 産地 | 出走頭数 | 勝馬頭数 | 出走回数 |
         勝利回数 | 賞金 | 1出走賞金 | 1頭平均賞金 | 勝馬率 | E・I
      1   キズナ（2010年） 青鹿毛 新冠町 276 93 952 117 3,021,929,000円
          3,174,295円 10,949,018円 0.337 1.86

E・I は「その種牡馬の**1頭平均賞金** ÷ 全馬の1頭平均賞金」（実物で確認済み。
`1頭平均賞金 ÷ E・I` を100頭ぶん出すと2%以内に揃うが、`1出走賞金 ÷ E・I` はばらける）。
**1.00が全種牡馬の平均**。表には上位100頭しか載らないが、この逆算で
**全出走馬の1頭平均賞金**（＝AEIの分母）が手に入るので、任意の年範囲の通算AEIを出せる。

## 辿り方（レース結果・オッズとまったく同じ作り。すべてPOST・cp932）

1. `/datafile/leading/`（GET）の `doAction('/JRADB/accessU.html','pt03hld…')` → 1ページ目
2. そのページ自身が**年度の選択肢**（`<select id="year">` の option value）と
   **ページャ**（2〜5ページ目）のcnameを全部持っている
3. 「2歳」「全馬」の切り替えリンクも同じページにある

`cname` の末尾2文字はチェックサムで**組み立てられないので必ずリンクを辿る**
（config.py と jra_odds.py に同じ注意書きがある）。年度は2009年まで遡れる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

from keiba_data import parsing
from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.jra_result import ACTION_RE

# リーディングの種類。DBの kind 列に入れる値と、見出しの見分け方
KIND_ALL, KIND_TWO, KIND_BMS = "all", "two", "bms"
KIND_LABELS = {KIND_ALL: "全馬", KIND_TWO: "2歳", KIND_BMS: "ブルードメアサイヤー"}

# 入口ページのリンクの目印（`pt03hld…`＝種牡馬、`pt04bld…`＝ブルードメアサイヤー）
SIRE_MARKER = "hld"
BMS_MARKER = "bld"

# 「2026年度 リーディングサイヤー（全馬）」「2026年度 リーディングブルードメアサイヤー」
_HEADING_RE = re.compile(r"(\d{4})年度\s*リーディング(ブルードメア)?サイヤー(?:（([^）]*)）)?")
# 「キズナ（2010年）」の生年
_BIRTH_YEAR_RE = re.compile(r"[（(](\d{4})年[）)]")
# 「2026年9月27日現在」
_AS_OF_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日現在")


@dataclass(frozen=True)
class SireRow:
    """リーディング1行（＝その年のその種牡馬の産駒成績）。金額はすべて円。"""

    rank: int | None
    sire_name: str
    birth_year: int | None
    coat_color: str | None
    birthplace: str | None
    n_horses: int | None          # 出走頭数
    n_winners: int | None         # 勝馬頭数
    n_starts: int | None          # 出走回数
    n_wins: int | None            # 勝利回数
    prize_yen: int | None         # 賞金（本賞＋付加賞）
    prize_per_start: int | None   # 1出走賞金
    prize_per_horse: int | None   # 1頭平均賞金
    win_rate: float | None        # 勝馬率（勝馬頭数÷出走頭数）＝勝ち上がり率
    ei: float | None              # E・I（アーニングインデックス）


@dataclass
class SireLeadingPage:
    """リーディング1ページぶん。cnameは**このページに載っていたもの**をそのまま持つ。"""

    year: int | None
    kind: str
    rows: list[SireRow]
    as_of: str | None = None                         # 'YYYY-MM-DD'（「◯年◯月◯日現在」）
    year_cnames: dict[int, str] = field(default_factory=dict)   # 年度セレクタ
    page_cnames: dict[int, str] = field(default_factory=dict)   # ページャ（2ページ目以降）
    kind_cnames: dict[str, str] = field(default_factory=dict)   # 「2歳」「全馬」の切り替え
    warnings: list[str] = field(default_factory=list)


def find_index_cname(html: str, marker: str = SIRE_MARKER) -> str | None:
    """リーディング情報のページから、種牡馬リーディングへの入口トークンを拾う。

    `doAction('/JRADB/accessU.html', 'pt03hld0099993101/A1')` がそれ。
    ブルードメアサイヤーは `marker=BMS_MARKER`。
    """
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", onclick=True):
        matched = ACTION_RE.search(a["onclick"])
        if matched and marker in matched.group(1):
            return matched.group(1)
    return None


def _cname(tag: Tag | None) -> str | None:
    if tag is None or not tag.has_attr("onclick"):
        return None
    matched = ACTION_RE.search(tag["onclick"])
    return matched.group(1) if matched else None


def _int(text: str | None) -> int | None:
    """「3,021,929,000円」「276」→ int。空欄・「-」はNone。"""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _float(text: str | None) -> float | None:
    if not text:
        return None
    matched = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(matched.group(0)) if matched else None


def _leading_table(soup: BeautifulSoup) -> Tag:
    """リーディングの表（`<table class="… lead">`）。見つからなければ構造変化。"""
    for table in soup.find_all("table"):
        classes = table.get("class") or []
        if "lead" in classes and table.find("thead"):
            return table
    raise LayoutError("種牡馬リーディングの表が見つかりません")


def _parse_heading(soup: BeautifulSoup) -> tuple[int | None, str]:
    """見出しから年度と種類を読む。「2026年度 リーディングサイヤー（全馬）」。"""
    for heading in soup.find_all(["h1", "h2", "h3"]):
        matched = _HEADING_RE.search(parsing.normalize_space(heading.get_text()))
        if matched is None:
            continue
        year = int(matched.group(1))
        if matched.group(2):                      # 「ブルードメア」が入っていた
            return year, KIND_BMS
        return year, KIND_TWO if (matched.group(3) or "") == "2歳" else KIND_ALL
    raise LayoutError("リーディングの見出し（◯年度 リーディングサイヤー）がありません")


def _parse_as_of(soup: BeautifulSoup) -> str | None:
    matched = _AS_OF_RE.search(soup.get_text(" "))
    if matched is None:
        return None
    y, m, d = (int(x) for x in matched.groups())
    return f"{y:04d}-{m:02d}-{d:02d}"


def _parse_row(cells: list[Tag]) -> SireRow | None:
    """1行（13セル）を読む。セル数が足りない行（小計など）はNoneで飛ばす。"""
    if len(cells) < 13:
        return None
    text = [parsing.normalize_space(c.get_text()) for c in cells]
    name_cell = text[1]
    birth = _BIRTH_YEAR_RE.search(name_cell)
    sire_name = _BIRTH_YEAR_RE.sub("", name_cell).strip()
    if not sire_name:
        return None
    return SireRow(
        rank=_int(text[0]),
        sire_name=sire_name,
        birth_year=int(birth.group(1)) if birth else None,
        coat_color=text[2] or None,
        birthplace=text[3] or None,
        n_horses=_int(text[4]),
        n_winners=_int(text[5]),
        n_starts=_int(text[6]),
        n_wins=_int(text[7]),
        prize_yen=_int(text[8]),
        prize_per_start=_int(text[9]),
        prize_per_horse=_int(text[10]),
        win_rate=_float(text[11]),
        ei=_float(text[12]),
    )


def parse_sire_leading(html: str) -> SireLeadingPage:
    """種牡馬リーディングのページを読む。

    行だけでなく、**次に取りに行くためのcname**（年度・ページャ・種類）も一緒に返す。
    チェックサムが付いていて組み立てられないので、ページが持っている値をそのまま使う。
    """
    soup = BeautifulSoup(html, "lxml")
    year, kind = _parse_heading(soup)
    table = _leading_table(soup)

    rows: list[SireRow] = []
    warnings: list[str] = []
    body = table.find("tbody")
    for tr in (body.find_all("tr") if body else []):
        row = _parse_row(tr.find_all("td"))
        if row is not None:
            rows.append(row)
    if not rows:
        raise LayoutError("種牡馬リーディングの表に行がありません")
    if any(r.ei is None for r in rows):
        warnings.append("E・Iが読めない行があります")

    year_cnames: dict[int, str] = {}
    select = soup.find("select", id="year")
    for option in (select.find_all("option") if select else []):
        matched = re.search(r"(\d{4})", option.get_text())
        value = option.get("value")
        if matched and value:
            year_cnames[int(matched.group(1))] = value

    page_cnames: dict[int, str] = {}
    for block in soup.find_all("div", class_="pager"):
        for li in block.find_all("li"):
            label = parsing.normalize_space(li.get_text())
            cname = _cname(li.find("a"))
            if cname and label.isdigit():
                page_cnames[int(label)] = cname

    kind_cnames: dict[str, str] = {}
    for a in soup.find_all("a", onclick=True):
        label = parsing.normalize_space(a.get_text())
        cname = _cname(a)
        if cname and label in ("2歳", "全馬"):
            kind_cnames[KIND_TWO if label == "2歳" else KIND_ALL] = cname

    return SireLeadingPage(
        year=year, kind=kind, rows=rows, as_of=_parse_as_of(soup),
        year_cnames=year_cnames, page_cnames=page_cnames, kind_cnames=kind_cnames,
        warnings=warnings,
    )
