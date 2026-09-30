"""JRA公式サイトのレース結果ページから、**当日・前日のレース情報とラップ**を読む。

出典: https://www.jra.go.jp/ （robots.txtは全許可）

netkeibaのdbページ（`db.netkeiba.com/race/…`）は当日・前日の結果をまだ公開しないため、
その日の馬場差（dashboard/race_forecast.py）を測る材料が手に入らない。JRA公式は
レース後すぐ出るうえ、**1開催ぶんの全レースが1ページ**にまとまっているので数リクエストで済む。

## 辿り方（すべてPOST。`cname` の末尾2文字はチェックサムで組み立てられないので必ずリンクを辿る）
1. トップページ（GET）の `doAction('/JRADB/accessS.html','pw01sli00/AF')` → **開催選択**
2. 開催選択の `pw01srl…` → **レース選択**（開催ごと）
3. レース選択の `pw01ses…` → **レース結果一覧**（その開催の全レースが1ページ）

`cname` の末尾には `場(2) 年(4) 回(2) 日目(2) 日付(8)` が並ぶ（先頭側の桁は日によって揺れるので、
**右から**読む）。例: `pw01ses10062026040520260919/D3` → 中山(06)・2026年・4回・5日目・2026-09-19。

## レース結果一覧のHTML（実物で確認済み）
    <div class="race_result_unit" id="race_result_11R">
      <div class="cell date">2026年9月20日（日曜） 4回中山6日</div>
      <div class="cell time">発走時刻：<strong>15時45分</strong></div>
      <div class="cell baba"><li class="weather">…天候 雨</li><li class="turf">…芝 重</li></div>
      <span class="race_name">産経賞オールカマー<span class="grade_icon"><img alt="GⅡ"></span></span>
      <div class="cell category">3歳以上</div><div class="cell class">オープン</div>
      <div class="cell rule">（国際）（指定）</div><div class="cell weight">別定</div>
      <div class="cell course">コース：2,200メートル（芝・右 外）</div>
      …着順テーブル…
      <table class="basic narrow"><th>ハロンタイム</th><td>12.8 - 11.8 - …</td></table>

ページの文字コードは **cp932**（`shift_jis` ではデコードできない文字がある）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import Tag

from keiba_data import config, parsing
from keiba_data.race_id import compose_race_id
from keiba_data.scrapers import LayoutError

# doAction('/JRADB/accessS.html', 'pw01srl1006202604052026091 9/CA') の第2引数。
# 出馬表は accessD.html なので、ページの種類（S/D/O）は問わない。
# オッズ（jra_odds.py）も同じ書き方なので、ここから使う。
ACTION_RE = re.compile(r"doAction\(\s*'/JRADB/access[A-Z]+\.html[^']*'\s*,\s*'([^']+)'\s*\)")
# cnameの末尾: 場(2) 年(4) 回(2) 日目(2) 日付(8) / チェックサム(2)
_CNAME_TAIL_RE = re.compile(r"(\d{2})(\d{4})(\d{2})(\d{2})(\d{8})/[0-9A-Za-z]{2}$")
# レース1件のcname。末尾は 場(2) 年(4) 回(2) 日目(2) R(2) 日付(8) / チェックサム(2)
# 種類は結果が `sde`、出馬表が `dde`。
_RACE_CNAME_RE = r"pw01{kind}\d*?(\d{{2}})(\d{{4}})(\d{{2}})(\d{{2}})(\d{{2}})(\d{{8}})/[0-9A-Za-z]{{2}}$"
# 「4回中山6日」
_MEETING_LABEL_RE = re.compile(r"(\d+)回(\D+?)(\d+)日")
# 「2026年9月20日（日曜） 4回中山6日」
_DATE_LINE_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
_POST_TIME_RE = re.compile(r"(\d{1,2})時\s*(\d{1,2})分")
# 「1,800メートル（ダート・右）」「3,140メートル（芝）」
_COURSE_RE = re.compile(r"([\d,]+)\s*メートル\s*[（(]([^）)]*)[）)]")
_RACE_NO_RE = re.compile(r"race_result_(\d+)R")
_AGE_RE = re.compile(r"(\d+歳(?:以上)?)")
# 過去レース結果検索ページが持っている月ごとのトークン表: objParam["2609"]="54";
_MONTH_PARAM_RE = re.compile(r'objParam\["(\d{4})"\]\s*=\s*"([0-9A-Za-z]{2})"')
_CURRENT_MONTH_RE = re.compile(r'var\s+yearMonth\s*=\s*"(\d{6})"')

_SURFACE_MAP = {"芝": "turf", "ダート": "dirt"}
_DIRECTION_MAP = {"右": "right", "左": "left", "直線": "straight"}
# 着順テーブルに出る「出走しなかった馬」。netkeibaのn_runnersと同じ数え方にするため除く。
_NON_STARTERS = ("取消", "除外")
# JRAは全角ローマ数字＋中黒で「J･GⅢ」のように書く。netkeiba側の表記（JGIII）に合わせる。
_GRADE_TRANS = str.maketrans({"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "･": "", "・": "", " ": "", "　": ""})
# 条件の表記をnetkeiba側（「混,指,馬齢」）に寄せる
_CONDITION_MAP = {"混合": "混", "指定": "指", "特別指定": "特指", "見習騎手": "見習騎手"}


@dataclass(frozen=True)
class Meeting:
    """開催選択ページに並ぶ1開催（例: 2026-09-19の4回中山5日）。"""

    cname: str  # レース選択ページを開くためのトークン
    date: str  # "YYYY-MM-DD"
    venue_code: str
    kaiji: int
    nichime: int
    venue_name: str | None = None  # ラベル「4回中山5日」から。突き合わせ用

    @property
    def key(self) -> str:
        """HTMLの保存キーやログに使う短い名前。"""
        return f"{self.date}_{self.venue_code}"


@dataclass
class JraRace:
    """レース結果一覧から読んだ1レース。"""

    race: dict  # キーは db.RACE_COLUMNS と同じ
    laps: list[dict]  # race_id, seq, distance_m（スタートからの累計）, lap_sec
    warnings: list[str] = field(default_factory=list)


def _text(tag: Tag | None) -> str:
    return parsing.normalize_space(tag.get_text(" ", strip=True)) if tag else ""


def cnames(html: str, marker: str) -> list[tuple[str, str]]:
    """`marker`（srl/ses/orl など）を含むcnameと、そのリンクの表示文字列を返す。"""
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    for a in soup.find_all("a", onclick=True):
        matched = ACTION_RE.search(a["onclick"])
        if matched and marker in matched.group(1):
            found.append((matched.group(1), _text(a)))
    return found


def _split_cname(cname: str) -> tuple[str, int, int, str] | None:
    """cnameの末尾から (場コード, 開催回, 日目, 日付) を取り出す。"""
    matched = _CNAME_TAIL_RE.search(cname)
    if matched is None:
        return None
    venue_code, _year, kaiji, nichime, yyyymmdd = matched.groups()
    if venue_code not in config.VENUE_CODES:
        return None
    date = f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    return venue_code, int(kaiji), int(nichime), date


def find_index_cname(html: str, marker: str = "sli") -> str | None:
    """トップページから開催選択ページを開くトークンを拾う。

    トップの `doAction('/JRADB/accessS.html','pw01sli00/AF')` がそれ（レース結果）。
    出馬表は `pw01dli00/F3` なので `marker="dli"` で拾う。ここだけは日付が埋まっていない
    固定のトークンなので、リンクの有無だけを見る。
    """
    for cname, _label in cnames(html, marker):
        return cname
    return None


def find_past_search_cname(html: str) -> str | None:
    """トップページから「過去のレース結果」検索ページへのトークンを拾う。"""
    for cname, _label in cnames(html, "skl"):
        return cname
    return None


def parse_month_links(html: str) -> dict[str, str]:
    """過去レース結果検索ページから、**月ごとの**開催一覧へのトークンを読む。

    このページは年月の選択に使う `objParam["YYMM"]="XX"` の表を持っていて、
    ページ内のJavaScriptと同じ組み立て方（当月以降は `pw01skl00`、それより前は `pw01skl10`）で
    任意の月のトークンが作れる。**チェックサムはページが持っている値をそのまま使う**。

    戻り値は `{"YYYY-MM": cname}`。
    """
    current = _CURRENT_MONTH_RE.search(html)
    current_ym = int(current.group(1)) if current else 0
    links: dict[str, str] = {}
    for yymm, checksum in _MONTH_PARAM_RE.findall(html):
        year = 1900 + int(yymm[:2]) if int(yymm[:2]) >= 70 else 2000 + int(yymm[:2])
        month = int(yymm[2:])
        if not 1 <= month <= 12:
            continue
        ym = year * 100 + month
        prefix = "pw01skl00" if ym >= current_ym else "pw01skl10"
        links[f"{year:04d}-{month:02d}"] = f"{prefix}{ym}/{checksum}"
    return links


def parse_meeting_index(html: str, marker: str = "srl") -> list[Meeting]:
    """開催選択ページから開催の一覧を読む（結果は `srl`、出馬表は `drl`）。"""
    meetings: list[Meeting] = []
    seen: set[str] = set()
    for cname, label in cnames(html, marker):
        parts = _split_cname(cname)
        if parts is None or cname in seen:
            continue
        seen.add(cname)
        venue_code, kaiji, nichime, date = parts
        venue_name = None
        if matched := _MEETING_LABEL_RE.search(label):
            venue_name = matched.group(2)
        meetings.append(Meeting(cname, date, venue_code, kaiji, nichime, venue_name))
    if not meetings:
        raise LayoutError("開催選択ページから開催のリンクが1件も読めませんでした")
    return meetings


def parse_result_index(html: str) -> list[Meeting]:
    """レース結果の開催選択ページ（今週の開催と、過去の開催が並ぶ）。"""
    return parse_meeting_index(html, "srl")


def parse_entry_index(html: str) -> list[Meeting]:
    """出馬表の開催選択ページ（今週これから行われる開催）。"""
    return parse_meeting_index(html, "drl")


def parse_race_menu(html: str) -> str | None:
    """レース選択ページから「レース結果一覧」（全レースが1ページ）のcnameを取り出す。"""
    for cname, _label in cnames(html, "ses"):
        if _split_cname(cname) is not None:
            return cname
    return None


@dataclass(frozen=True)
class HorseProfile:
    """JRAの出馬表から読んだ1頭ぶんの血統・馬主・生産牧場と、馬体重。"""

    umaban: int | None
    horse_name: str
    sire: str | None = None            # 父
    dam: str | None = None             # 母
    broodmare_sire: str | None = None  # 母の父
    owner: str | None = None           # 馬主
    breeder: str | None = None         # 生産牧場
    horse_weight: int | None = None    # 馬体重（当日発表前はNone）
    weight_diff: int | None = None     # 前走からの増減（初出走・発表前はNone）


def _family(cell: Tag, selector: str) -> str | None:
    """`<li class="sire"><span>父：</span>フィエールマン</li>` から馬名だけを取る。"""
    node = cell.select_one(selector)
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    text = re.sub(r"[（(]母の父[：:][^）)]*[）)]", "", text)   # 母の行から母父の括弧を外す
    text = re.sub(r"^(父|母)\s*[：:]\s*", "", parsing.normalize_space(text))
    return text or None


def _weight(cell: Tag) -> tuple[int | None, int | None]:
    """`<div class="cell weight">504kg<span class="transition">(+6)</span></div>` を読む。

    馬体重は当日発表なので、発表前はこの要素そのものが無い（代わりに戦績と総賞金が出る）。
    そのときは `(None, None)` を返す＝取れないのが正常。

    セレクタは**馬の欄（`td.horse`）の中だけ**を見る。レース表題の「馬齢」も
    `div.cell.weight`、斤量の列も `td.weight` なので、緩めると別の値を拾ってしまう。
    """
    node = cell.select_one("div.result_line div.cell.weight")
    if node is None:
        return None, None
    return parsing.parse_weight(node.get_text(" ", strip=True))


def parse_shutuba_profiles(html: str) -> list[HorseProfile]:
    """JRA公式の出馬表（1レース1ページ）から、馬ごとの血統・馬主・生産牧場と馬体重を読む。

    netkeibaの出馬表にはこれらが無いので、こちらから補う。JRAは馬のnetkeiba IDを
    持たないため、**馬番**（枠順確定前は馬名）で突き合わせる前提で返す。
    """
    soup = BeautifulSoup(html, "lxml")
    profiles: list[HorseProfile] = []
    for row in soup.select("table.basic tbody tr"):
        cell = row.select_one("td.horse")
        name = _text(cell.select_one("div.name")) if cell else ""
        if not cell or not name:
            continue
        bloodmare = _text(cell.select_one("span.bloodmare"))
        matched = re.search(r"母の父[：:]\s*([^）)]+)", bloodmare) if bloodmare else None
        horse_weight, weight_diff = _weight(cell)
        profiles.append(HorseProfile(
            umaban=parsing.to_int(_text(row.select_one("td.num"))),
            horse_name=name,
            sire=_family(cell, "li.sire"),
            dam=_family(cell, "li.mare"),
            broodmare_sire=parsing.normalize_space(matched.group(1)) if matched else None,
            owner=_text(cell.select_one("p.owner")) or None,
            breeder=_text(cell.select_one("p.breeder")) or None,
            horse_weight=horse_weight,
            weight_diff=weight_diff,
        ))
    if not profiles:
        raise LayoutError("出馬表から馬が1頭も読めませんでした")
    return profiles


def parse_race_links(html: str, meeting: Meeting, kind: str = "sde") -> dict[str, str]:
    """レース選択ページから、レースごとのページのトークンを集める（結果=sde / 出馬表=dde）。

    戻り値は `{race_id: cname}`。リンクは
    `https://www.jra.go.jp/JRADB/accessS.html?CNAME=<cname>` で開ける（GETで見られる）。
    トークンの末尾2文字はチェックサムなので**自力では組み立てず**、このページから拾う。

    ページによって書き方が2通りある（どちらも同じトークン）:
    - 今週の開催: `doAction('/JRADB/accessS.html', 'pw01sde01…/40')`
    - 過去の開催: `href="/JRADB/accessS.html?CNAME=pw01sde1004…/5E"`

    開催（場・回・日目・日付）が `meeting` と食い違うトークンは、取り違え防止のため捨てる。
    """
    found = set(ACTION_RE.findall(html)) | set(re.findall(r"CNAME=([^\"'&\s]+)", html))
    pattern = re.compile(_RACE_CNAME_RE.format(kind=kind))
    links: dict[str, str] = {}
    for cname in found:
        matched = pattern.search(cname)
        if matched is None:
            continue
        venue_code, year, kaiji, nichime, race_no, yyyymmdd = matched.groups()
        date = f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
        if (venue_code, int(kaiji), int(nichime), date) != (
            meeting.venue_code, meeting.kaiji, meeting.nichime, meeting.date
        ):
            continue
        links[compose_race_id(int(year), venue_code, int(kaiji), int(nichime), int(race_no))] = cname
    return links


def _parse_course(text: str, jump: bool, warnings: list[str]) -> dict:
    """「コース：2,200メートル（芝・右 外）」を分解する。"""
    out = {"surface": "jump" if jump else None, "direction": None,
           "distance_m": None, "course_detail": None}
    matched = _COURSE_RE.search(text)
    if matched is None:
        warnings.append(f"コース表記をパースできません: {text!r}")
        return out
    out["distance_m"] = int(matched.group(1).replace(",", ""))

    tokens = [t for t in re.split(r"[・\s]+", matched.group(2)) if t]
    rest = []
    for token in tokens:
        if token in _SURFACE_MAP and not jump:
            out["surface"] = _SURFACE_MAP[token]
        elif token in _DIRECTION_MAP:
            out["direction"] = _DIRECTION_MAP[token]
        elif token not in _SURFACE_MAP:
            rest.append(token)
    out["course_detail"] = " ".join(rest) or None
    if out["surface"] is None:
        warnings.append(f"馬場（芝/ダート）が読めません: {text!r}")
    return out


def _parse_grade(unit: Tag) -> str | None:
    """グレードのアイコン（alt="GⅡ" / "J･GⅢ"）をnetkeiba表記（GII / JGIII）にする。"""
    icon = unit.select_one("span.grade_icon img[alt]")
    if icon is None:
        return None
    grade = icon["alt"].translate(_GRADE_TRANS)
    return grade or None


def _parse_conditions(unit: Tag) -> tuple[str | None, str | None, str | None]:
    """条件の3つ組 (クラス, 条件, 生テキスト) を netkeiba と同じ形で作る。

    netkeibaは「2歳未勝利 牝 [指定] 馬齢」を class_condition=「未勝利 牝」、
    race_conditions=「指,馬齢」に分けている。JRAは同じ情報がセルに分かれているので、
    **括弧の中は条件へ、括弧の外に残った語（牝・見習騎手など）はクラスへ**まわす。
    """
    rule = _text(unit.select_one("div.cell.rule"))
    weight = _text(unit.select_one("div.cell.weight"))
    klass = _text(unit.select_one("div.cell.class"))

    tokens = [
        _CONDITION_MAP.get(raw, raw)
        for raw in re.findall(r"[（(\[［]([^）)\]］]+)[）)\]］]", rule)
    ]
    if weight:
        tokens.append(weight)  # 馬齢・定量・別定（netkeibaでは括弧の中に入っている）

    # 括弧を外した残り（「牝」「牡・牝」「見習騎手」など）はクラス表記の一部
    extra = parsing.normalize_space(re.sub(r"[（(\[［][^）)\]］]*[）)\]］]", "", rule))
    class_condition = parsing.normalize_space(f"{klass} {extra}") or None

    raw_text = " ".join(x for x in (
        _text(unit.select_one("div.cell.category")), klass, rule, weight
    ) if x)
    return class_condition, (",".join(tokens) or None), (raw_text or None)


def _parse_going(unit: Tag) -> dict:
    out = {"weather": None, "going_turf": None, "going_dirt": None}
    baba = unit.select_one("div.cell.baba")
    if baba is None:
        return out
    for li in baba.find_all("li"):
        value = _text(li.select_one("span.txt")) or None
        classes = li.get("class") or []
        if "weather" in classes:
            out["weather"] = value
        elif "turf" in classes:
            out["going_turf"] = value
        elif "durt" in classes or "dirt" in classes:  # JRAのクラス名は "durt"
            out["going_dirt"] = value
    return out


def _runner_count(unit: Tag) -> int | None:
    """出走頭数。着順テーブルの行数から、取消・除外を除いて数える。"""
    places = [_text(td) for td in unit.select("td.place")]
    if not places:
        return None
    return sum(1 for p in places if p not in _NON_STARTERS)


def _winner_corner(unit: Tag) -> str | None:
    """勝ち馬のコーナー通過順位（`1-1-1-1`）。読めなければ None。

    馬ごとの結果はDBに入れない（JRAは馬のnetkeiba IDを持たないため）が、
    **勝ち馬の脚質**は開催の傾向を読むのに要るので、これだけ取っておく。
    """
    for row in unit.select("tr"):
        place = row.select_one("td.place")
        if place is None or _text(place) != "1":
            continue
        corners = [_text(li) for li in row.select("td.corner li")]
        corners = [c for c in corners if c.isdigit()]
        return "-".join(corners) or None
    return None


def _parse_laps(unit: Tag, race_id: str, distance: int | None, warnings: list[str]) -> list[dict]:
    """「ハロンタイム」行を読む。`distance_m` はスタートからの累計にする。"""
    if distance is None:
        return []
    cell = None
    for th in unit.find_all("th"):
        if th.get_text(strip=True) == "ハロンタイム":
            cell = th.find_next_sibling("td")
            break
    if cell is None:
        return []
    values = [parsing.to_float(v) for v in cell.get_text(strip=True).split("-")]
    if not values or any(v is None for v in values):
        warnings.append(f"ハロンタイムをパースできません: {cell.get_text(strip=True)!r}")
        return []
    return parsing.lap_rows(race_id, distance, values, warnings)


def _parse_unit(unit: Tag, meeting: Meeting) -> JraRace | None:
    """`div.race_result_unit` 1つ分。まだ行われていないレースは None。"""
    matched = _RACE_NO_RE.search(unit.get("id") or "")
    if matched is None:
        return None
    race_no = int(matched.group(1))
    warnings: list[str] = []

    n_runners = _runner_count(unit)
    if not n_runners:
        return None  # 着順が無い＝まだ行われていない

    race_id = compose_race_id(
        int(meeting.date[:4]), meeting.venue_code, meeting.kaiji, meeting.nichime, race_no
    )
    # ページ側の日付・開催回とトークンが食い違えば、サイトの作りが変わった可能性がある
    date_line = _text(unit.select_one("div.cell.date"))
    if dm := _DATE_LINE_RE.search(date_line):
        page_date = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        if page_date != meeting.date:
            warnings.append(f"ページの日付({page_date})がリンクの日付({meeting.date})と一致しません")
    if lm := _MEETING_LABEL_RE.search(date_line):
        if (int(lm.group(1)), int(lm.group(3))) != (meeting.kaiji, meeting.nichime):
            warnings.append(
                f"ページの開催回/日目({lm.group(1)}回{lm.group(3)}日目)がリンクと一致しません"
            )

    category = _text(unit.select_one("div.cell.category"))
    jump = category.startswith("障害")
    age_match = _AGE_RE.search(category)
    class_condition, conditions, condition_raw = _parse_conditions(unit)

    post_time = None
    if tm := _POST_TIME_RE.search(_text(unit.select_one("div.cell.time"))):
        post_time = f"{int(tm.group(1)):02d}:{int(tm.group(2)):02d}"

    race = {
        "race_id": race_id,
        "race_date": meeting.date,
        "venue_code": meeting.venue_code,
        "kaiji": meeting.kaiji,
        "nichime": meeting.nichime,
        "race_no": race_no,
        "race_name": _text(unit.select_one("span.race_name")) or None,
        "grade": _parse_grade(unit),
        "post_time": post_time,
        "age_condition": age_match.group(1) if age_match else None,
        "class_condition": class_condition,
        "race_conditions": conditions,
        "condition_raw": condition_raw,
        "n_runners": n_runners,
        # 勝ち馬の脚質を出すためだけの値（馬ごとの結果は入れない）
        "winner_corner": _winner_corner(unit),
    }
    race.update(_parse_going(unit))
    race.update(_parse_course(_text(unit.select_one("div.cell.course")), jump, warnings))

    laps = _parse_laps(unit, race_id, race["distance_m"], warnings)
    if not laps and not jump:
        warnings.append(f"{race_no}R: ハロンタイムが取得できません")
    return JraRace(race=race, laps=laps, warnings=warnings)


def parse_meeting_results(html: str, meeting: Meeting) -> list[JraRace]:
    """レース結果一覧ページから、その開催のレースを読む（まだのレースは含まない）。

    日付・競馬場・開催回はリンクのトークン（`meeting`）を正とし、ページの表記と
    食い違うときは warning にする（race_idの組み立てを取り違えないため）。
    """
    soup = BeautifulSoup(html, "lxml")
    units = soup.select("div.race_result_unit")
    if not units:
        raise LayoutError("レース結果一覧にレースのブロック(div.race_result_unit)がありません")
    races = [race for unit in units if (race := _parse_unit(unit, meeting)) is not None]
    return races
