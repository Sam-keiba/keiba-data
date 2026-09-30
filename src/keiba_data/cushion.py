"""JRA公式のクッション値・含水率アーカイブPDFの取得と解析。

出典: https://www.jra.go.jp/keiba/baba/archive/{年}.html（robots.txtは全許可）
PDFは開催が終わってから公開されるため、直近の開催は空になる（時間が経てば埋まる）。

PDFのレイアウトは2種類ある（実データで確認済み）。

**2025年以降（`_parse_pdf_v2025`）**: pdfplumberのextract_tables()で取れる表形式。列（0始まり）:
    0: 開催日次（例 "第 1日"。空欄の行は開催前日＝金曜の測定）
    1: 測定月日（例 "3月27日"。年はPDFの置き場所のフォルダ名から補う）
    3: 使用コース（芝の内外・移動柵設定 A/B/C）
    5: 芝クッション値
    7,8: 含水率 芝（ゴール前 / 4コーナー）
    9,10: 含水率 ダート（ゴール前 / 4コーナー）

**2024年以前（`_parse_pdf_legacy`）**: テキストブロック形式。
    第N日・第M日（YYYY年M月D日～D日）
    金曜日 土曜日 日曜日          ← 列数 = 前日測定1列 + 開催日数
    芝コースクッション値 v1 v2 v3
    場所 金曜日 土曜日 日曜日
    芝コース含水率 ゴール前 v1 v2 v3
    （パーセント） ４コーナー v1 v2 v3
    ダートコース含水率 ゴール前 v1 v2 v3
    （パーセント） ４コーナー v1 v2 v3
使用コース・測定時刻はこの形式には無い（None固定）。

クッション値の公表は2020年に始まったため、それ以前のPDFは含水率のみ、
または解析結果が0件になることがある（このプロジェクトのDBは2023年以降なので支障はない）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup

from keiba_data import config
from keiba_data.http import PoliteSession

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://www.jra.go.jp/keiba/baba/archive/{year}.html"
_BASE_URL = "https://www.jra.go.jp"

# PDFでは「2月 1日」のように月と日の間に空白が入ることがある
_DATE_RE = re.compile(r"(\d{1,2})月\s*(\d{1,2})日")
_NICHIME_RE = re.compile(r"第\s*(\d+)\s*日")
_LEGACY_HEADER_RE = re.compile(
    r"(第[0-9０-９・第日]+)（(\d{4})年(\d{1,2})月(\d{1,2})日[～〜\-](?:(\d{1,2})月)?(\d{1,2})日）"
)
_NUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_PDF_NAME_RE = re.compile(r"([a-z]+)(\d{2})\.pdf$")
_ZEN_TO_HAN = str.maketrans("０１２３４５６７８９", "0123456789")


@dataclass(frozen=True)
class TrackCondition:
    """ある競馬場・ある日の馬場情報（1日1行）。"""

    venue_code: str | None
    date: str  # "YYYY-MM-DD"
    nichime: int | None  # 開催◯日目（前日測定や不明時はNone）
    day_label: str | None
    is_pre_meeting_day: bool  # 開催前日（金曜）の測定か
    course_setting: str | None  # 芝の使用コース（A/B/C等）。2024年以前は常にNone
    cushion_value: float | None
    turf_moisture_goal: float | None
    turf_moisture_4corner: float | None
    dirt_moisture_goal: float | None
    dirt_moisture_4corner: float | None
    source_pdf: str
    measured_at: str | None = None  # 測定日時（馬場情報ページ由来のときだけ入る）
    going_turf: str | None = None  # 当日発表の馬場状態（芝）。馬場情報ページ由来
    going_dirt: str | None = None  # 当日発表の馬場状態（ダート）
    weather: str | None = None  # 当日発表の天候


def _to_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_nichime(day_label: str | None) -> int | None:
    if not day_label:
        return None
    m = _NICHIME_RE.search(day_label)
    return int(m.group(1)) if m else None


def venue_code_from_filename(name: str) -> tuple[str, int] | None:
    """"nakayama03.pdf" を ("06", 3) に変換する。JRA10場以外は None。"""
    m = _PDF_NAME_RE.search(name)
    if not m:
        return None
    venue_code = config.VENUE_ENGLISH_TO_CODE.get(m.group(1))
    return (venue_code, int(m.group(2))) if venue_code else None


# --- PDFの解析 ---------------------------------------------------------------


def _looks_like_v2025(path: Path) -> bool:
    """extract_tables()に「開催日次」ヘッダーがあれば2025年以降の形式。"""
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if any((cell or "").strip() == "開催日次" for cell in row):
                        return True
    return False


def _parse_pdf_v2025(path: Path, venue_code: str | None, year: int) -> list[TrackCondition]:
    records: list[TrackCondition] = []
    prev_month: int | None = None
    year_adj = year

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if len(row) < 11:
                        continue
                    date_m = _DATE_RE.match((row[1] or "").strip())
                    if not date_m:
                        continue  # ヘッダー行など、データ行ではない
                    month, day = int(date_m.group(1)), int(date_m.group(2))
                    if prev_month is not None and month < prev_month:
                        year_adj += 1  # 年末年始をまたぐ開催
                    prev_month = month

                    day_label = (row[0] or "").strip() or None
                    records.append(
                        TrackCondition(
                            venue_code=venue_code,
                            date=f"{year_adj:04d}-{month:02d}-{day:02d}",
                            nichime=_extract_nichime(day_label),
                            day_label=day_label,
                            is_pre_meeting_day=day_label is None,
                            course_setting=(row[3] or "").strip() or None,
                            cushion_value=_to_float(row[5]),
                            turf_moisture_goal=_to_float(row[7]),
                            turf_moisture_4corner=_to_float(row[8]),
                            dirt_moisture_goal=_to_float(row[9]),
                            dirt_moisture_4corner=_to_float(row[10]),
                            source_pdf=path.name,
                        )
                    )
    return records


def _parse_pdf_legacy(path: Path, venue_code: str | None) -> list[TrackCondition]:
    records: list[TrackCondition] = []

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            lines = [ln.strip() for ln in (page.extract_text() or "").splitlines() if ln.strip()]
            i = 0
            while i < len(lines):
                m = _LEGACY_HEADER_RE.search(lines[i])
                if not m:
                    i += 1
                    continue
                if i + 7 >= len(lines):
                    break  # ブロックが尻切れ（想定外のレイアウト）

                labels_raw, year_s, month1_s, day1_s, _end_month_s, _end_day_s = m.groups()
                start_date = date(int(year_s), int(month1_s), int(day1_s))

                n_cols = len(lines[i + 1].split())  # 曜日の数 = 前日測定 + 開催日数
                dates = [start_date + timedelta(days=d) for d in range(n_cols)]

                cushion_vals = _NUM_RE.findall(lines[i + 2])
                turf_goal_vals = _NUM_RE.findall(lines[i + 4])
                turf_4c_vals = _NUM_RE.findall(lines[i + 5])
                dirt_goal_vals = _NUM_RE.findall(lines[i + 6])
                dirt_4c_vals = _NUM_RE.findall(lines[i + 7])

                nichime_labels = [
                    int(n.translate(_ZEN_TO_HAN)) for n in re.findall(r"第([0-9０-９]+)日", labels_raw)
                ]
                if len(nichime_labels) == n_cols - 1:
                    is_pre = [True] + [False] * (n_cols - 1)
                    nichimes: list[int | None] = [None, *nichime_labels]
                elif len(nichime_labels) == n_cols:
                    is_pre = [False] * n_cols
                    nichimes = list(nichime_labels)
                else:
                    is_pre = [False] * n_cols
                    nichimes = [None] * n_cols

                def at(values: list[str], idx: int) -> float | None:
                    return _to_float(values[idx]) if idx < len(values) else None

                for idx, d in enumerate(dates):
                    nichime = nichimes[idx] if idx < len(nichimes) else None
                    records.append(
                        TrackCondition(
                            venue_code=venue_code,
                            date=d.isoformat(),
                            nichime=nichime,
                            day_label=f"第{nichime}日" if nichime else None,
                            is_pre_meeting_day=is_pre[idx] if idx < len(is_pre) else False,
                            course_setting=None,
                            cushion_value=at(cushion_vals, idx),
                            turf_moisture_goal=at(turf_goal_vals, idx),
                            turf_moisture_4corner=at(turf_4c_vals, idx),
                            dirt_moisture_goal=at(dirt_goal_vals, idx),
                            dirt_moisture_4corner=at(dirt_4c_vals, idx),
                            source_pdf=path.name,
                        )
                    )
                i += 8  # このブロック（曜日+クッション+場所+含水率4行）を消費

    return records


def parse_pdf(path: Path, year: int | None = None) -> list[TrackCondition]:
    """1開催ぶんのPDFを日付ごとのレコードに変換する（形式は自動判定）。"""
    venue = venue_code_from_filename(path.name)
    venue_code = venue[0] if venue else None
    year = year or int(path.parent.name)
    if _looks_like_v2025(path):
        return _parse_pdf_v2025(path, venue_code, year)
    return _parse_pdf_legacy(path, venue_code)


# --- PDFのダウンロード ---------------------------------------------------------


def parse_archive_links(html: str) -> list[str]:
    """アーカイブページから、その年のPDFのURL一覧を取り出す。"""
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for a in soup.select('a[href$=".pdf"]'):
        href = a["href"]
        url = href if href.startswith("http") else _BASE_URL + href
        if url not in urls:
            urls.append(url)
    return urls


def year_dir(year: int) -> Path:
    return config.CUSHION_ARCHIVE_ROOT / str(year)


def local_pdfs(year: int) -> list[Path]:
    directory = year_dir(year)
    return sorted(directory.glob("*.pdf")) if directory.is_dir() else []


def download_missing_pdfs(session: PoliteSession, year: int) -> list[Path]:
    """その年のアーカイブページを見て、手元に無いPDFだけ保存する。保存したファイルを返す。"""
    html = session.get_text(ARCHIVE_URL.format(year=year), encoding="utf-8")
    if html is None:
        logger.warning("%d年のアーカイブページが見つかりません", year)
        return []

    urls = parse_archive_links(html)
    if not urls:
        logger.warning("%d年のアーカイブページにPDFのリンクがありません（構造変化の可能性）", year)
        return []

    directory = year_dir(year)
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for url in urls:
        path = directory / url.rsplit("/", 1)[-1]
        if path.exists():
            continue
        content = session.get_bytes(url)
        if content is None:
            continue
        path.write_bytes(content)
        saved.append(path)
        logger.info("ダウンロード: %s", path.name)
    logger.info("%d年: PDF %d件中 %d件を新しく取得しました", year, len(urls), len(saved))
    return saved
