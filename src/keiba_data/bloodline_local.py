"""手元のデータだけで5代血統表を広げる（`keiba-data bloodline-local`。ネットにはアクセスしない）。

netkeiba の5代血統表（`horse_ancestors` の source='netkeiba'）は2023年以降に走った馬にしか無い。
それより前の馬は、次の2つを組み合わせて組み立てる（source='local'）。

1. **祖先の部分木** … netkeiba の血統表に出てくる祖先は、その下に自分の祖先も持っている
   （ある馬の父がディープインパクトなら、その馬の 'f' から下はディープインパクトの4代血統表）。
   祖先ごとに「相対的な path → 祖先」を集めておき、同じ祖先が出てきたら接ぎ木する。
2. **Target の horse_data** … 1頭ごとに7頭の祖先（父・父の母の父・母・母父・母の母・母の母の父・
   母の母の母）が繁殖登録番号つきで入っている。これを種にする。国内で走った祖先は自分の
   horse_data の行も持っているので、それもたどる。

繁殖登録番号は netkeiba の番号と体系が違うので、両方を持つ馬の同じマスどうしで対応表を作る
（2023年以降の馬で約2万件、食い違いは0件）。対応の無い番号は、国内で走った馬なら馬名で
horse_id に引き（性別と、子より3年以上前に生まれていることを確かめる）、それでも引けなければ
`jv:<繁殖登録番号>` として `pedigree_horses` に入れる。

これ以上増えなくなるまで接ぎ木を繰り返す。マスが欠けることはあるが、入れたマスは
取り置き検証（`--check`）で99%以上が netkeiba と一致する。
"""

from __future__ import annotations

import logging
import sqlite3
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from keiba_data import db

logger = logging.getLogger(__name__)

MAX_GENERATION = 5
LOCAL = "local"

# horse_data の7祖先: path → (繁殖登録番号の列, 名前の列)
TARGET_CELLS = {
    "f": ("sire_breed_no", "sire_name"),
    "fmf": ("sire_bms_breed_no", "sire_bms_name"),
    "m": ("dam_breed_no", "dam_name"),
    "mf": ("bms_breed_no", "bms_name"),
    "mm": ("dam_dam_breed_no", "dam_dam_name"),
    "mmf": ("dam_dam_sire_breed_no", "dam_dam_sire_name"),
    "mmm": ("dam_dam_dam_breed_no", "dam_dam_dam_name"),
}


def is_horse_id(no: str) -> bool:
    """netkeiba の番号のうち、日本産（＝血統登録番号＝horses.horse_id）のもの。"""
    return len(no) == 10 and no.isdigit()


