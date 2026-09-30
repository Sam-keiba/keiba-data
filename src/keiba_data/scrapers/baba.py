"""JRAの馬場情報ページから、**当日の**クッション値と含水率を読む。

出典: https://www.jra.go.jp/keiba/baba/ （robots.txtは全許可）

ページ本体は値が空で、JavaScriptが次の2つを読み込んで埋めている。
どちらも素のHTML（Shift_JIS）なので、そのまま解析できる。

    /keiba/baba/_data_cushion.html … 競馬場ごとの測定時刻とクッション値
    /keiba/baba/_data_moist.html   … 競馬場ごとの含水率（芝・ダートのゴール前/4コーナー）

cushion.py が読む**アーカイブPDFは開催後の公開**なので、直近の開催（今週・前週）は
そちらでは埋まらない。この2つのHTMLはその期間を含むので、当日の値が取れる。

HTMLの形（実際の中身）:
    <div id="rcA" title="中山">
      <div class="unit">
        <div class="time">9月20日（日曜）7時00分</div>
        <div class="cushion">10.0</div>
      </div>
      …
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

from bs4 import BeautifulSoup

from keiba_data.scrapers import LayoutError

# 「9月20日（日曜）7時00分」から 月日と時刻を取り出す
_TIME_RE = re.compile(r"(\d{1,2})月\s*(\d{1,2})日.*?(\d{1,2})時\s*(\d{1,2})分")
# 表記に年が無いので、基準日からこれ以上先になる日付は「前年」とみなす
_FUTURE_TOLERANCE_DAYS = 60


@dataclass(frozen=True)
class CourseCondition:
    """当日の開催情報（馬場状態・天候）。JRAの `accessJ.html` が返すJSONから。"""

    venue_name: str
    date: str  # "YYYY-MM-DD"
    going_turf: str | None = None
    going_dirt: str | None = None
    weather: str | None = None


def parse_course_conditions(text: str) -> list[CourseCondition]:
    """当日の馬場状態・天候のJSONを読む。

    馬場情報ページが `POST /JRADB/accessJ.html`（`CNAME=pw01iwtS3/CD`）で読んでいる
    もので、**その日開催している全場ぶん**が1リクエストで返る。中身はこの形:

        {"kaisai_info": [{"jyoname": "阪神", "kaisai_ymd": "20260921",
                          "weather": "晴", "ba_s": "良", "ba_d": "良", ...}]}

    レース結果にも馬場状態は載っているが、それは**走り終えたレースの分**なので、
    まだ1レースも終わっていない時間帯はこちらでしか分からない。
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LayoutError(f"当日の馬場状態のJSONを読めません: {exc}") from exc
    conditions = []
    for row in data.get("kaisai_info") or []:
        ymd = str(row.get("kaisai_ymd") or "")
        venue = (row.get("jyoname") or "").strip()
        if len(ymd) != 8 or not venue:
            continue
        conditions.append(CourseCondition(
            venue_name=venue,
            date=f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}",
            going_turf=(row.get("ba_s") or "").strip() or None,
            going_dirt=(row.get("ba_d") or "").strip() or None,
            weather=(row.get("weather") or "").strip() or None,
        ))
    return conditions


@dataclass(frozen=True)
class BabaMeasurement:
    """ある競馬場・ある測定時刻の馬場情報。"""

    venue_name: str
    date: str  # "YYYY-MM-DD"
    measured_at: str  # "YYYY-MM-DD HH:MM"
    cushion_value: float | None = None
    turf_moisture_goal: float | None = None
    turf_moisture_4corner: float | None = None
    dirt_moisture_goal: float | None = None
    dirt_moisture_4corner: float | None = None


def _resolve_date(month: int, day: int, today: date) -> date:
    """年の無い「9月20日」を日付にする。基準日より大きく先なら前年とみなす。"""
    try:
        value = date(today.year, month, day)
    except ValueError:  # 2月29日など、その年に無い日
        return date(today.year - 1, month, day)
    if value - today > timedelta(days=_FUTURE_TOLERANCE_DAYS):
        return date(today.year - 1, month, day)
    return value


