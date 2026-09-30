"""netkeibaの5代血統表のパース（scrapers/netkeiba_ped.py）と、その保存。

フィクスチャはフレンドミラノ（父イスラボニータ・母ネイビーロマン・母の父キズナ）。
"""

import re

import pytest

from keiba_data import db
from keiba_data.scrapers import LayoutError
from keiba_data.scrapers.netkeiba_ped import (
    CELLS_PER_GENERATION,
    TOTAL_CELLS,
    parse_pedigree,
)
from tests.conftest import load_fixture

HORSE_ID = "2024100841"


def ped_html():
    return load_fixture(f"netkeiba_ped_{HORSE_ID}.html")


def cells_by_path():
    return {c.path: c for c in parse_pedigree(ped_html())}


def test_parse_pedigree_reads_all_62_cells():
    cells = parse_pedigree(ped_html())
    assert len(cells) == TOTAL_CELLS == 62
    counts: dict[int, int] = {}
    for cell in cells:
        counts[cell.generation] = counts.get(cell.generation, 0) + 1
    assert counts == CELLS_PER_GENERATION == {1: 2, 2: 4, 3: 8, 4: 16, 5: 32}


def test_paths_line_up_with_the_real_pedigree():
    """`f`＝父、`m`＝母、`mf`＝母の父。rowspanから位置を決めているので、ここがずれると全部ずれる。"""
    by_path = cells_by_path()
    assert by_path["f"].name == "イスラボニータ"
    assert by_path["m"].name == "ネイビーロマン"
    assert by_path["mf"].name == "キズナ"           # 母の父
    assert by_path["ff"].name == "フジキセキ"        # 父の父
    assert by_path["fff"].name == "サンデーサイレンス"
    assert by_path["mff"].name == "ディープインパクト"  # 母の父の父


def test_every_path_is_a_valid_position():
    """path は f/m の並びで、長さがそのまま代数になる。重複も無い。"""
    cells = parse_pedigree(ped_html())
    assert len({c.path for c in cells}) == TOTAL_CELLS
    for cell in cells:
        assert re.fullmatch(r"[fm]{1,5}", cell.path)
        assert len(cell.path) == cell.generation


def test_registration_numbers_cover_japanese_and_foreign_horses():
    by_path = cells_by_path()
    assert by_path["f"].horse_no == "2011103565"     # 日本産は血統登録番号
    assert by_path["fff"].horse_no == "000a00033a"   # 外国産は別の形


def test_country_is_split_off_the_name():
    """「Bedtime Toy (米)」→ 名前と産国に分ける。日本産は産国なし。"""
    by_path = cells_by_path()
    assert (by_path["mmmmm"].name, by_path["mmmmm"].country) == ("Bedtime Toy", "米")
    assert by_path["f"].country is None


def test_parse_pedigree_raises_without_the_table():
    with pytest.raises(LayoutError):
        parse_pedigree("<html><body><p>血統表がない</p></body></html>")


def test_parse_pedigree_raises_on_a_short_table():
    """マスが欠けたまま返さない（部分的な血統表は「間違った答え」を生むため）。"""
    html = ped_html().replace("</tr>", "</tr>", 1)
    broken = re.sub(r"<tr>.*?</tr>", "", html, count=4, flags=re.S)
    with pytest.raises(LayoutError):
        parse_pedigree(broken)


# --- 保存 -------------------------------------------------------------------------

def test_save_horse_pedigree_tree_is_idempotent(conn):
    cells = parse_pedigree(ped_html())
    assert db.save_horse_pedigree_tree(conn, HORSE_ID, cells) == 62
    db.save_horse_pedigree_tree(conn, HORSE_ID, cells)        # 2回目でも増えない
    (rows,) = conn.execute(
        "SELECT COUNT(*) FROM horse_ancestors WHERE horse_id = ?", (HORSE_ID,)
    ).fetchone()
    assert rows == 62
    (ancestors,) = conn.execute("SELECT COUNT(*) FROM pedigree_horses").fetchone()
    assert ancestors == len({c.horse_no for c in cells})


def test_save_horse_pedigree_tree_replaces_old_rows(conn):
    """マスの中身が変わったら、古い行は残さない。"""
    cells = parse_pedigree(ped_html())
    db.save_horse_pedigree_tree(conn, HORSE_ID, cells)
    db.save_horse_pedigree_tree(conn, HORSE_ID, cells[:3])
    (rows,) = conn.execute(
        "SELECT COUNT(*) FROM horse_ancestors WHERE horse_id = ?", (HORSE_ID,)
    ).fetchone()
    assert rows == 3
