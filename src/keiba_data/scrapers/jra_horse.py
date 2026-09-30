"""JRA公式の**競走馬検索**と**競走馬の詳細ページ**を読む。

出典: https://www.jra.go.jp/ （robots.txtは全許可）

手元のDBには2023年以降のJRA全レースが入っているが、**「この馬の父は誰か」だけが
埋まっていない**（出馬表からしか取れず、過去分をさかのぼる道が無かった）。
種牡馬分析の距離適性・BMS相性・代表産駒などは、そこさえ埋まれば手元のDBだけで出せる。

## 辿り方（結果・オッズと同じ作りだが、**検索だけはトークンを組み立てる**）

1. **検索**（`accessR.html` にPOST・cp932）

       cname = pw02uli D1 0 0 0 0 コントレイル
               └───┘ └┘ │ │ │ └ 検索方法 0=で始まる 1=で終わる 2=を含む
                 │    │  │ │ └── 現役/抹消 0=全て
                 │    │  │ └──── 性別     0=全て
                 │    │  └────── 所属     0=全て
                 │    └───────── キャッシュ時間（ページのJSが固定でD1を入れている）
                 └────────────── 固定

   **ここにはチェックサムが無い**（ページの `common2.js` の `doSearch()` が
   この順に文字列を繋いでいるだけ）ので、ほかのページと違って自力で組める。
   「で始まる」検索なので、**2文字の頭で引くと最大200頭がまとめて返る**。
   200件ちょうどのときは頭打ちを疑い、頭を1文字伸ばして引き直す。

2. **詳細**（`accessU.html?CNAME=…` をGET）。CNAMEは検索結果のリンクから拾う
   （こちらは末尾2文字がチェックサムなので組み立てられない）。

## 血統登録番号

検索結果・詳細ページのリンクは `pw01dud00 2017101835 /0D` の形で、
真ん中の10桁が**血統登録番号**。その**先頭4桁が生年**なので、馬名・性別と合わせれば
同名の馬を取り違えずに突き合わせられる（実例: コントレイルは牡4と牝3の2頭いる）。
**抹消馬の「馬齢」は抹消した時点で止まっている**ので、年齢の突き合わせには使えない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

from keiba_data import parsing
from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.jra_result import HorseProfile

# 馬のページを開くトークン。`pw01dud` の次の2桁は入口で変わる（検索結果は00、血統のリンクは10）
HORSE_CNAME_RE = re.compile(r"pw01dud\d{2}(\d{10})/[0-9A-Za-z]{2}")
# 「牡4」「牝3」「せん10」
_SEX_AGE_RE = re.compile(r"(牡|牝|せん|セン|セ)\s*(\d+)?")
# 「2017年4月1日」
_BIRTH_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
# 「矢作 芳人（栗東）」
_STABLE_RE = re.compile(r"[（(](美浦|栗東)[）)]")

# DBの `horses.sex` は 牡/牝/セ。検索結果は「せん」表記なのでここでそろえる
SEX_LABELS = {"牡": "牡", "牝": "牝", "せん": "セ", "セン": "セ", "セ": "セ"}


@dataclass(frozen=True)
class HorseHit:
    """検索結果の1行。`cname` は詳細ページを開くトークン。"""

    name: str
    sex: str | None            # 牡/牝/セ
    age: int | None            # 抹消馬は抹消時点の馬齢なので、突き合わせには使わない
    trainer_name: str | None
    stable: str | None         # 美浦/栗東
    is_retired: bool
    cname: str
    horse_no: str              # 血統登録番号（10桁）

    @property
    def birth_year(self) -> int | None:
        """血統登録番号の先頭4桁＝生年。突き合わせの決め手にする。"""
        return int(self.horse_no[:4]) if self.horse_no[:4].isdigit() else None


@dataclass
class HorseSearchPage:
    """検索結果1ページぶん。`capped` は「上限に達していて取りこぼしがある疑い」。"""

    word: str
    hits: list[HorseHit]
    capped: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HorseDetail:
    """詳細ページのプロフィール欄。`profile` はDBへの保存にそのまま渡せる形。"""

    profile: HorseProfile
    sex: str | None = None
    birth_date: str | None = None       # 'YYYY-MM-DD'
    sire_no: str | None = None          # 父の血統登録番号
    broodmare_sire_no: str | None = None
    trainer_name: str | None = None
    stable: str | None = None
    coat_color: str | None = None
    birthplace: str | None = None


def build_search_cname(word: str, prefix: str) -> str:
    """馬名から検索トークンを組む（`prefix` は config.JRA_HORSE_SEARCH_CNAME）。

    JRAの検索は**全角カタカナ2文字以上**しか受け付けない。
    """
    word = parsing.normalize_space(word)
    if len(word) < 2:
        raise ValueError(f"馬名は2文字以上にしてください: {word!r}")
    return prefix + word


def _horse_no(node: Tag) -> tuple[str, str] | None:
    """`<a href="...CNAME=pw01dud002017101835/0D">` から (cname, 血統登録番号)。"""
    for anchor in node.find_all("a", href=True):
        matched = HORSE_CNAME_RE.search(anchor["href"])
        if matched:
            return matched.group(0), matched.group(1)
    return None


def _sex_age(text: str) -> tuple[str | None, int | None]:
    matched = _SEX_AGE_RE.search(text or "")
    if matched is None:
        return None, None
    age = int(matched.group(2)) if matched.group(2) else None
    return SEX_LABELS.get(matched.group(1)), age


def _clean(node: Tag | None, *, drop: str = "span.icon") -> str:
    """アイコン（「抹」「美浦」）を落としてから文字だけを取る。"""
    if node is None:
        return ""
    copy = BeautifulSoup(str(node), "lxml")
    for junk in copy.select(drop):
        junk.decompose()
    return parsing.normalize_space(copy.get_text(" ", strip=True))


def parse_horse_search(html: str, word: str, limit: int) -> HorseSearchPage:
    """競走馬検索の結果を読む。**1頭も当たらないのは普通のこと**（空で返す）。"""
    soup = BeautifulSoup(html, "lxml")
    if soup.find("h1", string=re.compile("パラメータエラー")) or "パラメータエラー" in (
        soup.title.get_text() if soup.title else ""
    ):
        raise LayoutError(f"検索トークンが受け付けられませんでした: {word!r}")

    hits: list[HorseHit] = []
    warnings: list[str] = []
    for cell in soup.select("td.horse"):
        row = cell.find_parent("tr")
        if row is None:
            continue
        found = _horse_no(cell)
        if found is None:                      # 馬のページを持たない行（見出しなど）
            continue
        cname, horse_no = found
        name = _clean(cell)
        if not name:
            warnings.append(f"馬名が読めない行があります（{horse_no}）")
            continue
        sex, age = _sex_age(_clean(row.select_one("td.age")))
        trainer_cell = row.select_one("td.trainer")
        stable = None
        if trainer_cell is not None:
            for icon in trainer_cell.select("span.icon"):
                label = parsing.normalize_space(icon.get_text())
                if label in ("美浦", "栗東"):
                    stable = label
        hits.append(HorseHit(
            name=name, sex=sex, age=age,
            trainer_name=_clean(trainer_cell) or None, stable=stable,
            is_retired=bool(cell.select_one("span.icon")),
            cname=cname, horse_no=horse_no,
        ))
    return HorseSearchPage(word=word, hits=hits, capped=len(hits) >= limit, warnings=warnings)


def _profile_pairs(soup: BeautifulSoup) -> dict[str, Tag]:
    """プロフィール欄の `<dt>項目</dt><dd>値</dd>` を項目名で引けるようにする。

    「母」「母の母」の `<dd>` には産駒一覧への `<span class="sanku">` が入っているので、
    値を読むときに落とす（落とさないと「ロードクロサイト産駒」になる）。
    """
    block = soup.select_one("div.profile")
    if block is None:
        raise LayoutError("競走馬の詳細ページにプロフィール欄(div.profile)がありません")
    pairs: dict[str, Tag] = {}
    for dl in block.find_all("dl"):
        dt, dd = dl.find("dt"), dl.find("dd")
        if dt is None or dd is None:
            continue
        key = parsing.normalize_space(dt.get_text()).replace(" ", "")
        pairs.setdefault(key, dd)
    return pairs


def parse_horse_profile(html: str) -> HorseDetail:
    """競走馬の詳細ページから、父・母・母の父・馬主・生産牧場などを読む。"""
    soup = BeautifulSoup(html, "lxml")
    pairs = _profile_pairs(soup)
    if "父" not in pairs:
        raise LayoutError("競走馬の詳細ページに「父」がありません")

    def value(key: str) -> str | None:
        node = pairs.get(key)
        if node is None:
            return None
        return _clean(node, drop="span.sanku, span.icon") or None

    def registration_no(key: str) -> str | None:
        node = pairs.get(key)
        found = _horse_no(node) if node is not None else None
        return found[1] if found else None

    # 見出しは `<h1><span class="txt"><span class="opt">競走馬情報</span>コントレイル
    # <span class="name_en">Contrail（JPN）</span><span class="rest">放牧</span></span></h1>`。
    # 「競走馬情報」・英名・近況（放牧など）を落として、**馬名だけ**を取る
    horse_name = _clean(soup.select_one("h1 span.txt"),
                        drop="span.opt, span.name_en, span.rest, span.icon")

    birth = _BIRTH_RE.search(value("生年月日") or "")
    trainer = value("調教師名") or ""
    stable = _STABLE_RE.search(trainer)
    sex, _age = _sex_age(value("性別") or "")

    return HorseDetail(
        profile=HorseProfile(
            umaban=None,
            horse_name=horse_name,
            sire=value("父"),
            dam=value("母"),
            broodmare_sire=value("母の父"),
            owner=value("馬主名"),
            breeder=value("生産牧場"),
        ),
        sex=sex,
        birth_date=(f"{int(birth.group(1)):04d}-{int(birth.group(2)):02d}-{int(birth.group(3)):02d}"
                    if birth else None),
        sire_no=registration_no("父"),
        broodmare_sire_no=registration_no("母の父"),
        trainer_name=_STABLE_RE.sub("", trainer).strip() or None,
        stable=stable.group(1) if stable else None,
        coat_color=value("毛色"),
        birthplace=value("産地"),
    )
