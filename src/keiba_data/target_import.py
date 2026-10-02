"""Target（TARGET frontier JV）から書き出したCSVの取り込み（`keiba-data target-import`）。

2段に分けて入れる。

1. `load_files()` … CSVを正規化して `target_races` / `target_runs` / `target_horses` に入れる
   （Targetの全列を残す層。PCI などTargetにしか無い指標はここにだけある）。
2. `merge()` … そこから本体（races/entries/results/horses/jockeys/trainers/owners）へ、
   **無い行を足し、空欄だけを埋める**。既存の値は上書きしない。埋めた欄は `target_fills` に残す。

キーは本体と同じ体系なので、名前で突き合わせることはしない
（2023-01〜2026-09の重複期間で、race_id・馬番・血統登録番号・騎手/調教師コードが全件一致）。

- CSVは CP932・ヘッダー1行。**同じ列名が何度も出る**ので、列名ではなく列番号で読む。
- 冪等: 主キーで UPSERT。中身（sha256）が前回と同じファイルは読み直さない（`force` で読み直す）。
- 読めない行は飛ばし、ファイル名・行番号・理由をログと `parse_warnings` に残す。
- 元のCSVは読むだけで、変更・移動しない。
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from keiba_data import config, db
from keiba_data.race_id import decode_race_id

logger = logging.getLogger(__name__)

ENCODING = "cp932"
RACE_DIR, HORSE_DIR = "race_data", "horse_data"
RACE_N_COLUMNS, HORSE_N_COLUMNS = 110, 104

# 欠損を表す書き方（Targetの書き出しでは空欄のほか、これらが入る）
MISSING = frozenset({"", "----", "不明", "%1d", "0000000000", "(0000000000)", "00000000"})


class RowError(ValueError):
    """1行を取り込めない理由。行は飛ばしてログに残す。"""


# --- 値の正規化（純関数） -------------------------------------------------------


def text(value: str | None) -> str | None:
    """全角英数・半角カナをそろえ（NFKC）、前後の空白を落とす。欠損はNone。"""
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", value).strip()
    return None if value in MISSING else value


def to_int(value: str | None) -> int | None:
    value = text(value)
    if value is None:
        return None
    try:
        return int(value.replace("+", ""))
    except ValueError as exc:
        raise RowError(f"整数ではありません: {value!r}") from exc


def to_float(value: str | None) -> float | None:
    value = text(value)
    if value is None:
        return None
    try:
        return float(value.replace("+", ""))
    except ValueError as exc:
        raise RowError(f"数値ではありません: {value!r}") from exc


def to_flag(value: str | None) -> int:
    """'*' や 'B' のような印を 1/0 にする。"""
    return 0 if text(value) is None else 1


def dotted_date(value: str | None) -> str | None:
    """'2024. 1. 6' → '2024-01-06'。"""
    value = text(value)
    if value is None:
        return None
    m = re.fullmatch(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", value)
    if not m:
        raise RowError(f"日付の形式が違います: {value!r}")
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"


def birth_date(year: str | None, month_day: str | None) -> str | None:
    """生年 '2022' ＋ 誕生日 '5月3日' → '2022-05-03'。"""
    year, month_day = text(year), text(month_day)
    if year is None or month_day is None:
        return None
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", month_day)
    if not m or not year.isdigit():
        raise RowError(f"生年月日の形式が違います: {year!r} {month_day!r}")
    return f"{int(year):04d}-{int(m[1]):02d}-{int(m[2]):02d}"


def race_time(value: str | None) -> float | None:
    """走破タイム '1343'（1分34秒3）→ 94.3秒。'----' はNone。"""
    value = text(value)
    if value is None:
        return None
    if not value.isdigit():
        raise RowError(f"走破タイムの形式が違います: {value!r}")
    tenths = int(value) // 1000 * 600 + int(value) % 1000
    return tenths / 10


def time_raw(seconds: float | None) -> str | None:
    """94.3 → '1:34.3'（netkeiba由来の results.time_raw と同じ書き方）。"""
    if seconds is None:
        return None
    tenths = round(seconds * 10)
    return f"{tenths // 600}:{tenths % 600 // 10:02d}.{tenths % 10}"


def kinryo(value: str | None) -> tuple[float | None, str | None]:
    """'57☆' → (57.0, '☆')。減量記号は別に持つ。"""
    value = text(value)
    if value is None:
        return None, None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(\D*)", value)
    if not m:
        raise RowError(f"斤量の形式が違います: {value!r}")
    return float(m[1]), (m[2] or None)


GOING = {"良": "良", "稍": "稍重", "重": "重", "不": "不良"}
STABLE = {"栗": "栗東", "美": "美浦", "地": "地方", "外": "海外"}


def stable(value: str | None) -> str | None:
    """'(栗)' / '[外]' → '栗東' / '海外'（trainers.stable と同じ値域）。"""
    value = text(value)
    if value is None:
        return None
    return STABLE.get(value.strip("()[]"), value)


def split_stable(value: str | None) -> tuple[str | None, str | None]:
    """厩舎 '(栗)氏名' → ('栗東', '氏名')。"""
    value = text(value)
    if value is None:
        return None, None
    m = re.fullmatch(r"([(\[][^)\]]*[)\]])(.*)", value)
    if not m:
        return None, value
    return stable(m[1]), (m[2].strip() or None)


def price(value: str | None) -> tuple[float | None, str | None]:
    """取引価格 '990万円(他)' / '226.8万円' → (990.0, '(他)') / (226.8, None)。"""
    value = text(value)
    if value is None:
        return None, None
    m = re.fullmatch(r"([\d,]+(?:\.\d+)?)万円(.*)", value)
    if not m:
        raise RowError(f"取引価格の形式が違います: {value!r}")
    return float(m[1].replace(",", "")), (m[2].strip() or None)


def record(value: str | None) -> tuple[int | None, ...]:
    """着度数 ' 2- 1- 1- 7' → (2, 1, 1, 7)。"""
    value = text(value)
    if value is None:
        return (None, None, None, None)
    parts = [p.strip() for p in value.split("-")]
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        raise RowError(f"着度数の形式が違います: {value!r}")
    return tuple(int(p) for p in parts)


def amount_with_count(value: str | None) -> tuple[float | None, int | None]:
    """兄弟の本賞金 '2089万(2)' / '0.0  (1)' → (2089.0, 2)。"""
    value = text(value)
    if value is None:
        return None, None
    m = re.fullmatch(r"([\d.]+)\s*万?\s*\((\d+)\)", value)
    if not m:
        raise RowError(f"兄弟の賞金の形式が違います: {value!r}")
    return float(m[1]), int(m[2])


def surface_of(track_code_jv: str | None) -> str | None:
    """JVのトラックコード → turf/dirt/jump（10〜22=芝、23〜29=ダート、51〜59=障害）。"""
    if track_code_jv is None or not track_code_jv.isdigit():
        return None
    code = int(track_code_jv)
    if 10 <= code <= 22:
        return "turf"
    if 23 <= code <= 29:
        return "dirt"
    if 51 <= code <= 59:
        return "jump"
    return None


# --- 本体（races/results）へ写すときの語彙の変換 ----------------------------------
# どれも 2023-01〜2026-09 の重複期間で、既存の値（netkeiba由来）と突き合わせて作った。
# 重複期間に出てこないコードは推測で埋めずに None にする。

GRADE = {"G1": "GI", "G2": "GII", "G3": "GIII", "JG1": "JGI", "JG2": "JGII", "JG3": "JGIII", "OP(L)": "L"}
CLASS_CONDITION = {
    "新馬": "新馬", "未勝利": "未勝利", "未出走": "未出走",
    "1勝": "1勝クラス", "2勝": "2勝クラス", "3勝": "3勝クラス",
    # 2019年夏より前のクラス名（既存DBには無い時代なので、netkeibaの書き方「◯◯万下」に合わせる）
    "400万": "400万下", "500万": "500万下", "900万": "900万下", "1000万": "1000万下",
    "1500万": "1500万下", "1600万": "1600万下",
    "オープン": "オープン", "OP(L)": "オープン", "重賞": "オープン",
    "G1": "オープン", "G2": "オープン", "G3": "オープン",
    "JG1": "オープン", "JG2": "オープン", "JG3": "オープン",
}
# クラス名の後ろに付く条件（netkeibaの「未勝利 牝」の「牝」）。競走記号コードで決まる（重複期間で全件一致）
CLASS_SUFFIX = {"020": "牝", "023": "牝", "024": "牝", "A23": "牝", "A24": "牝", "N21": "牝", "N24": "牝",
                "N41": "牡・牝", "002": "見習騎手", "M01": "九州産馬", "M03": "九州産馬"}
AGE_CONDITION = {"11": "2歳", "12": "3歳", "13": "3歳以上", "14": "4歳以上", "18": "3歳以上", "19": "4歳以上"}
WEIGHT_TYPE = {"1": "ハンデ", "2": "別定", "3": "馬齢", "4": "定量"}
RACE_SYMBOL = {
    "000": "", "002": "", "020": "", "003": "指", "023": "指", "004": "特指", "024": "特指",
    "A00": "混", "A03": "混,指", "A23": "混,指", "A04": "混,特指", "A24": "混,特指",
    "M01": "指", "M03": "指", "N00": "国際", "N01": "国際,指", "N03": "国際,指", "N21": "国際,指",
    "N41": "国際,指", "N04": "国際,特指", "N24": "国際,特指",
}
DIRECTION = {"10": "straight", "11": "left", "12": "left", "17": "right", "18": "right",
             "21": "right", "23": "left", "24": "right"}
COURSE_DETAIL = {"12": "外", "18": "外", "21": "内2周", "52": "芝 ダート", "54": "芝",
                 "55": "芝 外", "56": "芝 外-内", "57": "芝 内-外"}
FINISH_STATUS = {1: "取消", 3: "除外", 4: "中止", 7: "降着"}
MARGIN_WORDS = {"頭": "アタマ", "大差": "大"}
MARGIN_DROP = frozenset({"取消", "除外", "中止", "降着", "失格"})


def race_conditions(symbol_code: str | None, weight_type_code: str | None) -> str | None:
    """競走記号＋重量種別 → '混,指,馬齢'（races.race_conditions と同じ書き方）。"""
    prefix, weight = RACE_SYMBOL.get(symbol_code or ""), WEIGHT_TYPE.get(weight_type_code or "")
    if prefix is None or weight is None:
        return None
    return f"{prefix},{weight}" if prefix else weight


def class_condition(class_name: str | None, symbol_code: str | None) -> str | None:
    """クラス名＋競走記号 → '未勝利 牝'（races.class_condition と同じ書き方）。"""
    base = CLASS_CONDITION.get(class_name or "")
    if base is None:
        return None
    suffix = CLASS_SUFFIX.get(symbol_code or "")
    return f"{base} {suffix}" if suffix else base


def netkeiba_margin(value: str | None) -> str | None:
    """Targetの着差 '1 1/4' / '頭' / '大差' → '1.1/4' / 'アタマ' / '大'。"""
    if value is None or value in MARGIN_DROP:
        return None
    value = MARGIN_WORDS.get(value, value)
    return re.sub(r"^(\d+) (\d/\d)$", r"\1.\2", value)


# --- CSVの列番号 -----------------------------------------------------------------
# race_data（110列）
R_DATE, R_KAISAI, R_RACE_NO, R_RACE_NAME, R_HORSE_NAME, R_SEX, R_AGE, R_JOCKEY = 2, 3, 4, 5, 6, 8, 9, 10
R_KINRYO, R_N_REGISTERED, R_UMABAN, R_POPULARITY, R_FINISH, R_GOING, R_MULTI, R_STABLE = 11, 12, 13, 14, 15, 17, 18, 19
R_TRAINER, R_TIME, R_MARGIN_SEC, R_LAST_3F, R_WEIGHT, R_WEIGHT_DIFF, R_BLINKER, R_WAKU = 20, 21, 22, 26, 27, 28, 29, 30
R_DIFF_AT_3F, R_CORNERS = 32, (36, 37, 38, 39)
R_FINISHING_MOVE, R_RUNNING_STYLE, R_AVE_3F, R_LAST_3F_RANK, R_PCI, R_GOOD_RUN, R_PCI3, R_RPCI = 40, 41, 42, 44, 45, 46, 47, 48
R_AVG_1F, R_AVG_SPEED, R_SPEED_EX3F, R_SPEED_3F = 49, 50, 51, 52
R_HORSE_ID, R_JOCKEY_ID, R_TRAINER_ID, R_FINISH_POS, R_ARRIVAL, R_ABNORMAL = 53, 54, 55, 56, 57, 58
R_TRACK, R_TRACK_JV, R_CLASS_CODE, R_SYMBOL, R_RACE_TYPE, R_WEIGHT_TYPE, R_MARK_CODE = 59, 60, 61, 62, 63, 64, 65
R_MARK, R_KEY18, R_POST_TIME, R_CLASS_NAME, R_MARGIN, R_WIN_ODDS = 68, 76, 79, 80, 81, 87
R_AGE_DAYS, R_PRIZE, R_ADDED_PRIZE, R_WEATHER, R_INOUT, R_SURFACE, R_DISTANCE, R_COURSE_SETTING = 99, 103, 104, 105, 106, 107, 108, 109

# horse_data（104列）
(H_NAME, H_NAME_EN, H_HORSE_ID, H_SEX, H_AGE, H_STATUS, H_MARK, H_STABLE, H_SIRE, H_SIRE_BMS, H_DAM, H_BMS,
 H_DAM_DAM, H_DAM_DAM_SIRE, H_DAM_DAM_DAM) = 0, 5, 6, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21
H_LINES = (22, 23, 24, 25)
H_OWNER, H_SILKS, H_OWNER_CODE, H_BREEDER, H_BIRTHPLACE, H_COAT = 26, 28, 30, 31, 33, 34
H_EARNED, H_JUMP_EARNED, H_MAIN_PRIZE, H_ADDED_PRIZE, H_RECORD = 35, 36, 37, 38, 39
H_N_TOTAL, H_N_ACTUAL, H_N_JRA, H_FIRST_VENUE, H_LATEST_VENUE = 40, 41, 42, 46, 47
H_BIRTH_YEAR, H_BIRTHDAY, H_FIRST_RACE, H_LATEST_RACE = 51, 52, 54, 56
H_CREATED, H_REGISTERED, H_RETIRED, H_PRICE, H_SALE, H_NAME_ORIGIN = 57, 58, 59, 62, 63, 64
H_SIBLINGS = ((72, 73), (74, 75), (76, 77), (78, 79), (80, 81))
H_SIBLING_SUM, H_SIBLING_AVG = 82, 83
(H_SIRE_AGE, H_SIRE_COAT, H_DAM_AGE, H_DAM_COAT, H_DAM_DAM_AGE, H_DAM_DAM_COAT) = 84, 85, 86, 87, 88, 89
H_BREED_NOS = (90, 91, 92, 93, 94, 95, 96)

RACE_COLUMNS = (
    "race_id", "race_date", "venue_code", "kaiji", "nichime", "race_no", "kaisai_label", "race_name_short",
    "class_name", "class_code", "race_symbol_code", "race_type_code", "weight_type_code", "track_code",
    "track_code_jv", "surface", "distance_m", "turf_inout", "course_setting", "going", "weather", "post_time",
    "n_registered", "n_runners", "pci3", "rpci", "source_file", "imported_at",
)
RUN_COLUMNS = (
    "race_id", "umaban", "waku", "horse_id", "horse_name", "sex", "age", "jockey_id", "jockey_name",
    "trainer_id", "trainer_name", "stable", "kinryo", "kinryo_mark", "blinker", "horse_mark", "horse_mark_code",
    "multi_entry", "abnormal_code", "finish_raw", "finish_position", "arrival_order", "time_sec", "margin_sec",
    "margin", "corner1", "corner2", "corner3", "corner4", "last_3f", "last_3f_rank", "diff_at_3f", "ave_3f",
    "pci", "good_run", "avg_1f_sec", "avg_speed", "speed_ex_last3f", "speed_last3f", "finishing_move",
    "running_style", "win_odds", "popularity", "horse_weight", "weight_diff", "prize_man_yen",
    "added_prize_man_yen", "age_days", "source_file", "imported_at",
)
HORSE_COLUMNS = (
    "horse_id", "horse_name", "name_en", "sex", "age_at_export", "status", "horse_mark", "stable",
    "trainer_name", "birth_date", "coat_color", "birthplace", "sire_name", "sire_bms_name", "dam_name",
    "bms_name", "dam_dam_name", "dam_dam_sire_name", "dam_dam_dam_name", "sire_breed_no", "sire_bms_breed_no",
    "dam_breed_no", "bms_breed_no", "dam_dam_breed_no", "dam_dam_sire_breed_no", "dam_dam_dam_breed_no",
    "sire_line", "sire_bms_line", "bms_line", "dam_dam_sire_line", "sire_age", "sire_coat", "dam_age",
    "dam_coat", "dam_dam_age", "dam_dam_coat", "owner_code", "owner_name", "silks", "breeder_name",
    "earned_prize_man_yen", "jump_earned_prize_man_yen", "main_prize_man_yen", "added_prize_man_yen",
    "n_1st", "n_2nd", "n_3rd", "n_other", "n_races_total", "n_races_actual", "n_races_jra", "first_venue",
    "latest_venue", "first_race_key18", "latest_race_key18", "registered_date", "retired_date",
    "data_created_date", "sale_price_man_yen", "sale_price_note", "sale_name", "name_origin", "sibling_n",
    "sibling_prize_sum_man_yen", "sibling_prize_avg_man_yen", "source_file", "imported_at",
)
# レースの行に毎回同じ値で出てくる列（1レースの中で食い違えば警告する）
_RACE_LEVEL = ("race_date", "kaisai_label", "race_name_short", "class_name", "class_code", "race_symbol_code",
               "race_type_code", "weight_type_code", "track_code", "track_code_jv", "surface", "distance_m",
               "turf_inout", "course_setting", "going", "weather", "post_time", "n_registered", "pci3", "rpci")


# --- 1行の読み取り -----------------------------------------------------------------


def parse_race_row(row: list[str]) -> tuple[dict, dict]:
    """race_data の1行 → (target_races の値, target_runs の値)。"""
    if len(row) != RACE_N_COLUMNS:
        raise RowError(f"列数が{len(row)}です（{RACE_N_COLUMNS}列のはず）")
    key18 = row[R_KEY18].strip()
    if not re.fullmatch(r"\d{18}", key18):
        raise RowError(f"レースID(新)が18桁ではありません: {key18!r}")
    race_id, umaban = key18[0:4] + key18[8:16], int(key18[16:18])
    try:
        info = decode_race_id(race_id)
    except ValueError as exc:
        raise RowError(str(exc)) from exc
    race_date = dotted_date(row[R_DATE])
    if race_date is None or race_date.replace("-", "") != key18[0:8]:
        raise RowError(f"日付({race_date})とレースIDの日付({key18[0:8]})が一致しません")
    if to_int(row[R_UMABAN]) != umaban:
        raise RowError(f"馬番({row[R_UMABAN]!r})とレースIDの馬番({umaban})が一致しません")
    horse_id = text(row[R_HORSE_ID])
    if horse_id is None or not re.fullmatch(r"\d{10}", horse_id):
        raise RowError(f"血統登録番号が10桁ではありません: {row[R_HORSE_ID]!r}")
    track_code_jv = text(row[R_TRACK_JV])
    race = {
        "race_id": race_id, "race_date": race_date, "venue_code": info.venue_code, "kaiji": info.kaiji,
        "nichime": info.nichime, "race_no": info.race_no, "kaisai_label": text(row[R_KAISAI]),
        "race_name_short": text(row[R_RACE_NAME]), "class_name": text(row[R_CLASS_NAME]),
        "class_code": text(row[R_CLASS_CODE]), "race_symbol_code": text(row[R_SYMBOL]),
        "race_type_code": text(row[R_RACE_TYPE]), "weight_type_code": text(row[R_WEIGHT_TYPE]),
        "track_code": text(row[R_TRACK]), "track_code_jv": track_code_jv, "surface": surface_of(track_code_jv),
        "distance_m": to_int(row[R_DISTANCE]), "turf_inout": text(row[R_INOUT]),
        "course_setting": text(row[R_COURSE_SETTING]), "going": GOING.get(text(row[R_GOING]) or ""),
        "weather": text(row[R_WEATHER]), "post_time": text(row[R_POST_TIME]),
        "n_registered": to_int(row[R_N_REGISTERED]), "pci3": to_float(row[R_PCI3]), "rpci": to_float(row[R_RPCI]),
    }
    weight, mark = kinryo(row[R_KINRYO])
    finish_position = to_int(row[R_FINISH_POS])
    run = {
        "race_id": race_id, "umaban": umaban, "waku": to_int(row[R_WAKU]), "horse_id": horse_id,
        "horse_name": text(row[R_HORSE_NAME]), "sex": text(row[R_SEX]), "age": to_int(row[R_AGE]),
        "jockey_id": text(row[R_JOCKEY_ID]), "jockey_name": text(row[R_JOCKEY]),
        "trainer_id": text(row[R_TRAINER_ID]), "trainer_name": text(row[R_TRAINER]),
        "stable": stable(row[R_STABLE]), "kinryo": weight, "kinryo_mark": mark,
        "blinker": to_flag(row[R_BLINKER]), "horse_mark": text(row[R_MARK]),
        "horse_mark_code": text(row[R_MARK_CODE]), "multi_entry": to_flag(row[R_MULTI]),
        "abnormal_code": to_int(row[R_ABNORMAL]),
        # 丸数字（降着・失格の入線順）はNFKCで消えてしまうので、着順だけは原文のまま残す
        "finish_raw": row[R_FINISH].strip() or None,
        "finish_position": finish_position or None, "arrival_order": to_int(row[R_ARRIVAL]) or None,
        "time_sec": race_time(row[R_TIME]), "margin_sec": to_float(row[R_MARGIN_SEC]),
        "margin": text(row[R_MARGIN]),
        **{f"corner{i + 1}": to_int(row[c]) for i, c in enumerate(R_CORNERS)},
        "last_3f": to_float(row[R_LAST_3F]), "last_3f_rank": to_int(row[R_LAST_3F_RANK]),
        "diff_at_3f": to_float(row[R_DIFF_AT_3F]), "ave_3f": to_float(row[R_AVE_3F]), "pci": to_float(row[R_PCI]),
        "good_run": to_flag(row[R_GOOD_RUN]), "avg_1f_sec": to_float(row[R_AVG_1F]),
        "avg_speed": to_float(row[R_AVG_SPEED]), "speed_ex_last3f": to_float(row[R_SPEED_EX3F]),
        "speed_last3f": to_float(row[R_SPEED_3F]), "finishing_move": text(row[R_FINISHING_MOVE]),
        "running_style": text(row[R_RUNNING_STYLE]), "win_odds": to_float(row[R_WIN_ODDS]),
        "popularity": to_int(row[R_POPULARITY]), "horse_weight": to_int(row[R_WEIGHT]),
        "weight_diff": to_int(row[R_WEIGHT_DIFF]), "prize_man_yen": to_float(row[R_PRIZE]),
        "added_prize_man_yen": to_float(row[R_ADDED_PRIZE]), "age_days": to_int(row[R_AGE_DAYS]),
    }
    return race, run


def parse_horse_row(row: list[str]) -> tuple[dict, list[tuple]]:
    """horse_data の1行 → (target_horses の値, 兄弟 [(順位, 馬名, 勝数)])。"""
    if len(row) != HORSE_N_COLUMNS:
        raise RowError(f"列数が{len(row)}です（{HORSE_N_COLUMNS}列のはず）")
    horse_id = text(row[H_HORSE_ID])
    if horse_id is None or not re.fullmatch(r"\d{10}", horse_id):
        raise RowError(f"血統登録番号が10桁ではありません: {row[H_HORSE_ID]!r}")
    stable_name, trainer_name = split_stable(row[H_STABLE])
    price_value, price_note = price(row[H_PRICE])
    n1, n2, n3, n_other = record(row[H_RECORD])
    sibling_sum, sibling_n = amount_with_count(row[H_SIBLING_SUM])
    sibling_avg, _ = amount_with_count(row[H_SIBLING_AVG])
    breed_nos = [text(row[i]) for i in H_BREED_NOS]
    horse = {
        "horse_id": horse_id, "horse_name": text(row[H_NAME]), "name_en": text(row[H_NAME_EN]),
        "sex": text(row[H_SEX]), "age_at_export": to_int(row[H_AGE]), "status": text(row[H_STATUS]),
        "horse_mark": text(row[H_MARK]), "stable": stable_name, "trainer_name": trainer_name,
        "birth_date": birth_date(row[H_BIRTH_YEAR], row[H_BIRTHDAY]), "coat_color": text(row[H_COAT]),
        "birthplace": text(row[H_BIRTHPLACE]), "sire_name": text(row[H_SIRE]),
        "sire_bms_name": text(row[H_SIRE_BMS]), "dam_name": text(row[H_DAM]), "bms_name": text(row[H_BMS]),
        "dam_dam_name": text(row[H_DAM_DAM]), "dam_dam_sire_name": text(row[H_DAM_DAM_SIRE]),
        "dam_dam_dam_name": text(row[H_DAM_DAM_DAM]),
        **dict(zip(("sire_breed_no", "sire_bms_breed_no", "dam_breed_no", "bms_breed_no", "dam_dam_breed_no",
                    "dam_dam_sire_breed_no", "dam_dam_dam_breed_no"), breed_nos)),
        **dict(zip(("sire_line", "sire_bms_line", "bms_line", "dam_dam_sire_line"),
                   (text(row[i]) for i in H_LINES))),
        "sire_age": to_int(row[H_SIRE_AGE]), "sire_coat": text(row[H_SIRE_COAT]),
        "dam_age": to_int(row[H_DAM_AGE]), "dam_coat": text(row[H_DAM_COAT]),
        "dam_dam_age": to_int(row[H_DAM_DAM_AGE]), "dam_dam_coat": text(row[H_DAM_DAM_COAT]),
        "owner_code": text(row[H_OWNER_CODE]), "owner_name": text(row[H_OWNER]), "silks": text(row[H_SILKS]),
        "breeder_name": text(row[H_BREEDER]), "earned_prize_man_yen": to_float(row[H_EARNED]),
        "jump_earned_prize_man_yen": to_float(row[H_JUMP_EARNED]),
        "main_prize_man_yen": to_float(row[H_MAIN_PRIZE]), "added_prize_man_yen": to_float(row[H_ADDED_PRIZE]),
        "n_1st": n1, "n_2nd": n2, "n_3rd": n3, "n_other": n_other,
        "n_races_total": to_int(row[H_N_TOTAL]), "n_races_actual": to_int(row[H_N_ACTUAL]),
        "n_races_jra": to_int(row[H_N_JRA]), "first_venue": text(row[H_FIRST_VENUE]),
        "latest_venue": text(row[H_LATEST_VENUE]), "first_race_key18": text(row[H_FIRST_RACE]),
        "latest_race_key18": text(row[H_LATEST_RACE]), "registered_date": dotted_date(row[H_REGISTERED]),
        "retired_date": dotted_date(row[H_RETIRED]), "data_created_date": dotted_date(row[H_CREATED]),
        "sale_price_man_yen": price_value, "sale_price_note": price_note, "sale_name": text(row[H_SALE]),
        "name_origin": text(row[H_NAME_ORIGIN]), "sibling_n": sibling_n,
        "sibling_prize_sum_man_yen": sibling_sum, "sibling_prize_avg_man_yen": sibling_avg,
    }
    siblings = []
    for rank, (name_col, wins_col) in enumerate(H_SIBLINGS, start=1):
        name = text(row[name_col])
        if name is not None:
            siblings.append((horse_id, rank, name, to_int(row[wins_col])))
    return horse, siblings


# --- ファイルの取り込み -----------------------------------------------------------


@dataclass
class FileResult:
    file_name: str
    kind: str
    n_rows: int = 0
    n_loaded: int = 0
    n_skipped: int = 0
    unchanged: bool = False                   # 前回と同じ中身なので読み飛ばした
    warnings: list[tuple[int, str]] = field(default_factory=list)   # (行番号, 理由)
    seconds: float = 0.0


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upsert(table: str, columns: tuple[str, ...], keys: tuple[str, ...], where: str = "") -> str:
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in keys)
    return (f"{db._insert_sql(table, columns)} ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}"
            + (f" WHERE {where}" if where else ""))


def _read_rows(path: Path, expected_columns: int):
    """(行番号, 行) を返す。1行目のヘッダーは列数だけ確かめて読み飛ばす。"""
    with path.open(encoding=ENCODING, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None or len(header) != expected_columns:
            raise RowError(f"ヘッダーの列数が{len(header or [])}です（{expected_columns}列のはず）")
        for row in reader:
            yield reader.line_num, row


def load_race_file(conn: sqlite3.Connection, path: Path, result: FileResult, ts: str, dry_run: bool) -> None:
    races: dict[str, dict] = {}
    runs: dict[tuple[str, int], dict] = {}
    for line_no, row in _read_rows(path, RACE_N_COLUMNS):
        result.n_rows += 1
        try:
            race, run = parse_race_row(row)
        except RowError as exc:
            result.warnings.append((line_no, str(exc)))
            continue
        known = races.get(race["race_id"])
        if known is None:
            races[race["race_id"]] = race
        else:
            diff = [c for c in _RACE_LEVEL if known[c] != race[c]]
            if diff:   # レース単位の値が行ごとに違う（実データでは0件）。最初の行を採り、記録だけ残す
                result.warnings.append((line_no, f"{race['race_id']}: レース単位の列が他の行と違います {diff}"))
        if (run["race_id"], run["umaban"]) in runs:
            result.warnings.append((line_no, f"{run['race_id']} {run['umaban']}番: 同じ馬番の行が2回あります"))
            continue
        runs[(run["race_id"], run["umaban"])] = run
    # 出走頭数は races.n_runners と同じく取消・除外（異常コード1〜3）を除いて数える
    n_runners = Counter(r["race_id"] for r in runs.values() if r["abnormal_code"] not in (1, 2, 3))
    for race_id, race in races.items():
        race["n_runners"] = n_runners.get(race_id, 0)
    result.n_loaded = len(runs)
    result.n_skipped = result.n_rows - result.n_loaded
    if dry_run:
        return
    common = {"source_file": result.file_name, "imported_at": ts}
    conn.executemany(_upsert("target_races", RACE_COLUMNS, ("race_id",)),
                     [[{**r, **common}[c] for c in RACE_COLUMNS] for r in races.values()])
    conn.executemany(_upsert("target_runs", RUN_COLUMNS, ("race_id", "umaban")),
                     [[{**r, **common}[c] for c in RUN_COLUMNS] for r in runs.values()])


def load_horse_file(conn: sqlite3.Connection, path: Path, result: FileResult, ts: str, dry_run: bool) -> None:
    horses: dict[str, tuple[dict, list[tuple], int]] = {}
    for line_no, row in _read_rows(path, HORSE_N_COLUMNS):
        result.n_rows += 1
        try:
            horse, siblings = parse_horse_row(row)
        except RowError as exc:
            result.warnings.append((line_no, str(exc)))
            continue
        known = horses.get(horse["horse_id"])
        if known is not None:
            # 同じ馬が2行ある（実データでは1件）。データ作成日が新しい方を採り、もう一方は記録して捨てる
            older, newer = sorted([known, (horse, siblings, line_no)], key=lambda h: h[0]["data_created_date"] or "")
            result.warnings.append((older[2], f"{horse['horse_id']}: 同じ馬が{newer[2]}行目にもあり、"
                                              f"データ作成日の新しい{newer[2]}行目を採用しました"))
            horses[horse["horse_id"]] = newer
            continue
        horses[horse["horse_id"]] = (horse, siblings, line_no)
    result.n_loaded = len(horses)
    result.n_skipped = result.n_rows - result.n_loaded
    if dry_run:
        return
    # 別のファイルに同じ馬が出てきても、データ作成日の新しい方が残るようにする
    sql = _upsert("target_horses", HORSE_COLUMNS, ("horse_id",),
                  where="excluded.data_created_date >= target_horses.data_created_date "
                        "OR target_horses.data_created_date IS NULL")
    common = {"source_file": result.file_name, "imported_at": ts}
    for horse, siblings, _ in horses.values():
        cur = conn.execute(sql, [{**horse, **common}[c] for c in HORSE_COLUMNS])
        if cur.rowcount:      # 馬の行を入れた（または新しい方で上書きした）ときだけ兄弟も入れ替える
            conn.execute("DELETE FROM target_horse_siblings WHERE horse_id = ?", (horse["horse_id"],))
            conn.executemany("INSERT INTO target_horse_siblings (horse_id, rank, sibling_name, n_wins) "
                             "VALUES (?, ?, ?, ?)", siblings)


def load_file(conn: sqlite3.Connection, path: Path, kind: str, *, run_id: int | None = None,
              force: bool = False, dry_run: bool = False) -> FileResult:
    """1ファイルを1トランザクションで取り込む。"""
    started = time.monotonic()
    file_name = f"{path.parent.name}/{path.name}"
    result = FileResult(file_name=file_name, kind=kind)
    digest = sha256_of(path)
    known = conn.execute("SELECT sha256 FROM target_import_files WHERE file_name = ?", (file_name,)).fetchone()
    if known is not None and known[0] == digest and not force:
        result.unchanged = True
        logger.info("%s: 前回と同じ中身なので飛ばします（読み直すときは --force）", file_name)
        return result
    ts = db.now_str()
    loader = load_race_file if kind == "race" else load_horse_file
    try:
        if dry_run:
            loader(conn, path, result, ts, dry_run=True)
        else:
            with conn:
                loader(conn, path, result, ts, dry_run=False)
                conn.execute(
                    _upsert("target_import_files", ("file_name", "kind", "sha256", "size_bytes", "n_rows",
                                                    "n_loaded", "n_skipped", "run_id", "imported_at"),
                            ("file_name",)),
                    (file_name, kind, digest, path.stat().st_size, result.n_rows, result.n_loaded,
                     result.n_skipped, run_id, ts),
                )
    except RowError as exc:            # ヘッダーから読めない＝ファイルごと飛ばす
        result.warnings.append((1, str(exc)))
        logger.error("%s: 取り込めません: %s", file_name, exc)
    result.seconds = time.monotonic() - started
    for line_no, message in result.warnings:
        logger.warning("%s:%d: %s", file_name, line_no, message)
        if not dry_run:
            db.log_warning(conn, run_id, f"{file_name}:{line_no}", None, f"[target-import] {message}")
    logger.info("%s: %s行 / 取込%s / スキップ%s（%.1f秒）%s", file_name, f"{result.n_rows:,}",
                f"{result.n_loaded:,}", f"{result.n_skipped:,}", result.seconds, "（dry-run）" if dry_run else "")
    return result


def target_files(directory: Path, kind: str, only: str | None = None) -> list[Path]:
    sub = RACE_DIR if kind == "race" else HORSE_DIR
    files = sorted((directory / sub).glob("*.csv"))
    if only:
        files = [p for p in files if p.name == only or str(p).endswith(only)]
    return files


def load_files(conn: sqlite3.Connection, directory: Path = config.TARGET_DATASETS_DIR, *,
               kinds: tuple[str, ...] = ("race", "horse"), only: str | None = None, run_id: int | None = None,
               force: bool = False, dry_run: bool = False) -> list[FileResult]:
    results = []
    for kind in kinds:
        files = target_files(directory, kind, only)
        logger.info("Target %s: %dファイル（%s）", kind, len(files), directory / (RACE_DIR if kind == "race" else HORSE_DIR))
        kind_results = [load_file(conn, p, kind, run_id=run_id, force=force, dry_run=dry_run) for p in files]
        logger.info("Target %s 合計: %s行 / 取込%s / スキップ%s / 前回と同じ%dファイル", kind,
                    f"{sum(r.n_rows for r in kind_results):,}", f"{sum(r.n_loaded for r in kind_results):,}",
                    f"{sum(r.n_skipped for r in kind_results):,}", sum(r.unchanged for r in kind_results))
        results += kind_results
    return results


# --- 本体へのマージ ----------------------------------------------------------------


@dataclass
class MergeStats:
    inserted: Counter = field(default_factory=Counter)        # テーブル → 足した行数
    filled: Counter = field(default_factory=Counter)          # (テーブル, 列) → 埋めた欄の数
    name_aliases: int = 0                                     # カナに直せた祖先名の数
    names_left_english: int = 0                               # 直せずTargetの表記のまま入れた祖先名の数


def _merge_rows(conn: sqlite3.Connection, table: str, keys: tuple[str, ...], columns: tuple[str, ...],
                rows: list[tuple], extra: dict, stats: MergeStats, ts: str) -> None:
    """rows を table に写す。既存の行は空欄（NULL/''）だけを埋め、無い行は足す。

    `extra` は新しく足す行にだけ入れる列（fetched_at・source など）。
    埋めた欄は target_fills に残す（既存の値は決して上書きしない）。
    """
    conn.execute("DROP TABLE IF EXISTS temp.merge_rows")
    conn.execute(f"CREATE TEMP TABLE merge_rows ({', '.join(columns)}, PRIMARY KEY ({', '.join(keys)}))")
    conn.executemany(f"INSERT OR REPLACE INTO temp.merge_rows VALUES ({', '.join('?' for _ in columns)})", rows)
    match = " AND ".join(f"m.{k} = {table}.{k}" for k in keys)
    row_key = " || '|' || ".join(f"{table}.{k}" for k in keys)
    key_tuple = f"({', '.join(keys)})"
    for col in columns:
        if col in keys:
            continue
        blank = f"({table}.{col} IS NULL OR {table}.{col} = '')"
        has = f"m.{col} IS NOT NULL AND m.{col} <> ''"
        conn.execute(
            f"INSERT OR IGNORE INTO target_fills (table_name, row_key, column_name, filled_at) "
            f"SELECT ?, {row_key}, ?, ? FROM temp.merge_rows m JOIN {table} ON {match} WHERE {blank} AND {has}",
            (table, col, ts),
        )
        # 対象の行は主キーで引く（本体を毎回全件なめないように、merge_rows の側から絞る）
        cur = conn.execute(
            f"UPDATE {table} SET {col} = (SELECT m.{col} FROM temp.merge_rows m WHERE {match}) "
            f"WHERE {key_tuple} IN (SELECT {', '.join(f'm.{k}' for k in keys)} FROM temp.merge_rows m "
            f"WHERE {has}) AND {blank}"
        )
        if cur.rowcount:
            stats.filled[(table, col)] += cur.rowcount
    extra_cols = tuple(extra)
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(columns + extra_cols)}) "
        f"SELECT {', '.join(columns)}{''.join(', ?' for _ in extra_cols)} FROM temp.merge_rows WHERE true "
        f"ON CONFLICT DO NOTHING",
        tuple(extra.values()),
    )
    stats.inserted[table] += cur.rowcount
    conn.execute("DROP TABLE temp.merge_rows")


def _name_aliases(conn: sqlite3.Connection) -> dict[str, str]:
    """繁殖登録番号 → 本体での馬名（カナ）。

    外国産の祖先は、本体（JRA公式・netkeiba由来）がカナ、Targetが英字で書く。
    重複期間に同じ馬の父・母・母の父として両方に出てきた番号だけ、本体の書き方にそろえる。
    1つの番号に複数の書き方が出たら、いちばん多いものを採る。
    """
    seen: dict[str, Counter] = defaultdict(Counter)
    sql = """
        SELECT th.{no} AS no, th.{tname} AS tname, h.{hname} AS hname
          FROM target_horses th JOIN horses h USING (horse_id)
         WHERE th.{no} IS NOT NULL AND h.{hname} IS NOT NULL AND h.{hname} <> '' AND h.source = 'scrape'
    """
    for no, tname, hname in (("sire_breed_no", "sire_name", "sire"), ("dam_breed_no", "dam_name", "dam"),
                             ("bms_breed_no", "bms_name", "broodmare_sire")):
        for r in conn.execute(sql.format(no=no, tname=tname, hname=hname)):
            seen[r["no"]][r["hname"]] += 1
    return {no: names.most_common(1)[0][0] for no, names in seen.items()}


def _merge_masters(conn: sqlite3.Connection, stats: MergeStats, ts: str) -> None:
    """騎手・調教師（race_data の最新の表記）と馬主（horse_data）を足す。"""
    latest = """
        SELECT {id} AS id, {name} AS name{extra} FROM (
            SELECT t.{id}, t.{name}{textra}, ROW_NUMBER() OVER (PARTITION BY t.{id} ORDER BY r.race_date DESC) AS n
              FROM target_runs t JOIN target_races r USING (race_id) WHERE t.{id} IS NOT NULL)
         WHERE n = 1
    """
    jockeys = [(r["id"], r["name"], ts) for r in conn.execute(
        latest.format(id="jockey_id", name="jockey_name", extra="", textra=""))]
    _merge_rows(conn, "jockeys", ("jockey_id",), ("jockey_id", "jockey_name", "updated_at"), jockeys, {}, stats, ts)
    trainers = [(r["id"], r["name"], r["stable"], ts) for r in conn.execute(
        latest.format(id="trainer_id", name="trainer_name", extra=", stable", textra=", t.stable"))]
    _merge_rows(conn, "trainers", ("trainer_id",), ("trainer_id", "trainer_name", "stable", "updated_at"),
                trainers, {}, stats, ts)
    # SQLite は MAX() と一緒に選んだ列を、その最大の行から取る（データ作成日の新しい表記を採る）
    owners = [(r[0], r[1], ts) for r in conn.execute(
        "SELECT owner_code, owner_name, MAX(data_created_date) FROM target_horses "
        "WHERE owner_code IS NOT NULL GROUP BY owner_code")]
    _merge_rows(conn, "owners", ("owner_id",), ("owner_id", "owner_name", "updated_at"), owners, {}, stats, ts)


HORSE_MERGE_COLUMNS = ("horse_id", "horse_name", "sex", "sire", "dam", "broodmare_sire", "owner_name", "breeder",
                       "birth_date", "trainer_name", "updated_at")


def _merge_horses(conn: sqlite3.Connection, stats: MergeStats, ts: str) -> None:
    aliases = _name_aliases(conn)
    rows = []

    def name(no: str | None, value: str | None) -> str | None:
        if no in aliases:
            if aliases[no] != value:
                stats.name_aliases += 1
            return aliases[no]
        if value and re.search(r"[A-Za-z]", value):
            stats.names_left_english += 1
        return value

    for h in conn.execute("SELECT * FROM target_horses"):
        rows.append((h["horse_id"], h["horse_name"], h["sex"], name(h["sire_breed_no"], h["sire_name"]),
                     name(h["dam_breed_no"], h["dam_name"]), name(h["bms_breed_no"], h["bms_name"]),
                     h["owner_name"], h["breeder_name"], h["birth_date"], h["trainer_name"], ts))
    # horse_data に無い馬（古い世代など）は、race_data の最後の出走から名前と性別だけ入れる
    for h in conn.execute("""
        SELECT horse_id, horse_name, sex, trainer_name FROM (
            SELECT t.horse_id, t.horse_name, t.sex, t.trainer_name,
                   ROW_NUMBER() OVER (PARTITION BY t.horse_id ORDER BY r.race_date DESC) AS n
              FROM target_runs t JOIN target_races r USING (race_id)
             WHERE t.horse_id NOT IN (SELECT horse_id FROM target_horses))
         WHERE n = 1"""):
        rows.append((h["horse_id"], h["horse_name"], h["sex"], None, None, None, None, None, None,
                     h["trainer_name"], ts))
    _merge_rows(conn, "horses", ("horse_id",), HORSE_MERGE_COLUMNS, rows, {"source": "target"}, stats, ts)


MERGE_RACE_COLUMNS = (
    "race_id", "race_date", "venue_code", "kaiji", "nichime", "race_no", "race_name", "grade", "surface",
    "direction", "distance_m", "course_detail", "weather", "going_turf", "going_dirt", "post_time",
    "age_condition", "class_condition", "race_conditions", "n_runners",
)
MERGE_ENTRY_COLUMNS = ("race_id", "umaban", "waku", "horse_id", "sex", "age", "kinryo", "jockey_id", "trainer_id",
                       "horse_weight", "weight_diff")
MERGE_RESULT_COLUMNS = ("race_id", "umaban", "horse_id", "finish_position", "finish_status", "time_sec", "time_raw",
                        "margin", "corner_passing", "last_3f", "win_odds", "popularity", "prize_man_yen")


def core_race(r: sqlite3.Row | dict) -> tuple:
    """target_races の1行 → races の値。"""
    jv = r["track_code_jv"] or ""
    surface = r["surface"]
    going = r["going"]
    return (
        r["race_id"], r["race_date"], r["venue_code"], r["kaiji"], r["nichime"], r["race_no"],
        r["race_name_short"], GRADE.get(r["class_name"] or ""), surface, DIRECTION.get(jv), r["distance_m"],
        COURSE_DETAIL.get(jv), r["weather"],
        going if surface in ("turf", "jump") else None, going if surface == "dirt" else None,
        r["post_time"], AGE_CONDITION.get(r["race_type_code"] or ""),
        class_condition(r["class_name"], r["race_symbol_code"]),
        race_conditions(r["race_symbol_code"], r["weight_type_code"]),
        r["n_runners"],
    )


def core_result(t: sqlite3.Row | dict) -> tuple:
    """target_runs の1行 → results の値（netkeiba由来の書き方にそろえる）。"""
    corners = "-".join(str(t[f"corner{i}"]) for i in range(1, 5) if t[f"corner{i}"] is not None) or None
    prize = (t["prize_man_yen"] or 0) + (t["added_prize_man_yen"] or 0)
    return (
        t["race_id"], t["umaban"], t["horse_id"], t["finish_position"], FINISH_STATUS.get(t["abnormal_code"]),
        t["time_sec"], time_raw(t["time_sec"]), netkeiba_margin(t["margin"]), corners, t["last_3f"],
        t["win_odds"], t["popularity"],
        # results.prize_man_yen は本賞金＋付加賞で、賞金の無い着外はNULL
        round(prize, 1) if prize else None,
    )


def _merge_year(conn: sqlite3.Connection, year: str, stats: MergeStats, ts: str) -> None:
    races = [core_race(r) for r in conn.execute(
        "SELECT * FROM target_races WHERE substr(race_date, 1, 4) = ?", (year,))]
    _merge_rows(conn, "races", ("race_id",), MERGE_RACE_COLUMNS, races,
                {"fetched_at": ts, "updated_at": ts, "source": "target"}, stats, ts)
    runs = conn.execute(
        "SELECT t.* FROM target_runs t JOIN target_races r USING (race_id) WHERE substr(r.race_date, 1, 4) = ?",
        (year,)).fetchall()
    _merge_rows(conn, "entries", ("race_id", "umaban"), MERGE_ENTRY_COLUMNS,
                [tuple(t[c] for c in MERGE_ENTRY_COLUMNS) for t in runs], {}, stats, ts)
    _merge_rows(conn, "results", ("race_id", "umaban"), MERGE_RESULT_COLUMNS,
                [core_result(t) for t in runs], {}, stats, ts)


def merge(conn: sqlite3.Connection, *, dry_run: bool = False) -> MergeStats:
    """target_* から本体へ写す。何度流しても結果は同じ（2回目は何も変わらない）。

    `dry_run` のときは最後にロールバックして、足す行数・埋める欄の数だけを返す。
    """
    stats = MergeStats()
    ts = db.now_str()
    years = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(race_date, 1, 4) FROM target_races ORDER BY 1")]
    # マスタ・馬で1トランザクション、そのあと1年ずつ1トランザクション（途中で止まっても続きから流せる）
    try:
        _merge_masters(conn, stats, ts)
        _merge_horses(conn, stats, ts)
        if not dry_run:
            conn.commit()
        logger.info("マスタ・馬: 騎手+%d 調教師+%d 馬主+%d 馬+%s（祖先名をカナに直した数 %s、英字のまま %s）",
                    stats.inserted["jockeys"], stats.inserted["trainers"], stats.inserted["owners"],
                    f"{stats.inserted['horses']:,}", f"{stats.name_aliases:,}", f"{stats.names_left_english:,}")
        for year in years:
            before = stats.inserted.copy()
            _merge_year(conn, year, stats, ts)
            if not dry_run:
                conn.commit()
            added = stats.inserted - before
            logger.info("%s年: レース+%s 出走+%s 結果+%s%s", year, f"{added['races']:,}",
                        f"{added['entries']:,}", f"{added['results']:,}", "（dry-run）" if dry_run else "")
    finally:
        if dry_run or conn.in_transaction:
            conn.rollback()
    for (table, col), n in sorted(stats.filled.items()):
        logger.info("空欄を埋めた: %s.%s %s件", table, col, f"{n:,}")
    return stats
