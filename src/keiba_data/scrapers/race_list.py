"""開催日ごとのレース一覧（race.netkeiba.com/top/race_list_sub.html?kaisai_date=YYYYMMDD）のパース。

各レースは `li.RaceList_DataItem` 内の `result.html?race_id=...`（開催前は `shutuba.html?race_id=...`）
へのリンクとして並んでいる（2023年1月5日・2026年9月13日のページで確認済み）。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from keiba_data.race_id import is_jra_race_id

_RACE_ID_RE = re.compile(r"race_id=(\d{12})")


def parse_race_list(html: str) -> list[str]:
    """レース一覧ページからJRAのrace_idの一覧（昇順・重複なし）を返す。

    レースが1件も無い場合は空リストを返す（警告を出すかどうかは呼び出し側が判断する）。
    """
    soup = BeautifulSoup(html, "lxml")
    race_ids: set[str] = set()
    for a in soup.select("li.RaceList_DataItem a[href]"):
        m = _RACE_ID_RE.search(a["href"])
        if m and is_jra_race_id(m.group(1)):
            race_ids.add(m.group(1))
    return sorted(race_ids)


_RACE_NO_RE = re.compile(r"(\d+)R")
_N_ENTRIES_RE = re.compile(r"(\d+)頭")


def parse_race_list_details(html: str) -> list[dict]:
    """レース一覧から、レース選択に使う情報（R番号・レース名・発走時刻・コース・頭数）を取り出す。

    未開催日・開催済みの日のどちらでも同じ構造で並んでいる（リンク先が
    shutuba.html か result.html かだけが違う）。ここで取れる情報を使うと、
    出馬表ページを取りに行かなくてもレース選択の一覧が作れる。
    """
    soup = BeautifulSoup(html, "lxml")
    races: dict[str, dict] = {}
    for item in soup.select("li.RaceList_DataItem"):
        link = item.select_one("a[href]")
        if link is None:
            continue
        m = _RACE_ID_RE.search(link["href"])
        if not m or not is_jra_race_id(m.group(1)):
            continue
        race_id = m.group(1)
        if race_id in races:
            continue
        num_text = item.select_one(".Race_Num")
        time_text = item.select_one(".RaceList_Itemtime")
        course_text = item.select_one(".RaceList_ItemLong") or item.select_one(".RaceData span:not([class])")
        n_text = item.select_one(".RaceList_Itemnumber")
        title = item.select_one(".ItemTitle")
        races[race_id] = {
            "race_id": race_id,
            "race_no": int(_RACE_NO_RE.search(num_text.get_text()).group(1)) if num_text and _RACE_NO_RE.search(num_text.get_text()) else None,
            "race_name": title.get_text(strip=True) if title else None,
            "post_time": time_text.get_text(strip=True) or None if time_text else None,
            "course_text": course_text.get_text(strip=True) or None if course_text else None,
            "n_entries": int(_N_ENTRIES_RE.search(n_text.get_text()).group(1)) if n_text and _N_ENTRIES_RE.search(n_text.get_text()) else None,
        }
    return [races[k] for k in sorted(races)]
