"""種牡馬（父・母の父）の名寄せ（`stallions` / `stallion_names` / `horses.sire_key`）。

同じ種牡馬が、表記でも番号でも割れている。

- 表記: 本体（JRA公式・netkeiba）はカナ、Target は外国産を英字で書く。
  2023年以降の馬でも、輸入された母の父は英字のことがある（`Sunday Silence`）
- 番号: Target の繁殖登録番号は、国内で供用された種牡馬（`112…`）と、
  外国産馬の血統に出てくる海外の記録（`114…`）で別になる（サンデーサイレンス = 1120001232 / 1140004339）

そこで「同じ馬の父（母の父）として、この番号とこの名前が一緒に出てきた」を辺にして
union-find でまとめる。**国内番号が2つ入る結合は同名異馬とみなして行わない。**
キーは国内番号（無ければいちばん小さい番号）。名前で引けるので、Target に無い馬にも付く。
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# (target_horses の番号の列, target_horses の名前の列, 同じ立場の horses の列)
ROLES = (
    ("sire_breed_no", "sire_name", "sire"),
    ("bms_breed_no", "bms_name", "broodmare_sire"),
    ("sire_bms_breed_no", "sire_bms_name", None),
    ("dam_dam_sire_breed_no", "dam_dam_sire_name", None),
)


def is_domestic(breed_no: str) -> bool:
    """国内で供用された種牡馬の繁殖登録番号か（`112…`）。海外の記録は `114…`。"""
    return breed_no[2:3] == "2"


@dataclass
class _Group:
    breed_nos: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)

    @property
    def domestic(self) -> set[str]:
        return {n for n in self.breed_nos if is_domestic(n)}


class SireGrouper:
    """(番号, 名前) の辺を受け取って、同じ馬どうしをまとめる。"""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.groups: dict[str, _Group] = {}
        self.refused: list[tuple[str, str]] = []

    def _find(self, node: str) -> str:
        if node not in self.parent:
            self.parent[node] = node
            group = _Group()
            (group.breed_nos if node.startswith("#") else group.names).add(node[1:])
            self.groups[node] = group
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != root:
            self.parent[node], node = root, self.parent[node]
        return root

    def add(self, breed_no: str, name: str) -> bool:
        a, b = self._find("#" + breed_no), self._find("@" + name)
        if a == b:
            return True
        ga, gb = self.groups[a], self.groups[b]
        if ga.domestic and gb.domestic and ga.domestic != gb.domestic:
            self.refused.append((breed_no, name))
            return False
        self.parent[b] = a
        ga.breed_nos |= gb.breed_nos
        ga.names |= gb.names
        del self.groups[b]
        return True

    def result(self) -> list[_Group]:
        return [g for g in self.groups.values() if g.breed_nos]


def sire_key_of(group: _Group) -> str:
    return min(group.domestic or group.breed_nos)


def canonical_name(names: set[str], recent: Counter, total: Counter) -> str:
    """2023年以降の馬が使う名前（JRAの表記）→ 使われた回数の多い名前 の順で代表を選ぶ。"""
    return min(names, key=lambda n: (-recent[n], -total[n], n))


def _edges(conn: sqlite3.Connection):
    """(番号, 名前) の辺。Target 自身の表記と、本体（horses）での同じ馬の表記の両方。"""
    for no, tname, hcol in ROLES:
        yield from conn.execute(
            f"SELECT DISTINCT {no}, {tname} FROM target_horses WHERE {no} IS NOT NULL AND {tname} IS NOT NULL")
        if hcol:
            yield from conn.execute(
                f"SELECT DISTINCT t.{no}, h.{hcol} FROM target_horses t JOIN horses h USING (horse_id) "
                f"WHERE t.{no} IS NOT NULL AND h.{hcol} IS NOT NULL AND h.{hcol} <> ''")


def _usage(conn: sqlite3.Connection) -> tuple[Counter, Counter]:
    recent, total = Counter(), Counter()
    for col in ("sire", "broodmare_sire"):
        for name, n_recent, n_total in conn.execute(
                f"SELECT {col}, SUM(source = 'scrape'), COUNT(*) FROM horses "
                f"WHERE {col} IS NOT NULL AND {col} <> '' GROUP BY {col}"):
            recent[name] += n_recent or 0
            total[name] += n_total
    return recent, total


def rebuild_sire_keys(conn: sqlite3.Connection) -> dict[str, int]:
    """stallions / stallion_names を作り直し、horses.sire_key / broodmare_sire_key を全件引き直す。"""
    grouper = SireGrouper()
    for breed_no, name in _edges(conn):
        grouper.add(breed_no, name)
    for breed_no, name in grouper.refused:
        logger.warning("名寄せ: %s と %s は国内番号の違う別の束に入るため、つなぎませんでした（同名異馬の疑い）",
                       breed_no, name)
    recent, total = _usage(conn)
    stallions: list[tuple[str, str]] = []
    owner_of: dict[str, tuple[str, int]] = {}          # 名前 → (キー, その束での使われた回数)
    for group in grouper.result():
        key = sire_key_of(group)
        stallions.append((key, canonical_name(group.names, recent, total) if group.names else key))
        for name in group.names:
            known = owner_of.get(name)
            if known is None or total[name] > known[1]:
                owner_of[name] = (key, total[name])
    with conn:
        conn.execute("DELETE FROM stallion_names")
        conn.execute("DELETE FROM stallions")
        conn.executemany("INSERT INTO stallions (sire_key, name) VALUES (?, ?)", stallions)
        conn.executemany("INSERT INTO stallion_names (name, sire_key) VALUES (?, ?)",
                         [(name, key) for name, (key, _) in owner_of.items()])
        n_sire = conn.execute(
            "UPDATE horses SET sire_key = (SELECT sire_key FROM stallion_names s WHERE s.name = horses.sire)"
        ).rowcount
        n_bms = conn.execute(
            "UPDATE horses SET broodmare_sire_key = "
            "(SELECT sire_key FROM stallion_names s WHERE s.name = horses.broodmare_sire)"
        ).rowcount
    multi = sum(1 for g in grouper.result() if len(g.names) > 1)
    keyed = conn.execute("SELECT SUM(sire_key IS NOT NULL), SUM(broodmare_sire_key IS NOT NULL), "
                         "SUM(sire IS NOT NULL AND sire <> ''), "
                         "SUM(broodmare_sire IS NOT NULL AND broodmare_sire <> '') FROM horses").fetchone()
    logger.info("名寄せ: 種牡馬%s頭（別表記をまとめた%s頭）、つながなかった辺%s。"
                "キーが付いた馬 父%s/%s 母父%s/%s", f"{len(stallions):,}", multi, len(grouper.refused),
                f"{keyed[0] or 0:,}", f"{keyed[2] or 0:,}", f"{keyed[1] or 0:,}", f"{keyed[3] or 0:,}")
    return {"stallions": len(stallions), "multi_name": multi, "refused": len(grouper.refused),
            "horses": max(n_sire, n_bms)}


def fill_missing_sire_keys(conn: sqlite3.Connection) -> int:
    """キーがまだ無い馬（scrape で新しく入った馬）だけ、名前から引いて埋める。軽いので update のたびに呼べる。"""
    with conn:
        n = conn.execute(
            "UPDATE horses SET sire_key = (SELECT sire_key FROM stallion_names s WHERE s.name = horses.sire) "
            "WHERE sire_key IS NULL AND sire IN (SELECT name FROM stallion_names)").rowcount
        n += conn.execute(
            "UPDATE horses SET broodmare_sire_key = "
            "(SELECT sire_key FROM stallion_names s WHERE s.name = horses.broodmare_sire) "
            "WHERE broodmare_sire_key IS NULL AND broodmare_sire IN (SELECT name FROM stallion_names)").rowcount
    return n
