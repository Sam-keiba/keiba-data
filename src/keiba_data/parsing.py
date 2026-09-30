"""スクレイパーで共有するテキスト→データ型変換ユーティリティ。"""

from __future__ import annotations

import re

# 馬体重のセル。netkeibaは "498(+4)"、JRA公式は "498kg (+4)" や "456kg (初出走)" と書く。
# 括弧の中が数字でないとき（初出走・前計不）は増減なしとして体重だけを読む。
_WEIGHT_RE = re.compile(
    r"^(\d+)\s*(?:kg|Kg|KG)?\s*(?:[(（]\s*(?:(?P<diff>[+-]?\d+)|[^)）]*?)\s*[)）])?$"
)
_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+(?:\.\d+)?)$")
_SEX_AGE_RE = re.compile(r"^(牡|牝|セ|騸)(\d+)$")


def normalize_space(text: str | None) -> str:
    """&nbsp;や連続空白を1つの半角スペースにまとめる。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def parse_weight(text: str | None) -> tuple[int | None, int | None]:
    """馬体重セルを (体重, 増減) に分解する。未計測("計不"等)は (None, None)。

    netkeibaの `"498(+4)"` と、JRA公式の `"498kg (+4)"` `"456kg (初出走)"` の
    どちらも読む。初出走のように増減が書かれていないときは増減を `None` にする
    （増減0とは区別する）。
    """
    if not text:
        return None, None
    text = normalize_space(text)
    m = _WEIGHT_RE.match(text)
    if not m:
        return to_int(text), None
    diff = m.group("diff")
    return int(m.group(1)), int(diff) if diff is not None else None


def parse_time_to_seconds(text: str | None) -> float | None:
    """"2:21.8" や "58.3" のようなタイム文字列を秒(float)に変換する。"""
    if not text:
        return None
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    minutes = int(m.group(1)) if m.group(1) else 0
    return round(minutes * 60 + float(m.group(2)), 1)


def parse_sex_age(text: str | None) -> tuple[str | None, int | None]:
    """"牡3" を ("牡", 3) に分解する。"""
    if not text:
        return None, None
    m = _SEX_AGE_RE.match(text.strip())
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def to_int(text: str | None) -> int | None:
    """数値文字列をintに変換する（カンマ区切り可）。変換できなければNone。"""
    if text is None:
        return None
    text = text.strip().replace(",", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def to_float(text: str | None) -> float | None:
    """数値文字列をfloatに変換する（カンマ区切り可）。"---"等の変換できない値はNone。"""
    if text is None:
        return None
    text = text.strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def lap_rows(race_id: str, distance_m: int, lap_secs: list[float], warnings: list[str]) -> list[dict]:
    """ハロンタイムの並びを race_laps の行にする（netkeibaとJRAで共通）。

    `distance_m` は **スタートからの累計**にする。先頭区間は距離を200で割った余り
    （例: 2500mなら最初の100m）、以降は200mずつ。ここの決めごとが2つの取り込み経路で
    食い違うと、ダッシュボードの1Fペース計算（`sum(lap_sec)*200/max(distance_m)`）が狂う。
    """
    first = distance_m % 200 or 200
    expected = 1 + (distance_m - first) // 200
    if len(lap_secs) != expected:
        warnings.append(
            f"ラップ数({len(lap_secs)})が距離{distance_m}mから想定される区間数({expected})と一致しません"
        )
    rows = []
    cumulative = 0
    for seq, lap in enumerate(lap_secs, start=1):
        cumulative += first if seq == 1 else 200
        rows.append({"race_id": race_id, "seq": seq, "distance_m": cumulative, "lap_sec": lap})
    return rows
