"""出馬表ページ（race.netkeiba.com/race/shutuba.html?race_id=...）のパース。

未開催レースの出走表を取るためのモジュール。2026-09-20 産経賞オールカマー
（race_id=202606040611）の実HTMLで構造を確認済み。

- ログイン不要、静的HTML、UTF-8
- レース名: `h1.RaceName`、グレードは<title>の「(G2)」表記から取る
  （見出し側はアイコン画像のクラス名でしか表現されていないため）
- コース: `.RaceData01`（例:「15:45発走 / 芝2200m (右 外 C)」）
- 開催・条件: `.RaceData02`（例:「4回 中山 6日目 サラ系３歳以上 オープン (国際)(指) 別定 13頭」）
- 出走馬: `tr.HorseList`。ただし同じページには「AI予測ラップ」用の表も
  `Shutuba_Table` というクラスで存在するため、`td.HorseInfo` を持つ表だけを使う
- 斤量のtdには区別できるクラスが無いので、`td.Barei`（性齢）の次のtdを位置で取る
- **枠順確定前**（出走登録の段階）は、枠番・馬番のtdが空欄のまま馬名・騎手・斤量だけが入る。
  この段階も出走予定馬として保存できるよう、主キーには馬番ではなく並び順(seq)を使う
- 障害レースは競馬場によって「障2880m」とも「ダ2880m」とも表記されるため、
  レース名に「障害」が含まれていれば surface を jump に補正する
- **オッズ・人気はJavaScriptで後から読み込まれるため静的HTMLには無い**（`---.-`, `**`）。
  馬体重も当日発表で、それまでは空。いずれもこのパーサでは取得できない
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

from keiba_data import parsing
from keiba_data.race_id import decode_race_id
from keiba_data.scrapers import LayoutError

_SURFACE_MAP = {"芝": "turf", "ダ": "dirt", "障": "jump"}
_DIRECTION_MAP = {"右": "right", "左": "left", "直": "straight"}
_GRADE_MAP = {"G1": "GI", "G2": "GII", "G3": "GIII", "JG1": "JGI", "JG2": "JGII", "JG3": "JGIII"}
_ZEN_TO_HAN = str.maketrans("０１２３４５６７８９", "0123456789")

_COURSE_RE = re.compile(r"(?:(?P<post_time>\d{1,2}:\d{2})発走)?.*?(?P<surface>芝|ダ|障)(?P<distance>\d+)m(?:\s*\((?P<detail>[^)]*)\))?")
_TITLE_GRADE_RE = re.compile(r"\((J\.?G[123]|G[123])\)")
_TITLE_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_N_ENTRIES_RE = re.compile(r"(\d+)頭")
_AGE_RE = re.compile(r"(\d+歳(?:以上)?)")
_BRACKET_RE = re.compile(r"[\[(]([^\])]+)[\])]")
_CANCEL_RE = re.compile(r"取消|除外")
_KAISAI_RE = re.compile(r"\d+回|\d+日目")
_WEIGHT_RULES = ("別定", "定量", "馬齢", "ハンデ")


@dataclass
class ShutubaPage:
    """出馬表1件のパース結果。各dictのキーはDBの列名に対応する。"""

    race: dict
    entries: list[dict]
    warnings: list[str] = field(default_factory=list)


def _text(tag: Tag | None) -> str:
    return parsing.normalize_space(tag.get_text(" ", strip=True)) if tag else ""


def _link_id(td: Tag | None, pattern: re.Pattern) -> tuple[str | None, str | None]:
    if td is None:
        return None, None
    for a in td.find_all("a", href=True):
        m = pattern.search(a["href"])
        if m:
            name = a.get("title") or a.get_text(strip=True)
            return m.group(1), parsing.normalize_space(name) or None
    return None, _text(td) or None


_HORSE_RE = re.compile(r"/horse/([0-9A-Za-z]+)")
_JOCKEY_RE = re.compile(r"/jockey/(?:result/recent/)?([0-9A-Za-z]+)")
_TRAINER_RE = re.compile(r"/trainer/(?:result/recent/)?([0-9A-Za-z]+)")


def _entries_table(soup: BeautifulSoup) -> Tag | None:
    """出走馬テーブルを返す。AI予測ラップ用の同クラスの表は除外する。"""
    for table in soup.select("table.Shutuba_Table"):
        if table.select_one("td.HorseInfo"):
            return table
    return None


def _parse_race_meta(soup: BeautifulSoup, race_id: str, race_date: str | None, warnings: list[str]) -> dict:
    info = decode_race_id(race_id)
    title = _text(soup.select_one("title"))

    race_name = _text(soup.select_one("h1.RaceName")) or None
    grade = None
    if gm := _TITLE_GRADE_RE.search(title):
        grade = _GRADE_MAP.get(gm.group(1).replace(".", ""))
    if race_date is None and (dm := _TITLE_DATE_RE.search(title)):
        race_date = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    if race_date is None:
        warnings.append("開催日が特定できません（<title>から取得できず、呼び出し側からも渡されていません）")

    race = {
        "race_id": race_id,
        "race_date": race_date,
        "venue_code": info.venue_code,
        "kaiji": info.kaiji,
        "nichime": info.nichime,
        "race_no": info.race_no,
        "race_name": race_name,
        "grade": grade,
        "surface": None,
        "direction": None,
        "distance_m": None,
        "course_detail": None,
        "post_time": None,
        "age_condition": None,
        "class_condition": None,
        "race_conditions": None,
        "n_entries": None,
    }

    course_text = _text(soup.select_one(".RaceData01"))
    if cm := _COURSE_RE.search(course_text):
        race["surface"] = _SURFACE_MAP.get(cm.group("surface"))
        race["distance_m"] = int(cm.group("distance"))
        race["post_time"] = cm.group("post_time")
        detail = parsing.normalize_space(cm.group("detail") or "")
        for ch, direction in _DIRECTION_MAP.items():
            if ch in detail:
                race["direction"] = direction
                detail = detail.replace(ch, "", 1)
                break
        race["course_detail"] = parsing.normalize_space(detail) or None
    else:
        warnings.append(f"コース表記(.RaceData01)をパースできません: {course_text!r}")

    # 中山の障害レースは出馬表では「ダ2880m」と表記される（発走・決勝が
    # ダートコース上のため）。レース名に「障害」とあれば障害として扱う。
    if race["race_name"] and "障害" in race["race_name"]:
        race["surface"] = "jump"

    data02 = soup.select_one(".RaceData02")
    if data02 is None:
        warnings.append("開催・条件(.RaceData02)が見つかりません")
    else:
        spans = [parsing.normalize_space(s.get_text(" ", strip=True)) for s in data02.find_all("span")]
        spans = [s for s in spans if s]
        conditions: list[str] = []
        for span in spans:
            if span.startswith("本賞金") or _KAISAI_RE.fullmatch(span) or span == info.venue_name:
                continue  # 「4回」「中山」「6日目」はrace_idから分かるので読み飛ばす
            if am := _AGE_RE.search(span.translate(_ZEN_TO_HAN)):
                race["age_condition"] = am.group(1)  # 「サラ系３歳以上」→「3歳以上」
            elif nm := _N_ENTRIES_RE.fullmatch(span):
                race["n_entries"] = int(nm.group(1))
            elif brackets := _BRACKET_RE.findall(span):
                conditions.extend(brackets)  # 「(国際)(指)」
            elif span in _WEIGHT_RULES:
                conditions.append(span)  # 別定/定量/馬齢/ハンデ
            elif race["class_condition"] is None:
                race["class_condition"] = span  # オープン/3勝クラス/未勝利 等
        race["race_conditions"] = ",".join(conditions) or None
    _fill_condition_from_name(race)
    return race


# レース名がそのまま条件になっているときに拾うクラス（長いものから見る）
_CLASS_IN_NAME = ("3勝クラス", "2勝クラス", "1勝クラス", "未勝利", "新馬", "オープン")


def _fill_condition_from_name(race: dict) -> None:
    """条件が読めなかったとき、レース名から年齢とクラスを補う。

    枠順確定前のページでは `.RaceData02` の中身が空のことがあり、そのときは
    年齢・クラス・付記がまとめて取れない。ただし `3歳以上1勝クラス` `2歳新馬` の
    ように**レース名がそのまま条件になっている**レースなら、そこから読める
    （名前の付いたレースは確定前でも `.RaceData02` から取れている）。

    ページから読めた値のほうが正確なので、**空のときだけ**埋める。
    """
    name = parsing.normalize_space(race.get("race_name") or "").translate(_ZEN_TO_HAN)
    if not name:
        return
    if race.get("class_condition") is None:
        for klass in _CLASS_IN_NAME:
            if klass in name:
                race["class_condition"] = klass
                break
    if race.get("age_condition") is None and (am := _AGE_RE.search(name)):
        race["age_condition"] = am.group(1)


def _parse_entry_row(row: Tag, race_id: str, seq: int, warnings: list[str]) -> dict | None:
    # 枠順確定前（出走登録の段階）は枠番・馬番が空欄のまま出走馬だけが並ぶ
    umaban = parsing.to_int(_text(row.select_one('td[class*="Umaban"]')))

    barei_td = row.select_one("td.Barei")
    sex, age = parsing.parse_sex_age(_text(barei_td))
    kinryo_td = barei_td.find_next_sibling("td") if barei_td else None
    horse_id, horse_name = _link_id(row.select_one("td.HorseInfo"), _HORSE_RE)
    jockey_id, jockey_name = _link_id(row.select_one("td.Jockey"), _JOCKEY_RE)
    trainer_td = row.select_one("td.Trainer")
    trainer_id, trainer_name = _link_id(trainer_td, _TRAINER_RE)
    stable_span = trainer_td.select_one('span[class*="Label"]') if trainer_td else None
    weight_kg, weight_diff = parsing.parse_weight(_text(row.select_one("td.Weight")))

    row_text = _text(row)
    status = None
    if "Cancel" in " ".join(row.get("class", [])) or _CANCEL_RE.search(row_text):
        cm = _CANCEL_RE.search(row_text)
        status = cm.group(0) if cm else "取消"

    if horse_id is None and horse_name:
        warnings.append(f"{horse_name}の馬IDが取得できません")

    if horse_id is None and not horse_name:
        warnings.append(f"馬名も馬IDも取れない行があります（{seq}行目）")
        return None

    return {
        "race_id": race_id,
        "seq": seq,
        "umaban": umaban,
        "waku": parsing.to_int(_text(row.select_one('td[class*="Waku"]'))),
        "horse_id": horse_id,
        "horse_name": horse_name,
        "sex": sex,
        "age": age,
        "kinryo": parsing.to_float(_text(kinryo_td)),
        "jockey_id": jockey_id,
        "jockey_name": jockey_name,
        "trainer_id": trainer_id,
        "trainer_name": trainer_name,
        "stable": _text(stable_span) or None,
        "horse_weight": weight_kg,
        "weight_diff": weight_diff,
        "status": status,
    }


def parse_shutuba(html: str, race_id: str, race_date: str | None = None) -> ShutubaPage | None:
    """出馬表ページをパースする。

    出馬表がまだ公開されていない（枠順未確定・存在しないrace_id）場合は None を返す。
    ページはあるのに構造が想定と違う場合は LayoutError を送出する。
    """
    soup = BeautifulSoup(html, "lxml")
    table = _entries_table(soup)
    header = soup.select_one("h1.RaceName")
    if table is None:
        return None
    if header is None:
        raise LayoutError("出走馬テーブルはあるのにレース名(h1.RaceName)が見つかりません")

    warnings: list[str] = []
    race = _parse_race_meta(soup, race_id, race_date, warnings)

    entries = []
    for seq, row in enumerate(table.select("tr.HorseList"), start=1):
        entry = _parse_entry_row(row, race_id, seq, warnings)
        if entry is not None:
            entries.append(entry)
    if not entries:
        raise LayoutError("出走馬テーブルから1頭も取得できません")

    n_listed = race.get("n_entries")
    if n_listed is not None and n_listed != len(entries):
        warnings.append(f"ページ記載の頭数({n_listed})と出走馬の行数({len(entries)})が一致しません")
    race["n_entries"] = len(entries)

    return ShutubaPage(race=race, entries=entries, warnings=warnings)