def subtrees(trees: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """祖先 → {その祖先から見た path: 祖先}。馬そのものの血統表も入れる。"""
    rel: dict[str, dict[str, str]] = defaultdict(dict)
    for horse_id, tree in trees.items():
        for path, no in tree.items():
            rel[horse_id].setdefault(path, no)
            for sub, anc in tree.items():
                if len(sub) > len(path) and sub.startswith(path):
                    rel[no].setdefault(sub[len(path):], anc)
    return rel


def breed_no_map(trees: dict[str, dict[str, str]], target_cells: dict[str, dict[str, tuple]]) -> tuple[dict, int]:
    """繁殖登録番号 → netkeiba の番号。両方を持つ馬の同じマスから作る。食い違う番号は採らない。"""
    seen: dict[str, Counter] = defaultdict(Counter)
    for horse_id, tree in trees.items():
        for path, (breed_no, _name) in target_cells.get(horse_id, {}).items():
            if breed_no and path in tree:
                seen[breed_no][tree[path]] += 1
    mapping = {b: c.most_common(1)[0][0] for b, c in seen.items() if len(c) == 1}
    return mapping, sum(1 for c in seen.values() if len(c) > 1)


@dataclass
class Builder:
    """手元のデータから1頭ぶんの血統表を組み立てる。"""

    rel: dict[str, dict[str, str]]
    b2n: dict[str, str]
    target_cells: dict[str, dict[str, tuple]]                 # horse_id → {path: (繁殖登録番号, 名前)}
    by_name: dict[str, list[tuple[str, str | None]]]          # 馬名 → [(horse_id, 性別)]
    named: Counter = field(default_factory=Counter)           # 解決のしかたごとの数（ログ用）

    def resolve(self, breed_no: str | None, name: str | None, path: str, child_year: int) -> str | None:
        """繁殖登録番号（と名前）→ 祖先の番号。番号の対応 → 馬名 → `jv:` の順。"""
        if breed_no and breed_no in self.b2n:
            self.named["number"] += 1
            return self.b2n[breed_no]
        if name:
            want = "牝" if path[-1] == "m" else "牡"
            hits = [h for h, sex in self.by_name.get(name, ())
                    if sex in (want, None) and int(h[:4]) <= child_year - 3]
            if len(hits) == 1:
                self.named["name"] += 1
                return hits[0]
        if breed_no:
            self.named["jv"] += 1
            return "jv:" + breed_no
        return None

    def build(self, horse_id: str, seeds: dict[str, tuple] | None = None) -> dict[str, str]:
        cells: dict[str, str] = {}
        year = int(horse_id[:4])

        def child_year(path: str) -> int:
            parent = cells.get(path[:-1]) if len(path) > 1 else horse_id
            return int(parent[:4]) if parent and is_horse_id(parent) else year - 3 * (len(path) - 1)

        def put_target(prefix: str, cell_values: dict[str, tuple]) -> bool:
            grew = False
            for path, (breed_no, name) in cell_values.items():
                full = prefix + path
                if len(full) > MAX_GENERATION or full in cells:
                    continue
                no = self.resolve(breed_no, name, full, child_year(full))
                if no:
                    cells[full] = no
                    grew = True
            return grew

        # その馬自身が netkeiba の血統表に祖先として出てくる（2023年以降の馬の母など）なら、まずそれを使う
        for path, no in self.rel.get(horse_id, {}).items():
            if len(path) <= MAX_GENERATION:
                cells[path] = no
        put_target("", seeds if seeds is not None else self.target_cells.get(horse_id, {}))
        expanded: set[str] = set()
        grew = True
        while grew:
            grew = False
            for path, no in sorted(cells.items(), key=lambda c: len(c[0])):
                if (path, no) in expanded or len(path) >= MAX_GENERATION:
                    continue
                expanded.add((path, no))
                for sub, anc in self.rel.get(no, {}).items():
                    full = path + sub
                    if len(full) <= MAX_GENERATION and full not in cells:
                        cells[full] = anc
                        grew = True
                if is_horse_id(no) and no in self.target_cells:
                    grew |= put_target(path, self.target_cells[no])
        return cells


# --- DBから読む・DBへ書く ----------------------------------------------------------


def load_trees(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    trees: dict[str, dict[str, str]] = defaultdict(dict)
    for horse_id, path, no in conn.execute(
            "SELECT horse_id, path, ancestor_no FROM horse_ancestors WHERE source = 'netkeiba'"):
        trees[horse_id][path] = no
    return dict(trees)


def load_target_cells(conn: sqlite3.Connection) -> dict[str, dict[str, tuple]]:
    cols = ", ".join(c for pair in TARGET_CELLS.values() for c in pair)
    cells = {}
    for row in conn.execute(f"SELECT horse_id, {cols} FROM target_horses"):
        cells[row[0]] = {path: (row[1 + 2 * i], row[2 + 2 * i]) for i, path in enumerate(TARGET_CELLS)
                         if row[1 + 2 * i] or row[2 + 2 * i]}
    return cells


def load_names(conn: sqlite3.Connection) -> dict[str, list[tuple[str, str | None]]]:
    """馬名 → [(horse_id, 性別)]。国内で走った馬（target_horses と horses）。"""
    by_name: dict[str, dict[str, str | None]] = defaultdict(dict)
    for horse_id, name, sex in conn.execute("SELECT horse_id, horse_name, sex FROM target_horses"):
        if name:
            by_name[name][horse_id] = sex
    for horse_id, name, sex in conn.execute("SELECT horse_id, horse_name, sex FROM horses"):
        if name and horse_id not in by_name[name]:
            by_name[name][horse_id] = sex
    # 去勢馬も牡として数える（父として出てくる馬は去勢前）
    return {n: [(h, "牡" if s == "セ" else s) for h, s in ids.items()] for n, ids in by_name.items()}


def builder_from(conn: sqlite3.Connection, trees: dict[str, dict[str, str]] | None = None) -> tuple[Builder, int]:
    trees = load_trees(conn) if trees is None else trees
    target_cells = load_target_cells(conn)
    b2n, conflicts = breed_no_map(trees, target_cells)
    return Builder(subtrees(trees), b2n, target_cells, load_names(conn)), conflicts


def _jv_names(conn: sqlite3.Connection) -> dict[str, str]:
    """繁殖登録番号 → 名前（Target の表記。カナにできるものは本体の表記にそろえる）。"""
    from keiba_data.target_import import _name_aliases
    seen: dict[str, Counter] = defaultdict(Counter)
    for no_col, name_col in TARGET_CELLS.values():
        for no, name, n in conn.execute(
                f"SELECT {no_col}, {name_col}, COUNT(*) FROM target_horses "
                f"WHERE {no_col} IS NOT NULL AND {name_col} IS NOT NULL GROUP BY 1, 2"):
            seen[no][name] += n
    names = {no: c.most_common(1)[0][0] for no, c in seen.items()}
    names.update(_name_aliases(conn))
    return names


def _seeds_without_target(conn: sqlite3.Connection) -> dict[str, dict[str, tuple]]:
    """horse_data に無い馬（1995年前後の馬）は、名寄せキーから父と母の父だけを種にする。"""
    seeds = {}
    for horse_id, sire_key, sire, bms_key, bms in conn.execute(
            "SELECT horse_id, sire_key, sire, broodmare_sire_key, broodmare_sire FROM horses "
            "WHERE horse_id NOT IN (SELECT horse_id FROM target_horses)"):
        cells = {}
        if sire_key or sire:
            cells["f"] = (sire_key, sire)
        if bms_key or bms:
            cells["mf"] = (bms_key, bms)
        if cells:
            seeds[horse_id] = cells
    return seeds


@dataclass
class LocalResult:
    horses: int = 0
    cells: int = 0
    full: int = 0
    by_year: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    conflicts: int = 0
    resolved: Counter = field(default_factory=Counter)


def rebuild(conn: sqlite3.Connection, *, dry_run: bool = False) -> LocalResult:
    """source='local' の行を作り直す。netkeiba の血統表がある馬には触らない。"""
    trees = load_trees(conn)
    builder, conflicts = builder_from(conn, trees)
    if conflicts:
        logger.warning("繁殖登録番号→netkeiba番号の対応で、食い違う番号が%d件あり採りませんでした", conflicts)
    seeds = _seeds_without_target(conn)
    targets = [r[0] for r in conn.execute(
        "SELECT h.horse_id FROM horses h WHERE EXISTS (SELECT 1 FROM entries e WHERE e.horse_id = h.horse_id)")
        if r[0] not in trees and r[0][:4].isdigit()]
    result = LocalResult(conflicts=conflicts)
    rows: list[tuple] = []
    for horse_id in targets:
        cells = builder.build(horse_id, seeds.get(horse_id) if horse_id not in builder.target_cells else None)
        if not cells:
            continue
        result.horses += 1
        result.cells += len(cells)
        result.full += len(cells) == 62
        result.by_year[horse_id[:4]].append(len(cells))
        rows += [(horse_id, path, len(path), no, LOCAL) for path, no in cells.items()]
    result.resolved = builder.named
    logger.info("手元のデータで組んだ5代血統表: %s頭（62マスそろった%s頭）、平均%.1fマス。"
                "祖先の解決: 番号%s 馬名%s 番号のまま(jv:)%s",
                f"{result.horses:,}", f"{result.full:,}", result.cells / max(result.horses, 1),
                *(f"{builder.named[k]:,}" for k in ("number", "name", "jv")))
    for year in sorted(result.by_year):
        counts = result.by_year[year]
        logger.info("  %s年生: %s頭 平均%.1fマス 62マス%.0f%%", year, f"{len(counts):,}",
                    sum(counts) / len(counts), 100 * sum(c == 62 for c in counts) / len(counts))
    if dry_run:
        return result
    _save(conn, rows)
    return result


def _save(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    used = {r[3] for r in rows}
    jv_names = _jv_names(conn)
    known = {r[0] for r in conn.execute("SELECT horse_no FROM pedigree_horses")}
    horse_names = dict(conn.execute("SELECT horse_id, horse_name FROM target_horses WHERE horse_name IS NOT NULL"))
    horse_names.update({k: v for k, v in conn.execute(
        "SELECT horse_id, horse_name FROM horses WHERE horse_name IS NOT NULL") if k not in horse_names})
    new_ancestors = []
    for no in used - known:
        name = jv_names.get(no[3:]) if no.startswith("jv:") else horse_names.get(no)
        new_ancestors.append((no, name or no, None, db.now_str()))
    with conn:
        conn.execute("DELETE FROM horse_ancestors WHERE source = ?", (LOCAL,))
        conn.executemany("INSERT INTO pedigree_horses (horse_no, name, country, updated_at) VALUES (?, ?, ?, ?)",
                         new_ancestors)
        conn.executemany("INSERT INTO horse_ancestors (horse_id, path, generation, ancestor_no, source) "
                         "VALUES (?, ?, ?, ?, ?)", rows)
    logger.info("horse_ancestors（local）に%s行、pedigree_horses に祖先%s頭を足しました",
                f"{len(rows):,}", f"{len(new_ancestors):,}")


def holdout_check(conn: sqlite3.Connection, fraction: float = 0.1) -> dict[str, float]:
    """netkeiba の血統表がある馬の一部を外して組み直し、netkeiba と比べる（DBは変えない）。"""
    trees = load_trees(conn)
    held = {h for h in trees if zlib.crc32(h.encode()) % 1000 < fraction * 1000}
    builder, _ = builder_from(conn, {h: t for h, t in trees.items() if h not in held})
    filled = agree = 0
    for horse_id in held:
        if horse_id not in builder.target_cells:
            continue
        cells = builder.build(horse_id)
        truth = trees[horse_id]
        for path, no in cells.items():
            if path in truth and not no.startswith("jv:"):
                filled += 1
                agree += truth[path] == no
    stats = {"horses": len(held), "cells_compared": filled, "agreement": agree / max(filled, 1)}
    logger.info("取り置き検証: %s頭を外して組み直し、%sマスを比べて一致率%.2f%%",
                f"{len(held):,}", f"{filled:,}", 100 * stats["agreement"])
    return stats
