"""レース結果ページ（db.netkeiba.com/race/{race_id}/）のパース。

実際のHTMLで構造を確認済み（2023年1月中山金杯・2024年1月中山開催・2026年9月セントライト記念）。

- ログイン不要、静的HTML、euc-jp
- レース見出し: `.data_intro` の `dt`(レース番号)・`h1`(レース名+グレード)・
  `dd p span`(「芝右 外2200m / 天候 : 曇 / 芝 : 稍重 / 発走 : 15:45」)
- 開催日・条件: `p.smalltxt`（「2026年09月13日 4回中山4日目 3歳オープン (国際)(指)(馬齢)」）
- 結果テーブル `table.race_table_01` は thead/tbody を持たず、一部の列が非標準タグ
  `<diary_snap_cut>` で囲まれているため、`find_all("th"/"td")` を再帰的に呼んで
  見出しとセルの位置を揃える。列の位置は決め打ちせず見出し名で特定する
- 着順列: 数字 / 「取」(取消) / 「除」(除外) / 「中」(競走中止) / 「失」(失格) / 「3(降)」(降着)
- 払戻: `table.pay_table_01`（1券種1行、同着・複勝・ワイドは <br> 区切りで複数）
- ラップ: `table[summary="ラップタイム"]` の「ラップ」行（障害レースは空）

このパーサは出走表（枠・馬番・馬・騎手・調教師・馬主・斤量・馬体重）と
結果（着順・タイム・オッズ等）の両方を1ページから取り出す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

from keiba_data import parsing
from keiba_data.race_id import decode_race_id
from keiba_data.scrapers import LayoutError

REQUIRED_HEADERS = (
    "着順", "枠番", "馬番", "馬名", "性齢", "斤量", "騎手", "タイム", "着差",
    "単勝", "人気", "馬体重", "調教師",
)
OPTIONAL_HEADERS = ("通過", "上り", "馬主", "賞金(万円)")

FINISH_STATUS_MAP = {"取": "取消", "除": "除外", "中": "中止", "失": "失格"}
STABLE_MAP = {"東": "美浦", "西": "栗東", "地": "地方", "外": "海外"}
BET_TYPES = ("単勝", "複勝", "枠連", "馬連", "ワイド", "馬単", "三連複", "三連単")

_SURFACE_MAP = {"芝": "turf", "ダ": "dirt", "障": "jump"}
_DIRECTION_MAP = {"右": "right", "左": "left", "直": "straight"}

_ID_RE = {
    "horse": re.compile(r"/horse/([0-9A-Za-z]+)/?"),
    "jockey": re.compile(r"/jockey/(?:result/recent/)?([0-9A-Za-z]+)/?"),
    "trainer": re.compile(r"/trainer/(?:result/recent/)?([0-9A-Za-z]+)/?"),
    "owner": re.compile(r"/owner/(?:result/recent/)?([0-9A-Za-z]+)/?"),
}
_COURSE_RE = re.compile(r"^(芝|ダ|障)(.*?)(\d+)m$")
_WEATHER_RE = re.compile(r"^天候\s*[:：]\s*(\S+)$")
_GOING_RE = re.compile(r"^(芝|ダート)\s*[:：]\s*(\S+)$")
_POST_TIME_RE = re.compile(r"^発走\s*[:：]\s*(\d{1,2}:\d{2})$")
_GRADE_RE = re.compile(r"\((J\.?G(?:III|II|I)|G(?:III|II|I)|L)\)\s*$")
_SMALLTXT_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d+)回(\S+?)(\d+)日目\s*(.*)$")
_AGE_RE = re.compile(r"^(障害)?\s*(\d+歳(?:以上)?)")
_BRACKET_RE = re.compile(r"[\[(]([^\])]+)[\])]")
_FINISH_RE = re.compile(r"^(\d+)")
_STABLE_RE = re.compile(r"\[(\S)\]")


@dataclass
class RaceResultPage:
    """結果ページ1件のパース結果。各dictのキーはDBの列名に対応する。"""

    race: dict
    entries: list[dict]
    results: list[dict]
    payouts: list[dict]
    laps: list[dict]
    warnings: list[str] = field(default_factory=list)


def _text(tag: Tag | None) -> str:
    return parsing.normalize_space(tag.get_text(" ", strip=True)) if tag else ""


def _link_id(td: Tag | None, kind: str) -> tuple[str | None, str | None]:
    """セル内の <a> から (ID, 表示名) を取り出す。リンクが無ければ (None, セルのテキスト or None)。"""
    if td is None:
        return None, None
    for a in td.find_all("a", href=True):
        m = _ID_RE[kind].search(a["href"])
        if m:
            return m.group(1), a.get("title") or a.get_text(strip=True) or None
    return None, _text(td) or None


def _parse_course(text: str, warnings: list[str]) -> dict:
    """「芝右 外2200m / 天候 : 曇 / 芝 : 稍重 / 発走 : 15:45」を分解する。"""
    out = {
        "surface": None, "direction": None, "distance_m": None, "course_detail": None,
        "weather": None, "going_turf": None, "going_dirt": None, "post_time": None,
    }
    parts = [p.strip() for p in text.split("/") if p.strip()]
    if not parts:
        warnings.append("コース情報(.data_intro dd p span)が見つかりません")
        return out

    cm = _COURSE_RE.match(parts[0])
    if cm:
        out["surface"] = _SURFACE_MAP[cm.group(1)]
        out["distance_m"] = int(cm.group(3))
        middle = cm.group(2)
        if "直線" in middle:
            out["direction"] = "straight"
            middle = middle.replace("直線", "")
        else:
            for ch, direction in _DIRECTION_MAP.items():
                if ch in middle:
                    out["direction"] = direction
                    middle = middle.replace(ch, "", 1)
                    break
        out["course_detail"] = parsing.normalize_space(middle) or None
    else:
        warnings.append(f"コース表記をパースできません: {parts[0]!r}")

    for part in parts[1:]:
        if m := _WEATHER_RE.match(part):
            out["weather"] = m.group(1)
        elif m := _GOING_RE.match(part):
            out["going_turf" if m.group(1) == "芝" else "going_dirt"] = m.group(2)
        elif m := _POST_TIME_RE.match(part):
            out["post_time"] = m.group(1)
    return out


def _parse_smalltxt(text: str, warnings: list[str]) -> dict:
    out = {
        "race_date": None, "kaiji": None, "nichime": None,
        "age_condition": None, "class_condition": None, "race_conditions": None,
        "condition_raw": text or None,
    }
    m = _SMALLTXT_RE.match(text)
    if not m:
        warnings.append(f"開催日・条件(p.smalltxt)をパースできません: {text!r}")
        return out
    y, mo, d, kaiji, _venue, nichime, remainder = m.groups()
    out["race_date"] = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    out["kaiji"] = int(kaiji)
    out["nichime"] = int(nichime)

    rest = remainder
    if am := _AGE_RE.match(rest):
        out["age_condition"] = am.group(2)
        rest = rest[am.end():]
    brackets = _BRACKET_RE.findall(rest)
    out["race_conditions"] = ",".join(brackets) if brackets else None
    out["class_condition"] = parsing.normalize_space(_BRACKET_RE.sub("", rest)) or None
    return out


def _parse_race_meta(soup: BeautifulSoup, race_id: str, warnings: list[str]) -> dict:
    info = decode_race_id(race_id)

    name_raw = _text(soup.select_one(".data_intro h1"))
    grade = None
    race_name = name_raw or None
    if gm := _GRADE_RE.search(name_raw):
        grade = gm.group(1).replace(".", "")
        race_name = name_raw[: gm.start()].strip() or None

    race = {
        "race_id": race_id,
        "venue_code": info.venue_code,
        "race_no": info.race_no,
        "race_name": race_name,
        "grade": grade,
    }
    race.update(_parse_course(_text(soup.select_one(".data_intro dd p span")), warnings))
    race.update(_parse_smalltxt(_text(soup.select_one("p.smalltxt")), warnings))

    # ページ内の開催回・日目がrace_idと食い違う場合は構造変化かID体系の変化を疑う
    if race["kaiji"] is not None and (race["kaiji"], race["nichime"]) != (info.kaiji, info.nichime):
        warnings.append(
            f"ページの開催回/日目({race['kaiji']}回{race['nichime']}日目)がrace_idと一致しません"
        )
    # race_idから確実に分かる値を優先する
    race["kaiji"], race["nichime"] = info.kaiji, info.nichime
    return race


def _header_index_map(header_row: Tag) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for i, th in enumerate(header_row.find_all("th")):
        name = th.get_text(strip=True)
        mapping.setdefault(name, i)
    return mapping


def _parse_finish(text: str) -> tuple[int | None, str | None]:
    """着順セルを (着順, 状態) に分解する。状態は 取消/除外/中止/失格/降着/None。"""
    if not text:
        return None, None
    if text in FINISH_STATUS_MAP:
        return None, FINISH_STATUS_MAP[text]
    m = _FINISH_RE.match(text)
    position = int(m.group(1)) if m else None
    status = "降着" if "降" in text else None
    if position is None and status is None:
        status = text
    return position, status


def _parse_results_table(table: Tag, race_id: str, warnings: list[str]) -> tuple[list[dict], list[dict]]:
    rows = table.find_all("tr", recursive=False)
    if not rows:
        raise LayoutError("結果テーブルに行がありません")
    col = _header_index_map(rows[0])
    missing = [h for h in REQUIRED_HEADERS if h not in col]
    if missing:
        raise LayoutError(f"結果テーブルに必要な列がありません: {missing}")
    missing_optional = [h for h in OPTIONAL_HEADERS if h not in col]
    if missing_optional:
        warnings.append(f"結果テーブルに列がありません: {missing_optional}")

    entries: list[dict] = []
    results: list[dict] = []
    for row in rows[1:]:
        tds = row.find_all("td")
        if not tds:
            continue
        if len(tds) != len(rows[0].find_all("th")):
            warnings.append(f"結果テーブルの見出し数とセル数が一致しません (行{len(entries) + 1})")

        def cell(name: str) -> Tag | None:
            idx = col.get(name)
            return tds[idx] if idx is not None and idx < len(tds) else None

        def text(name: str) -> str:
            return _text(cell(name))

        umaban = parsing.to_int(text("馬番"))
        if umaban is None:
            warnings.append(f"馬番をパースできない行があります: {text('馬番')!r}")
            continue

        horse_id, horse_name = _link_id(cell("馬名"), "horse")
        jockey_id, jockey_name = _link_id(cell("騎手"), "jockey")
        trainer_id, trainer_name = _link_id(cell("調教師"), "trainer")
        owner_id, owner_name = _link_id(cell("馬主"), "owner")
        if horse_id is None:
            warnings.append(f"{umaban}番の馬IDが取得できません")
        stable_m = _STABLE_RE.search(text("調教師"))
        sex, age = parsing.parse_sex_age(text("性齢"))
        weight, weight_diff = parsing.parse_weight(text("馬体重"))
        finish_position, finish_status = _parse_finish(text("着順"))

        entries.append({
            "race_id": race_id,
            "umaban": umaban,
            "waku": parsing.to_int(text("枠番")),
            "horse_id": horse_id,
            "horse_name": horse_name,
            "sex": sex,
            "age": age,
            "kinryo": parsing.to_float(text("斤量")),
            "jockey_id": jockey_id,
            "jockey_name": jockey_name,
            "trainer_id": trainer_id,
            "trainer_name": trainer_name,
            "trainer_stable": STABLE_MAP.get(stable_m.group(1)) if stable_m else None,
            "owner_id": owner_id,
            "owner_name": owner_name,
            "horse_weight": weight,
            "weight_diff": weight_diff,
        })
        results.append({
            "race_id": race_id,
            "umaban": umaban,
            "horse_id": horse_id,
            "finish_position": finish_position,
            "finish_status": finish_status,
            "time_sec": parsing.parse_time_to_seconds(text("タイム")),
            "time_raw": text("タイム") or None,
            "margin": text("着差") or None,
            "corner_passing": text("通過") or None,
            "last_3f": parsing.to_float(text("上り")),
            "win_odds": parsing.to_float(text("単勝")),
            "popularity": parsing.to_int(text("人気")),
            "prize_man_yen": parsing.to_float(text("賞金(万円)")),
        })

    if not entries:
        raise LayoutError("結果テーブルから出走馬を1頭も取得できません")
    return entries, results


def _split_br(td: Tag) -> list[str]:
    return [s.strip() for s in td.get_text("\n").split("\n") if s.strip()]


def _parse_payouts(soup: BeautifulSoup, race_id: str, warnings: list[str]) -> list[dict]:
    tables = soup.select("table.pay_table_01")
    if not tables:
        warnings.append("払戻テーブル(table.pay_table_01)が見つかりません")
        return []
    payouts: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for table in tables:
        for tr in table.find_all("tr"):
            th = tr.find("th")
            tds = tr.find_all("td")
            if th is None or len(tds) < 3:
                continue
            bet_type = th.get_text(strip=True)
            if bet_type not in BET_TYPES:
                warnings.append(f"未知の券種です: {bet_type!r}")
                continue
            combos, amounts, pops = _split_br(tds[0]), _split_br(tds[1]), _split_br(tds[2])
            if len(combos) != len(amounts):
                warnings.append(f"{bet_type}の組番と払戻金の数が一致しません")
            for i, combo in enumerate(combos):
                combination = combo.replace(" ", "")
                if (bet_type, combination) in seen:
                    continue
                seen.add((bet_type, combination))
                payouts.append({
                    "race_id": race_id,
                    "bet_type": bet_type,
                    "combination": combination,
                    "payout_yen": parsing.to_int(amounts[i]) if i < len(amounts) else None,
                    "popularity": parsing.to_int(pops[i]) if i < len(pops) else None,
                })
    return payouts


def _parse_laps(soup: BeautifulSoup, race: dict, warnings: list[str]) -> list[dict]:
    lap_table = soup.find("table", attrs={"summary": "ラップタイム"})
    distance = race.get("distance_m")
    if lap_table is None or distance is None:
        return []
    for tr in lap_table.find_all("tr"):
        th = tr.find("th")
        td = tr.find("td")
        if th is None or td is None or th.get_text(strip=True) != "ラップ":
            continue
        values = [parsing.to_float(v) for v in td.get_text(strip=True).split("-")]
        if any(v is None for v in values):
            warnings.append(f"ラップをパースできません: {td.get_text(strip=True)!r}")
            return []
        return parsing.lap_rows(race["race_id"], distance, values, warnings)
    return []


def parse_race_result(html: str, race_id: str) -> RaceResultPage | None:
    """結果ページをパースする。

    結果がまだ掲載されていない（未確定・存在しないrace_id）ページの場合は None を返す。
    ページはあるのに構造が想定と違う場合は LayoutError を送出する。
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.race_table_01")
    intro = soup.select_one(".data_intro")
    if table is None and intro is None:
        return None
    if table is None or intro is None:
        raise LayoutError(
            "結果テーブル(table.race_table_01)とレース見出し(.data_intro)の片方しか見つかりません"
        )

    warnings: list[str] = []
    race = _parse_race_meta(soup, race_id, warnings)
    entries, results = _parse_results_table(table, race_id, warnings)
    race["n_runners"] = sum(1 for r in results if r["finish_status"] not in ("取消", "除外"))
    payouts = _parse_payouts(soup, race_id, warnings)
    laps = _parse_laps(soup, race, warnings) if race["surface"] != "jump" else []
    if race["surface"] != "jump" and not laps:
        warnings.append("ラップタイムが取得できません")

    return RaceResultPage(
        race=race, entries=entries, results=results, payouts=payouts, laps=laps, warnings=warnings
    )
