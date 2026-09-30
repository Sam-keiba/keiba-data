"""クッション値PDFの解析とアーカイブページのリンク抽出。"""

import pytest

from keiba_data import config, cushion, db

V2025_PDF = config.CUSHION_ARCHIVE_ROOT / "2026" / "nakayama03.pdf"
LEGACY_PDF = config.CUSHION_ARCHIVE_ROOT / "2023" / "tokyo02.pdf"

ARCHIVE_HTML = """
<html><body>
  <a href="/keiba/baba/archive/2026pdf/sapporo01.pdf">札幌1回</a>
  <a href="/keiba/baba/archive/2026pdf/nakayama03.pdf">中山3回</a>
  <a href="/keiba/baba/archive/2026pdf/nakayama03.pdf">重複</a>
  <a href="https://www.jra.go.jp/keiba/baba/archive/2026pdf/tokyo01.pdf">東京1回</a>
  <a href="/keiba/index.html">PDFではないリンク</a>
</body></html>
"""


def test_parse_archive_links():
    urls = cushion.parse_archive_links(ARCHIVE_HTML)
    assert urls == [
        "https://www.jra.go.jp/keiba/baba/archive/2026pdf/sapporo01.pdf",
        "https://www.jra.go.jp/keiba/baba/archive/2026pdf/nakayama03.pdf",
        "https://www.jra.go.jp/keiba/baba/archive/2026pdf/tokyo01.pdf",
    ]


def test_venue_code_from_filename():
    assert cushion.venue_code_from_filename("nakayama03.pdf") == ("06", 3)
    assert cushion.venue_code_from_filename("hanshin01.pdf") == ("09", 1)
    assert cushion.venue_code_from_filename("kawasaki01.pdf") is None  # 地方競馬場
    assert cushion.venue_code_from_filename("readme.txt") is None


@pytest.mark.skipif(not V2025_PDF.exists(), reason="クッション値PDF（2025年以降形式）が無い")
def test_parse_pdf_v2025_layout():
    records = cushion.parse_pdf(V2025_PDF, 2026)
    assert records
    assert all(r.venue_code == "06" for r in records)
    assert all(len(r.date) == 10 for r in records)
    assert any(r.is_pre_meeting_day for r in records)  # 開催前日（金曜）の測定行
    race_days = [r for r in records if not r.is_pre_meeting_day]
    assert race_days and all(r.nichime for r in race_days)
    first = records[0]
    assert first.cushion_value is not None
    assert first.turf_moisture_goal is not None and first.dirt_moisture_goal is not None
    assert first.course_setting  # 2025年以降は使用コース（A/B/C）が入る
    assert first.source_pdf == "nakayama03.pdf"


@pytest.mark.skipif(not LEGACY_PDF.exists(), reason="クッション値PDF（2024年以前形式）が無い")
def test_parse_pdf_legacy_layout():
    records = cushion.parse_pdf(LEGACY_PDF, 2023)
    assert records
    assert all(r.venue_code == "05" for r in records)
    assert any(r.cushion_value is not None for r in records)
    assert all(r.course_setting is None for r in records)  # この形式には使用コースが無い
    dates = [r.date for r in records]
    assert dates == sorted(dates)


@pytest.mark.skipif(not V2025_PDF.exists(), reason="クッション値PDFが無い")
def test_upsert_is_idempotent(conn):
    records = cushion.parse_pdf(V2025_PDF, 2026)
    db.upsert_track_conditions(conn, records)
    first = db.table_counts(conn)["track_conditions"]
    db.upsert_track_conditions(conn, records)
    assert db.table_counts(conn)["track_conditions"] == first == len(records)


def test_upsert_skips_records_without_venue(conn):
    record = cushion.TrackCondition(
        venue_code=None, date="2026-01-01", nichime=1, day_label="第1日", is_pre_meeting_day=False,
        course_setting="A", cushion_value=9.0, turf_moisture_goal=None, turf_moisture_4corner=None,
        dirt_moisture_goal=None, dirt_moisture_4corner=None, source_pdf="x.pdf",
    )
    assert db.upsert_track_conditions(conn, [record]) == 0
    assert db.table_counts(conn)["track_conditions"] == 0
