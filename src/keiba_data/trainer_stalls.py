"""調教師別の貸付馬房数を、人が確かめたCSVから取り込む（`keiba-data trainer-stalls-import`）。

JRAは毎年2〜3月に「○月○日からの調教師別貸付馬房数」を美浦・栗東のPDFで出す。
PDFはスキャン画像で文字のデータが無いので、読み取ったCSVを人が確かめてから入れる。

## CSVの形（1行1調教師。見出し行あり・UTF-8）

    effective_date,stable,name,stalls
    2026-03-04,美浦,国枝 栄,20

発表の名前は正式名でIDが無いので、`trainers` と**所属（美浦/栗東）＋名前**で結びつける:
1. 空白を除いた正式名（名鑑から入れた `full_name`。`keiba-data trainer-meikan`）
2. 1で決まらなければ、先頭4文字（netkeibaの `trainer_name` は4文字で切れている）で1人に決まるとき
結びつかない名前は取り込まずに返す（呼び手が一覧で出す）。
同じ日付・所属を入れ直すと、その行はCSVの中身で置き換わる（美浦と栗東は別のファイルでもよい）。
"""

from __future__ import annotations

import csv
import logging
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from keiba_data import db

logger = logging.getLogger(__name__)

COLUMNS = ("effective_date", "stable", "name", "stalls")
STABLES = ("美浦", "栗東")
# 発表と名鑑・netkeibaで字体が揺れる字（髙橋／高橋 など）
_VARIANTS = str.maketrans({"髙": "高", "﨑": "崎", "德": "徳", "邊": "辺", "邉": "辺", "濵": "浜", "瀨": "瀬"})


@dataclass
class StallsImport:
    rows: int = 0
    saved: int = 0
    unmatched: list[dict] = field(default_factory=list)
    totals: dict[tuple[str, str], int] = field(default_factory=dict)   # (日付, 所属) → 馬房数の合計（照合前）


def name_key(name: str | None) -> str:
    """照合用に、全角・半角と空白、字体の揺れをならす。"""
    return "".join(unicodedata.normalize("NFKC", name or "").split()).translate(_VARIANTS)


def _read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSVに列がありません: {', '.join(missing)}（必要な列: {', '.join(COLUMNS)}）")
        rows = []
        for n, row in enumerate(reader, 2):
            name = (row["name"] or "").strip()
            if not name:
                continue
            stable = (row["stable"] or "").strip()
            if stable not in STABLES:
                raise ValueError(f"{path.name} {n}行目: 所属は美浦か栗東です: {stable!r}")
            try:
                stalls = int(row["stalls"])
            except (TypeError, ValueError):
                raise ValueError(f"{path.name} {n}行目: 馬房数が数ではありません: {row['stalls']!r}") from None
            effective = (row["effective_date"] or "").strip()
            try:
                date.fromisoformat(effective)
            except ValueError:
                raise ValueError(f"{path.name} {n}行目: 日付はYYYY-MM-DDです: {effective!r}") from None
            rows.append({"effective_date": effective, "stable": stable,
                         "name": name, "stalls": stalls})
    return rows


def _matcher(conn: sqlite3.Connection):
    """(所属, 名前) → trainer_id を引く関数を作る。"""
    by_full: dict[tuple[str, str], set[str]] = {}
    by_short: dict[tuple[str, str], set[str]] = {}
    for r in conn.execute("SELECT trainer_id, trainer_name, full_name, stable FROM trainers "
                          "WHERE stable IN ('美浦', '栗東')"):
        if r["full_name"]:
            by_full.setdefault((r["stable"], name_key(r["full_name"])), set()).add(r["trainer_id"])
        if r["trainer_name"]:
            by_short.setdefault((r["stable"], name_key(r["trainer_name"])), set()).add(r["trainer_id"])

    def match(stable: str, name: str) -> str | None:
        key = name_key(name)
        found = by_full.get((stable, key))
        if not found:
            found = by_short.get((stable, key[:4]))
        return next(iter(found)) if found and len(found) == 1 else None

    return match


def import_csv(conn: sqlite3.Connection, path: Path) -> StallsImport:
    """CSVを1つ取り込む。結びつかない行は入れずに `unmatched` で返す。"""
    rows = _read_rows(path)
    result = StallsImport(rows=len(rows))
    match = _matcher(conn)
    matched: list[tuple] = []
    seen: dict[tuple[str, str], str] = {}
    ts = db.now_str()
    for row in rows:
        key = (row["effective_date"], row["stable"])
        result.totals[key] = result.totals.get(key, 0) + row["stalls"]
        trainer_id = match(row["stable"], row["name"])
        if trainer_id is None:
            result.unmatched.append({**row, "reason": "該当なし・同名が複数"})
            continue
        if (trainer_id, row["effective_date"]) in seen:
            result.unmatched.append({**row, "reason": f"{seen[(trainer_id, row['effective_date'])]} と同じ調教師に結びついた"})
            continue
        seen[(trainer_id, row["effective_date"])] = row["name"]
        matched.append((trainer_id, row["effective_date"], row["stalls"], row["stable"], row["name"], ts))

    dates = sorted({r["effective_date"] for r in rows})
    with conn:
        conn.executemany("DELETE FROM trainer_stalls WHERE effective_date = ? AND stable = ?",
                         sorted(result.totals))
        conn.executemany(
            "INSERT INTO trainer_stalls (trainer_id, effective_date, stalls, stable, name_in_source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", matched)
    result.saved = len(matched)
    logger.info("貸付馬房数 %s（%s）: %d行中 %d人を保存、結びつかない %d行",
                path.name, "・".join(dates), result.rows, result.saved, len(result.unmatched))
    return result