def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.strip())
    except ValueError:
        return None


def _venue_blocks(html: str, container_id: str) -> list:
    """競馬場ごとのかたまり（`<div id="rcA" title="中山">`）を返す。"""
    soup = BeautifulSoup(html, "lxml")
    container = soup.find(id=container_id)
    if container is None:
        raise LayoutError(f"{container_id} が見つかりません（ページの作りが変わった可能性）")
    blocks = [d for d in container.find_all("div", recursive=False) if d.get("title")]
    if not blocks:
        raise LayoutError(f"{container_id} の中に競馬場のブロックがありません")
    return blocks


def _measured(unit, today: date) -> tuple[str, str] | None:
    """`<div class="time">` から (日付, 測定日時) を返す。読めなければ None。"""
    time_div = unit.find("div", class_="time")
    if time_div is None:
        return None
    matched = _TIME_RE.search(time_div.get_text(strip=True))
    if matched is None:
        return None
    month, day, hour, minute = (int(x) for x in matched.groups())
    measured_date = _resolve_date(month, day, today)
    return measured_date.isoformat(), f"{measured_date.isoformat()} {hour:02d}:{minute:02d}"


def parse_cushion(html: str, today: date) -> list[BabaMeasurement]:
    """クッション値のページを読む（競馬場 × 測定時刻）。"""
    results = []
    for block in _venue_blocks(html, "cushion_data_list"):
        venue = block["title"].strip()
        for unit in block.find_all("div", class_="unit"):
            when = _measured(unit, today)
            value = unit.find("div", class_="cushion")
            if when is None or value is None:
                continue
            cushion = _to_float(value.get_text(strip=True))
            if cushion is None:
                continue
            results.append(BabaMeasurement(venue, when[0], when[1], cushion_value=cushion))
    if not results:
        raise LayoutError("クッション値が1件も読めませんでした")
    return results


def parse_moisture(html: str, today: date) -> list[BabaMeasurement]:
    """含水率のページを読む（芝・ダートのゴール前と4コーナー）。"""
    results = []
    for block in _venue_blocks(html, "moist_data_list"):
        venue = block["title"].strip()
        for unit in block.find_all("div", class_="unit"):
            when = _measured(unit, today)
            if when is None:
                continue
            values = {}
            for surface in ("turf", "dirt"):
                area = unit.find("div", class_=surface)
                if area is None:
                    continue
                # mg=ゴール前、m4c=4コーナー
                values[f"{surface}_goal"] = _to_float(
                    area.find("span", class_="mg").get_text(strip=True) if area.find("span", class_="mg") else None
                )
                values[f"{surface}_4corner"] = _to_float(
                    area.find("span", class_="m4c").get_text(strip=True) if area.find("span", class_="m4c") else None
                )
            if not values:
                continue
            results.append(BabaMeasurement(
                venue, when[0], when[1],
                turf_moisture_goal=values.get("turf_goal"),
                turf_moisture_4corner=values.get("turf_4corner"),
                dirt_moisture_goal=values.get("dirt_goal"),
                dirt_moisture_4corner=values.get("dirt_4corner"),
            ))
    if not results:
        raise LayoutError("含水率が1件も読めませんでした")
    return results


def latest_by_day(measurements: list[BabaMeasurement]) -> dict[tuple[str, str], BabaMeasurement]:
    """(競馬場名, 日付) -> その日の**いちばん新しい測定**。

    同じ日に複数回測ることがある（開催日の朝と正午など）ので、遅い時刻を採る。
    """
    latest: dict[tuple[str, str], BabaMeasurement] = {}
    for measurement in measurements:
        key = (measurement.venue_name, measurement.date)
        current = latest.get(key)
        if current is None or measurement.measured_at > current.measured_at:
            latest[key] = measurement
    return latest
