"""開催カレンダー（race.netkeiba.com/top/calendar.html?year=Y&month=M）のパース。

開催がある日のセル（td.RaceCellBox）だけに `race_list.html?kaisai_date=YYYYMMDD` への
リンクが張られている（2023年1月・2026年9月のページで確認済み）。
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from keiba_data.scrapers import LayoutError

_KAISAI_DATE_RE = re.compile(r"kaisai_date=(\d{4})(\d{2})(\d{2})")


def parse_calendar(html: str) -> list[date]:
    """カレンダーページから開催日の一覧（昇順・重複なし）を返す。"""
    soup = BeautifulSoup(html, "lxml")
    cells = soup.select("td.RaceCellBox")
    if not cells:
        raise LayoutError("カレンダーの日付セル(td.RaceCellBox)が見つかりません")

    dates: set[date] = set()
    for cell in cells:
        for a in cell.select("a[href]"):
            m = _KAISAI_DATE_RE.search(a["href"])
            if m:
                dates.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return sorted(dates)
