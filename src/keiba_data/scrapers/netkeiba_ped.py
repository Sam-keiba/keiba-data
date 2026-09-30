"""netkeibaの**5代血統表**を読む（血統クロス分析の土台）。

出典: https://db.netkeiba.com/horse/ped/{horse_id}/

## なぜここから取るか（ほかを全部あたった結果）

- **JRA公式では届かない。** 競走馬ページは父・母・母の父・母の母にリンクを持ち再帰できるが、
  **日本で走っていない祖先にはページが無く、そこで途切れる**。実測で3代目までは必ず完全、
  4代目は半分、5代目は1〜2割しか埋まらなかった。Sadler's Wells や Northern Dancer は
  まさにその4〜5代目にいる
- **JBIS（JAIRS公式）は規約で不可。** 「加工・編集することなくあるがままに」利用すること、
  第三者提供の禁止が明記されている。加えて `robots.txt` に `Crawl-delay: 600`（10分間隔）
- allbreedpedigree.com / pedigreequery.com は403（bot対策）

**個人で見るためだけに使い、再配布はしない。** ほかのページと同じく3秒間隔で取る。

## 表の作り（実物で確認済み）

`table.blood_table` は32行の規則的な表で、`rowspan` が代数を表す。

    rowspan 16 × 2本 = 1代目（父・母）     rowspan 4 × 8本 = 3代目
    rowspan  8 × 4本 = 2代目               rowspan 2 × 16本 = 4代目
                                            rowspan 1 × 32本 = 5代目   合計62セル

各セルは `<a href="/horse/ped/{id}/">`。IDは日本産が `2010105827`（血統登録番号）、
外国産が `000a00033a`。名前は `Bedtime Toy&nbsp;(米)` のように産国が付く。

## 位置（path）の決め方

世代 `g` のセルは `rowspan = 2^(5-g)`。行 `r` から始まるセルの世代内の通し番号は
`i = r / rowspan` で、`i` を `g` ビットの2進数として**上の桁から 0=父(f)・1=母(m)** と
読むと `path` になる（`f`＝父、`mf`＝母の父、`mmmmm`＝母の母の母の母の母）。

フレンドミラノ（父イスラボニータ・母ネイビーロマン・母の父キズナ）で突き合わせ済み。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import Tag

from keiba_data import parsing
from keiba_data.scrapers import LayoutError

# 5代血統表の形。世代ごとのセル数（これと違ったら構造が変わったとみなす）
GENERATIONS = 5
CELLS_PER_GENERATION = {1: 2, 2: 4, 3: 8, 4: 16, 5: 32}
TOTAL_CELLS = sum(CELLS_PER_GENERATION.values())   # 62
N_ROWS = 2 ** GENERATIONS                          # 32

# 「Bedtime Toy (米)」「サンデーサイレンス」。産国は末尾の短い括弧書き
_COUNTRY_RE = re.compile(r"^(.*?)\s*[（(]\s*([^）)]{1,4})\s*[）)]\s*$")
# `/horse/ped/2010105827/` `/horse/000a00033a/`
_HORSE_NO_RE = re.compile(r"/horse/(?:ped/)?(\w+)/?")


@dataclass(frozen=True)
class PedigreeCell:
    """5代血統表の1マス。"""

    path: str            # 'f' 'm' 'ff' … 'mmmmm'（父=f・母=m）
    generation: int      # 1〜5（path の長さ）
    horse_no: str        # 日本産は血統登録番号、外国産は 000a00033a 形式
    name: str            # 産国を外した馬名
    country: str | None  # 「米」「愛」。日本産はNone


def _grid(table: Tag) -> list[tuple[int, int, int, Tag]]:
    """rowspanを追って、セルを (行, 列, rowspan, セル) の格子に置く。

    血統表はセルが縦にまたがるので、**行の中の何番目か**では列が決まらない。
    列ごとに「あと何行ふさがっているか」を持って、空いている左端に置いていく。
    """
    body = table.find("tbody") or table
    rows = body.find_all("tr", recursive=False)
    if len(rows) != N_ROWS:
        raise LayoutError(f"5代血統表の行数が{len(rows)}行です（{N_ROWS}行のはず）")

    cells: list[tuple[int, int, int, Tag]] = []
    occupied: dict[int, int] = {}
    for row, tr in enumerate(rows):
        column = 0
        for td in tr.find_all("td", recursive=False):
            while occupied.get(column, 0) > 0:
                column += 1
            span = int(td.get("rowspan") or 1)
            cells.append((row, column, span, td))
            occupied[column] = span
            column += 1
        for key in list(occupied):
            occupied[key] -= 1
    return cells


def _path(row: int, column: int, span: int) -> str:
    """格子の位置から path（'f' 'mf' 'ffm' …）を組む。"""
    generation = column + 1
    index = row // span
    return "".join(
        "m" if (index >> (generation - 1 - bit)) & 1 else "f" for bit in range(generation)
    )


def _name_and_country(text: str) -> tuple[str, str | None]:
    """「Bedtime Toy (米)」→ ('Bedtime Toy', '米')。産国が無ければ (名前, None)。"""
    text = parsing.normalize_space(text.replace("\xa0", " "))
    matched = _COUNTRY_RE.match(text)
    if matched and matched.group(1):
        return matched.group(1).strip(), matched.group(2).strip()
    return text, None


def parse_pedigree(html: str) -> list[PedigreeCell]:
    """5代血統表を62マスぶん読む。マスが欠けていたら `LayoutError`。

    **欠けたまま返さない**のが大事なところ。血統表は部分的に埋まっていると
    「持っていない」と誤判定してしまい、不完全ではなく**間違った答え**になる。
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.blood_table")
    if table is None:
        raise LayoutError("5代血統表(table.blood_table)がありません")

    cells: list[PedigreeCell] = []
    for row, column, span, td in _grid(table):
        if column >= GENERATIONS:
            raise LayoutError(f"5代血統表に{column + 1}代目の列があります")
        anchor = td.find("a", href=True)
        if anchor is None:
            raise LayoutError(f"血統表のマスに馬のリンクがありません（{row}行{column}列）")
        matched = _HORSE_NO_RE.search(anchor["href"])
        if matched is None:
            raise LayoutError(f"血統表のリンクから馬IDを読めません: {anchor['href']}")
        name, country = _name_and_country(anchor.get_text(" ", strip=True))
        if not name:
            raise LayoutError(f"血統表のマスに馬名がありません（{row}行{column}列）")
        cells.append(PedigreeCell(
            path=_path(row, column, span), generation=column + 1,
            horse_no=matched.group(1), name=name, country=country,
        ))

    counts: dict[int, int] = {}
    for cell in cells:
        counts[cell.generation] = counts.get(cell.generation, 0) + 1
    if counts != CELLS_PER_GENERATION:
        raise LayoutError(f"5代血統表のマスの数が{counts}です（{CELLS_PER_GENERATION}のはず）")
    if len({c.path for c in cells}) != TOTAL_CELLS:
        raise LayoutError("5代血統表に同じ位置のマスが2つあります")
    return cells
