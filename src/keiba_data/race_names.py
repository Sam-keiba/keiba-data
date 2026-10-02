"""付記の無いレース名（`races.race_name_plain`）。

`races.race_name` は出どころで書き方が違う。

- netkeiba（2023年以降）: `第57回スプリンターズS` `アクアマリンS(3勝)` `3歳以上1勝クラス`
- Target（1995〜2022年）: 略称で、しかも**名前が切り詰められている**。
  `スプリンG1` `アクアH1600` `500万下・牝*`

どちらも回次・格・クラスの付記を落とし、Target の略称は 2023年以降の同じレースの名前に寄せる。
Target の略称を元に戻す手がかりは手元に無いので、**同じ競馬場・同じ馬場・同じ格で、
前方一致する2023年以降の名前が1つだけ**のときにその名前を採り、それ以外は付記を落とした略称のまま残す。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from collections import defaultdict

logger = logging.getLogger(__name__)

# --- netkeiba（2023年以降） --------------------------------------------------------

_SCRAPE_PREFIX = re.compile(r"^第\d+回")
_SCRAPE_SUFFIX = re.compile(r"\((?:[1-3]勝|OP|L)\)$")
# netkeiba でも年によって `シリウスS` と `シリウスステークス` が混ざるので、短い方にそろえる
_SCRAPE_WORDS = ((re.compile(r"ステークス$"), "S"), (re.compile(r"カップ$"), "C"))


def plain_scrape_name(name: str | None) -> str | None:
    """`第57回スプリンターズS` → `スプリンターズS`、`アクアマリンS(3勝)` → `アクアマリンS`。

    `天皇賞(秋)` の `(秋)` は名前の一部なので残す。末尾の `ステークス` `カップ` は `S` `C` にそろえる。
    """
    if not name:
        return None
    text = _SCRAPE_SUFFIX.sub("", _SCRAPE_PREFIX.sub("", name.strip()))
    for pattern, short in _SCRAPE_WORDS:
        text = pattern.sub(short, text)
    return text or name.strip()


# --- Target（1995〜2022年） ---------------------------------------------------------

# 略称の末尾に付く付記。外側から順に何度でも落とす（keiba-analysis の race_name.py から移した）
_TARGET_SUFFIXES = (
    re.compile(r"\*$"),                                # 指定の印
    re.compile(r"・(?:牝|父|市|若|九|重|[1-3]勝)$"),   # 条件（牝馬限定・父内国産・市場取引馬・見習騎手…）
    re.compile(r"(?<=\D)(?:500|900|1000|1500|1600)$"),  # 旧クラスの金額（名前の後ろに付いたものだけ）
    re.compile(r"J?G[123]$"),                          # 格
    re.compile(r"\(L\)$"),                             # リステッド
)
_HANDICAP = re.compile(r"H$")                          # ハンデ戦の印（重量種別がハンデのときだけ落とす）

# 一般戦（固有の名前が無い）のクラス名。netkeiba と同じ `{年齢条件}{クラス}` の形に組み直す
_GENERIC = re.compile(r"^(障害)?・?(新馬|未出走|未勝利|オープ(?:ン)?|\d+万下|[1-3]勝クラス)$")

# 前方一致では引けないもの（略称 → 2023年以降の名前、または正式名）。
# キーは付記を落としたあとの略称
OVERRIDES: dict[str, str] = {
    "JC": "ジャパンC",
    "JCダート": "ジャパンCダート",
    "WASJ第1": "ワールドオールスタージョッキーズ第1戦",
    "WASJ第2": "ワールドオールスタージョッキーズ第2戦",
    "WASJ第3": "ワールドオールスタージョッキーズ第3戦",
    "WASJ第4": "ワールドオールスタージョッキーズ第4戦",
}


def _key(name: str) -> str:
    """突き合わせ用に表記をそろえる（全半角・ダッシュ・括弧の違いを消す）。"""
    text = unicodedata.normalize("NFKC", name)
    text = re.sub(r"[‐-‒–—―−ｰ-]", "-", text)
    return re.sub(r"[()（）・\s]", "", text)


def strip_target_suffixes(name: str | None, handicap: bool = False) -> str:
    """`皐月賞G1` → `皐月賞`、`アクアH1600` → `アクア`（ハンデ戦のとき）、`500万下・牝*` → `500万下`。"""
    text = unicodedata.normalize("NFKC", name or "").strip()
    changed = True
    while changed and text:
        changed = False
        patterns = _TARGET_SUFFIXES + ((_HANDICAP,) if handicap else ())
        for pattern in patterns:
            stripped = pattern.sub("", text)
            if stripped != text and stripped:
                text, changed = stripped, True
    return text


def generic_name(short: str, age_condition: str | None, class_condition: str | None,
                 surface: str | None) -> str | None:
    """一般戦なら netkeiba と同じ書き方（`3歳以上500万下` `障害4歳以上未勝利`）を返す。固有名なら None。"""
    m = _GENERIC.match(short)
    if not m:
        return None
    jump = bool(m[1]) or surface == "jump"
    cls = (class_condition or "").split(" ")[0] or m[2]
    if cls.startswith("オープ"):
        cls = "OP"
    return f"{'障害' if jump else ''}{age_condition or ''}{cls}"


def _expand(short: str, surface: str | None) -> str:
    """切り詰めでよく欠ける語尾を補う（`紫野特` → `紫野特別`、障害の `清秋ジャ` → `清秋ジャンプS`）。"""
    if short.endswith("特"):
        return short + "別"
    if surface == "jump":
        return re.sub(r"ジャ?$", "ジャンプS", short)
    return short


def match_modern(short: str, candidates: list[str]) -> str | None:
    """2023年以降の名前のうち、略称を含むものが**1つだけ**なら、略称が始まる位置から後ろを返す。

    netkeiba の名前には冠（`MBS賞スワンS` `日刊スポシンザン記念`）が付くので前方一致では引けない。
    冠は年によって変わるので落とす（`シンザン` → `シンザン記念`）。冠だけが違う名前は1つと数える。
    略称と同じ名前があればそれを採る（`京成杯` を `京成杯オータムH` と取り違えない）。
    括弧・ダッシュの違い（`天皇賞秋` と `天皇賞(秋)`）は表記をそろえて前方一致で見る。
    """
    text = unicodedata.normalize("NFKC", short)
    key = _key(short)
    if not key:
        return None
    hits = set()
    for c in candidates:
        norm = unicodedata.normalize("NFKC", c)
        at = norm.find(text)
        if at >= 0:
            hits.add(norm[at:])
        elif _key(c).startswith(key):
            hits.add(norm)
    if text in hits:
        return text
    return hits.pop() if len(hits) == 1 else None


def _match_stages(short: str, stages: list[list[str]]) -> str | None:
    """候補を狭い順に試す。当たりが2つ以上の段で止める（広げるほど取り違えやすいので）。"""
    for candidates in stages:
        text = unicodedata.normalize("NFKC", short)
        key = _key(short)
        n = sum(1 for c in candidates if text in unicodedata.normalize("NFKC", c) or _key(c).startswith(key))
        if n == 0:
            continue
        return match_modern(short, candidates)
    return None


def _wasj(short: str) -> str | None:
    m = re.match(r"^WASJ第(\d)", short)
    return OVERRIDES.get(f"WASJ第{m[1]}") if m else None


def target_plain_name(short: str, *, handicap: bool, age_condition: str | None, class_condition: str | None,
                      surface: str | None, stages: list[list[str]]) -> tuple[str, str]:
    """Target の略称 → (付記の無い名前, どう決めたか)。どう決めたか: generic/override/matched/stripped。

    `stages` は2023年以降の名前の候補を狭い順に並べたもの（`candidate_stages` が作る）。
    """
    base = strip_target_suffixes(short, handicap=handicap)
    generic = generic_name(base, age_condition, class_condition, surface)
    if generic:
        return generic, "generic"
    override = OVERRIDES.get(base) or _wasj(base)
    if override:
        return override, "override"
    matched = _match_stages(_expand(base, surface), stages) or _match_stages(base, stages)
    if matched is None and not handicap and base.endswith("H"):
        matched = _match_stages(base[:-1], stages)
    if matched:
        return matched, "matched"
    return _expand(base, surface), "stripped"


# --- DBへの反映 ---------------------------------------------------------------------


def refresh_scrape_plain_names(conn: sqlite3.Connection, only_missing: bool = True) -> int:
    """netkeiba 由来の行の race_name_plain を埋める（`only_missing` なら空のものだけ）。"""
    where = "source = 'scrape'" + (" AND race_name_plain IS NULL" if only_missing else "")
    rows = [(plain_scrape_name(r[1]), r[0]) for r in conn.execute(f"SELECT race_id, race_name FROM races WHERE {where}")]
    conn.executemany("UPDATE races SET race_name_plain = ? WHERE race_id = ?", rows)
    return len(rows)


class CandidatePool:
    """2023年以降のレース名（付記なし）を、競馬場・馬場・格で引けるようにしたもの。"""

    def __init__(self, rows):
        self.by: dict[tuple, set[str]] = defaultdict(set)
        for venue, surface, grade, name in rows:
            for key in ((venue, surface, grade), (surface, grade), (surface,)):
                self.by[key].add(name)

    def stages(self, venue: str | None, surface: str | None, grade: str | None) -> list[list[str]]:
        """同じ競馬場・馬場・格 → 同じ馬場・格 → 同じ馬場 の順（開催替えと、L格ができる前の年のため）。"""
        keys = ((venue, surface, grade), (surface, grade), (surface,))
        return [sorted(self.by.get(k, ())) for k in keys]


def modern_pool(conn: sqlite3.Connection) -> CandidatePool:
    return CandidatePool(conn.execute(
        "SELECT venue_code, surface, grade, race_name_plain FROM races "
        "WHERE source = 'scrape' AND race_name_plain IS NOT NULL"))


def refresh_target_plain_names(conn: sqlite3.Connection) -> dict[str, int]:
    """Target 由来の行の race_name_plain を作り直す。決め方ごとの件数を返す。"""
    refresh_scrape_plain_names(conn)
    pool = modern_pool(conn)
    counts: dict[str, int] = defaultdict(int)
    rows = []
    for r in conn.execute("""
            SELECT ra.race_id, ra.venue_code, ra.surface, ra.grade, ra.age_condition, ra.class_condition,
                   COALESCE(t.race_name_short, ra.race_name) AS short, t.weight_type_code
              FROM races ra LEFT JOIN target_races t USING (race_id)
             WHERE ra.source = 'target'"""):
        name, how = target_plain_name(
            r["short"] or "", handicap=r["weight_type_code"] == "1", age_condition=r["age_condition"],
            class_condition=r["class_condition"], surface=r["surface"],
            stages=pool.stages(r["venue_code"], r["surface"], r["grade"]))
        counts[how] += 1
        rows.append((name or None, r["race_id"]))
    conn.executemany("UPDATE races SET race_name_plain = ? WHERE race_id = ?", rows)
    logger.info("race_name_plain（Target由来）: 一般戦%s 対応表%s 2023年以降の名前に寄せた%s 略称のまま%s",
                *(f"{counts[k]:,}" for k in ("generic", "override", "matched", "stripped")))
    return dict(counts)
