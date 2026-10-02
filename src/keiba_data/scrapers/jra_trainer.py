"""JRA公式サイトの**調教師名鑑**（現役の調教師プロフィール）を読む。

出典: https://www.jra.go.jp/datafile/meikan/trainer.html （robots.txtは全許可）

netkeiba の調教師名は4文字で切れていて（「中内田充」「二本柳俊」）、同じ4文字の別人を
見分けられない。名鑑には正式名・読み・生年月日・免許取得年（開業年）が載っている。

## 辿り方（すべてPOST・cp932。`cname` の末尾2文字はチェックサムなので必ずリンクを辿る）
1. `/datafile/meikan/trainer.html`（GET）に「あ行」〜「わ行」の10個のリンク `pw05cnl…`
2. 行ごとの一覧に、1人1リンク `doAction('/JRADB/accessC.html', 'pw05cmk001137/D0')`
3. その先が個人ページ

**`pw05cmk0` の直後の5桁が調教師コードで、DBの `trainer_id`（netkeiba・Targetと同じ体系）と
一致する**（中内田 充正＝01137。実物で確認済み）。名前で照合しなくてよい。
引退した調教師は `/datafile/meikan/retirement.html` から `pw05cmk1<コード>` で辿れる（今は取らない）。

## 個人ページのHTML（実物で確認済み）
    <h1>…<span class="txt"><span class="opt">調教師情報</span>中内田 充正
          <span class="kana">（ナカウチダ ミツマサ）</span></span></h1>
    <div class="data"><ul>
      <li><dl><dt>生年月日</dt><dd>1978年12月18日</dd></dl></li>
      <li><dl><dt>免許取得年</dt><dd>2012年</dd></dl></li>
      <li><dl><dt>所属</dt><dd>栗東</dd></dl></li> …
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from keiba_data import parsing
from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.jra_result import cnames

# 行ごとの一覧（あ行〜わ行）と、個人ページ（現役）のリンクの目印
LIST_MARKER = "pw05cnl"
PROFILE_MARKER = "pw05cmk0"
_CODE_RE = re.compile(r"pw05cmk0(\d{5})/")
_DATE_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
_YEAR_RE = re.compile(r"(\d{4})年")


@dataclass
class TrainerLink:
    """一覧の1人ぶん。"""

    trainer_id: str
    cname: str
    name: str            # 「中内田 充正」（姓と名の間に空白）


@dataclass
class TrainerProfile:
    """個人ページから読んだ1人ぶん。"""

    trainer_id: str
    full_name: str               # 「中内田 充正」
    kana: str | None             # 「ナカウチダ ミツマサ」
    birth_date: str | None       # 'YYYY-MM-DD'
    license_year: int | None     # 免許取得年（開業年の目安）
    stable: str | None           # 美浦/栗東


def parse_list_cnames(html: str) -> list[str]:
    """名鑑の入口・行ごとの一覧から、行ごとの一覧へのトークンを返す（重複なし・出てきた順）。"""
    return list(dict.fromkeys(cname for cname, _ in cnames(html, LIST_MARKER)))


def parse_trainer_links(html: str) -> list[TrainerLink]:
    """行ごとの一覧から、現役の調教師1人1リンクを返す。"""
    links: dict[str, TrainerLink] = {}
    for cname, label in cnames(html, PROFILE_MARKER):
        matched = _CODE_RE.match(cname)
        if matched is None:
            continue
        # 表示は「栗東 中内田 充正」のように所属の印が先に付く
        name = parsing.normalize_space(re.sub(r"^(美浦|栗東)\s*", "", label))
        links.setdefault(matched.group(1), TrainerLink(matched.group(1), cname, name))
    return list(links.values())


def trainer_id_of(cname: str) -> str | None:
    matched = _CODE_RE.match(cname)
    return matched.group(1) if matched else None


def _date(text: str | None) -> str | None:
    matched = _DATE_RE.search(text or "")
    if matched is None:
        return None
    year, month, day = (int(g) for g in matched.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_trainer_profile(html: str, trainer_id: str) -> TrainerProfile:
    """個人ページを読む。名前が読めなければ LayoutError。"""
    soup = BeautifulSoup(html, "lxml")
    txt = soup.select_one("h1 span.txt")
    if txt is None:
        raise LayoutError("調教師の個人ページに名前の見出しがありません")
    kana_tag = txt.select_one("span.kana")
    kana = parsing.normalize_space(kana_tag.get_text()).strip("（）() ") if kana_tag else None
    for extra in txt.select("span.opt, span.kana"):
        extra.decompose()
    full_name = parsing.normalize_space(txt.get_text(" ", strip=True))
    if not full_name:
        raise LayoutError("調教師の個人ページから名前が読めませんでした")

    fields: dict[str, str] = {}
    for dl in soup.select("div.data dl"):
        dt, dd = dl.find("dt"), dl.find("dd")
        if dt and dd:
            fields[parsing.normalize_space(dt.get_text())] = parsing.normalize_space(dd.get_text(" ", strip=True))
    year = _YEAR_RE.search(fields.get("免許取得年", ""))
    stable = fields.get("所属")
    return TrainerProfile(
        trainer_id=trainer_id,
        full_name=full_name,
        kana=kana or None,
        birth_date=_date(fields.get("生年月日")),
        license_year=int(year.group(1)) if year else None,
        stable=stable if stable in ("美浦", "栗東") else None,
    )
