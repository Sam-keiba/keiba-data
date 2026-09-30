"""DB更新の流れ（update / backfill / reparse 共通）。

1. 開催カレンダー（月単位）から開催日を洗い出し、kaisai_days に登録する
   （終わった月のカレンダーは一度取得したら再取得しない）
2. 未完了の開催日ごとにレース一覧を取得してrace_idを得る
3. DBに未保存のrace_idだけ結果ページを取得し、パースして保存する
   （保存済みHTMLがあればネットにアクセスせずそれを使う）
4. その日の全レースを保存できたら kaisai_days.completed = 1 にする

1レースごとにコミットするので、中断（Ctrl+C・ブロック検知・リクエスト上限）しても
保存済みの分は残り、同じコマンドを再実行すると続きから再開する。
"""

from __future__ import annotations

import logging
import sqlite3
from calendar import monthrange
from datetime import date, timedelta
from typing import Protocol

import requests

from keiba_data import config, db
from keiba_data.cushion import TrackCondition
from keiba_data.html_store import HtmlStore
from keiba_data.race_id import decode_race_id
from keiba_data.scrapers import (
    LayoutError, baba, jra_horse, jra_odds, jra_result, jra_sire, netkeiba_ped,
)
from keiba_data.scrapers.calendar import parse_calendar
from keiba_data.scrapers.race_list import parse_race_list, parse_race_list_details
from keiba_data.scrapers.race_result import parse_race_result
from keiba_data.scrapers.shutuba import parse_shutuba

logger = logging.getLogger(__name__)

# 開催日からこの日数が過ぎても結果ページが無いレースは、中止等で結果が出ないものとみなす
RESULT_GRACE_DAYS = 3
# DBの最新開催日から何日さかのぼってupdateで再確認するか（途中で実行した日の取りこぼし対策）
UPDATE_OVERLAP_DAYS = 7
# updateがさかのぼる最大日数（これより古い分はbackfillの担当）
UPDATE_MAX_LOOKBACK_DAYS = 60

SAVED, UNAVAILABLE, ERROR = "saved", "unavailable", "error"


class HttpClient(Protocol):
    n_requests: int

    def get_text(self, url: str, *, encoding: str) -> str | None: ...

    def post_text(self, url: str, data: dict[str, str | bytes], *, encoding: str) -> str | None: ...


def _months(start: date, end: date) -> list[tuple[int, int]]:
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def _merge_measurements(a: "baba.BabaMeasurement", b: "baba.BabaMeasurement") -> "baba.BabaMeasurement":
    """クッション値のページと含水率のページの値を、同じ（競馬場・日）でひとつにまとめる。"""
    from dataclasses import replace

    merged = replace(
        a,
        cushion_value=a.cushion_value if a.cushion_value is not None else b.cushion_value,
        turf_moisture_goal=a.turf_moisture_goal if a.turf_moisture_goal is not None else b.turf_moisture_goal,
        turf_moisture_4corner=(
            a.turf_moisture_4corner if a.turf_moisture_4corner is not None else b.turf_moisture_4corner
        ),
        dirt_moisture_goal=a.dirt_moisture_goal if a.dirt_moisture_goal is not None else b.dirt_moisture_goal,
        dirt_moisture_4corner=(
            a.dirt_moisture_4corner if a.dirt_moisture_4corner is not None else b.dirt_moisture_4corner
        ),
    )
    # 測定日時は遅いほう（＝新しいほう）を残す
    return replace(merged, measured_at=max(a.measured_at, b.measured_at))


def _group_by_prefix(horses: list[dict], length: int) -> list[tuple[str, list[dict]]]:
    """馬を名前の頭 `length` 文字でまとめる（名前が短い馬はそのまま1組にする）。"""
    groups: dict[str, list[dict]] = {}
    for horse in horses:
        name = horse["horse_name"] or ""
        groups.setdefault(name[:length] or name, []).append(horse)
    return sorted(groups.items())


def _store_key(word: str) -> str:
    """保存HTMLのファイル名。馬名をそのまま使えないので16進にする。"""
    return word.encode("utf-8").hex()


class Updater:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: HttpClient,
        store: HtmlStore,
        *,
        run_id: int | None = None,
        today: date | None = None,
        force: bool = False,
    ) -> None:
        self.conn = conn
        self.client = client
        self.store = store
        self.run_id = run_id
        self.today = today or date.today()
        self.force = force
        self.n_races_saved = 0
        self.n_warnings = 0
        # 検索済みの頭文字 `{頭文字: 上限に達したか}`（血統の取り込みで使う）
        self._prefix_state: dict[str, bool] = {}

    # --- 共通 -----------------------------------------------------------------

    def _warn(self, key: str | None, url: str | None, message: str) -> None:
        """HTML構造変化を疑う警告。ログとparse_warningsテーブルの両方に残す。"""
        self.n_warnings += 1
        logger.warning("[HTML構造変化の可能性] %s: %s (%s)", key, message, url)
        db.log_warning(self.conn, self.run_id, key, url, message)

    def sync_calendar(self, start: date, end: date) -> None:
        for year, month in _months(start, end):
            if not self.force and db.is_calendar_month_final(self.conn, year, month):
                continue
            url = config.CALENDAR_URL.format(year=year, month=month)
            key = f"{year:04d}-{month:02d}"
            try:
                html = self.client.get_text(url, encoding="utf-8")
            except requests.RequestException as exc:
                logger.error("カレンダーの取得に失敗しました: %s: %s", key, exc)
                continue
            if html is None:
                self._warn(key, url, "カレンダーページが見つかりません(404)")
                continue
            try:
                days = parse_calendar(html)
            except LayoutError as exc:
                self._warn(key, url, str(exc))
                continue
            if not days:
                self._warn(key, url, "カレンダーから開催日が1件も取得できません")
                continue
            db.add_kaisai_days(self.conn, days)
            month_end = date(year, month, monthrange(year, month)[1])
            db.mark_calendar_month(self.conn, year, month, is_final=month_end < self.today)
            logger.info("カレンダー %s: 開催日%d日", key, len(days))

    def process_day(self, day: date) -> None:
        key = day.isoformat()
        url = config.RACE_LIST_URL.format(yyyymmdd=day.strftime("%Y%m%d"))
        try:
            html = self.client.get_text(url, encoding="utf-8")
        except requests.RequestException as exc:
            logger.error("レース一覧の取得に失敗しました: %s: %s", key, exc)
            return
        race_ids = parse_race_list(html) if html else []
        if not race_ids:
            if day < self.today:
                self._warn(key, url, "レース一覧からrace_idが1件も取得できません")
            db.update_kaisai_day(self.conn, day, 0, 0, completed=False)
            return

        # 「取得済み」は馬ごとの結果まで入っているもの（JRA由来のラップだけの行は含めない）
        already = set() if self.force else db.race_ids_with_results(self.conn, race_ids)
        statuses = {}
        for race_id in race_ids:
            if race_id in already:
                continue
            statuses[race_id] = self.fetch_race(race_id, day)

        n_saved = len(db.race_ids_with_results(self.conn, race_ids))
        n_unavailable = sum(1 for s in statuses.values() if s == UNAVAILABLE)
        # 全レース保存済み、または開催日から猶予日数が過ぎて残りが全て「結果なし」なら完了扱い
        grace_passed = day <= self.today - timedelta(days=RESULT_GRACE_DAYS)
        completed = n_saved == len(race_ids) or (
            grace_passed and n_saved + n_unavailable == len(race_ids)
        )
        db.update_kaisai_day(self.conn, day, len(race_ids), n_saved, completed)
        logger.info(
            "%s: %d/%dレース保存済み%s | 今回の保存%d件・リクエスト%d回",
            key, n_saved, len(race_ids), "" if completed else "（未完了）",
            self.n_races_saved, self.client.n_requests,
        )

    def fetch_race(self, race_id: str, day: date) -> str:
        url = config.RACE_RESULT_URL.format(race_id=race_id)
        html = None if self.force else self.store.get("race_result", race_id)
        from_store = html is not None
        if html is None:
            try:
                html = self.client.get_text(url, encoding="euc-jp")
            except requests.RequestException as exc:
                logger.error("結果ページの取得に失敗しました: %s: %s", race_id, exc)
                return ERROR
        if html is None:
            return self._unavailable(race_id, url, day, "結果ページが見つかりません(404)")

        try:
            page = parse_race_result(html, race_id)
        except LayoutError as exc:
            self._warn(race_id, url, str(exc))
            return ERROR
        if page is None:
            return self._unavailable(race_id, url, day, "結果ページに結果が掲載されていません")

        for message in page.warnings:
            self._warn(race_id, url, message)
        if page.race.get("race_date") and page.race["race_date"] != day.isoformat():
            self._warn(race_id, url, f"ページの開催日({page.race['race_date']})がレース一覧の日付と一致しません")
        try:
            db.save_race_page(self.conn, page)
        except (ValueError, sqlite3.Error) as exc:
            self._warn(race_id, url, f"保存に失敗しました: {exc}")
            return ERROR
        if not from_store:
            self.store.put("race_result", race_id, html)
        self.n_races_saved += 1
        logger.debug("保存: %s %s %s", race_id, page.race.get("race_name"), "(保存済みHTML)" if from_store else "")
        return SAVED

    def _unavailable(self, race_id: str, url: str, day: date, message: str) -> str:
        if day <= self.today - timedelta(days=RESULT_GRACE_DAYS):
            self._warn(race_id, url, message)
        else:
            logger.info("%s: まだ結果が確定していません（%s）", race_id, message)
        return UNAVAILABLE

    # --- コマンド ---------------------------------------------------------------

    def run_update(self) -> None:
        """前回までの続きから今日までを取得する。

        開始日は「DBの最新開催日の7日前」（途中で実行した日の取りこぼし対策）。ただし
        最大60日前まで（それより古い分はbackfillで取る）。DBが空なら14日前から。
        """
        latest = db.latest_race_date(self.conn)
        if latest is None:
            start = self.today - timedelta(days=config.DEFAULT_UPDATE_LOOKBACK_DAYS)
        else:
            start = min(latest - timedelta(days=UPDATE_OVERLAP_DAYS), self.today)
            floor = self.today - timedelta(days=UPDATE_MAX_LOOKBACK_DAYS)
            if start < floor:
                logger.info(
                    "DBの最新開催日(%s)が%d日以上前のため、updateは%s以降だけを対象にします。"
                    "それより前はbackfillで取得してください。",
                    latest, UPDATE_MAX_LOOKBACK_DAYS, floor,
                )
                start = floor
        logger.info("update: 対象期間 %s 〜 %s", start, self.today)
        self._run_range(start, self.today)

    def run_backfill(self, start: date, end: date | None = None) -> None:
        end = min(end or self.today, self.today)
        logger.info("backfill: 対象期間 %s 〜 %s", start, end)
        self._run_range(start, end)

    def _run_range(self, start: date, end: date) -> None:
        self.sync_calendar(start, end)
        if self.force:
            days = db.kaisai_days_between(self.conn, start, end)
        else:
            days = db.incomplete_kaisai_days(self.conn, start, end)
        logger.info("未完了の開催日: %d日", len(days))
        for i, day in enumerate(days, start=1):
            self.process_day(day)
            if i % 10 == 0:
                logger.info("進捗: %d/%d開催日", i, len(days))

    def run_reparse(self) -> None:
        """保存済みHTMLだけを使ってDBを作り直す（ネットにはアクセスしない）。"""
        for race_id in self.store.keys("race_result"):
            html = self.store.get("race_result", race_id)
            url = config.RACE_RESULT_URL.format(race_id=race_id)
            try:
                page = parse_race_result(html or "", race_id)
            except LayoutError as exc:
                self._warn(race_id, url, str(exc))
                continue
            if page is None:
                self._warn(race_id, url, "保存済みHTMLに結果が含まれていません")
                continue
            for message in page.warnings:
                self._warn(race_id, url, message)
            try:
                db.save_race_page(self.conn, page)
            except (ValueError, sqlite3.Error) as exc:
                self._warn(race_id, url, f"保存に失敗しました: {exc}")
                continue
            self.n_races_saved += 1
            if self.n_races_saved % 500 == 0:
                logger.info("reparse: %d件", self.n_races_saved)


    # --- 当日の馬場情報（クッション値・含水率） -------------------------------

    def fetch_track_conditions(self, today: date | None = None) -> int:
        """JRAの馬場情報ページから、**当日を含む直近の**クッション値・含水率を取り込む。

        アーカイブPDF（cushion.py）は開催後の公開なので、今週・前週はそちらでは埋まらない。
        このページは直近3開催ぶんを載せているので、その穴も一緒に埋まる。
        当日の**馬場状態（芝・ダート）と天候**も、馬場情報ページが読んでいるJSONから
        一緒に取る（まだ1レースも終わっていない時間帯は、これでしか分からない）。
        保存した（競馬場×日）の件数を返す。リクエストは3本。
        """
        today = today or date.today()
        measurements: dict[tuple[str, str], baba.BabaMeasurement] = {}
        for url, parse, kind in (
            (config.BABA_CUSHION_URL, baba.parse_cushion, "baba_cushion"),
            (config.BABA_MOISTURE_URL, baba.parse_moisture, "baba_moist"),
        ):
            try:
                html = self.client.get_text(url, encoding=config.BABA_ENCODING)
            except requests.RequestException as exc:
                logger.error("馬場情報の取得に失敗しました: %s: %s", url, exc)
                continue
            if not html:
                continue
            self.store.put(kind, today.isoformat(), html)
            try:
                parsed = baba.latest_by_day(parse(html, today))
            except LayoutError as exc:
                self._warn(f"{kind}:{today}", url, str(exc))
                continue
            for key, value in parsed.items():
                current = measurements.get(key)
                measurements[key] = _merge_measurements(current, value) if current else value

        # 当日の馬場状態・天候（全場ぶんが1リクエストで返るJSON）
        conditions: dict[tuple[str, str], baba.CourseCondition] = {}
        try:
            text = self.client.post_text(
                config.BABA_CONDITION_URL, {"CNAME": config.BABA_CONDITION_CNAME},
                encoding=config.BABA_ENCODING,
            )
        except requests.RequestException as exc:
            logger.error("当日の馬場状態の取得に失敗しました: %s", exc)
            text = None
        if text:
            self.store.put("jra_course_condition", today.isoformat(), text)
            try:
                for condition in baba.parse_course_conditions(text):
                    conditions[(condition.venue_name, condition.date)] = condition
            except LayoutError as exc:
                self._warn(f"baba_going:{today}", config.BABA_CONDITION_URL, str(exc))

        venue_codes = {
            row["venue_name"]: row["venue_code"]
            for row in self.conn.execute("SELECT venue_code, venue_name FROM venues")
        }
        # 馬場状態だけ分かっている日（測定値がまだ出ていない時間帯）も保存できるように、
        # 測定値のキーと馬場状態のキーを合わせる
        keys = set(measurements) | set(conditions)
        records = []
        for (venue_name, day) in sorted(keys):
            m = measurements.get((venue_name, day))
            condition = conditions.get((venue_name, day))
            venue_code = venue_codes.get(venue_name)
            if not venue_code:
                self._warn(f"baba:{day}", config.BABA_CUSHION_URL, f"知らない競馬場です: {venue_name}")
                continue
            records.append(TrackCondition(
                venue_code=venue_code, date=day, nichime=None, day_label=None,
                is_pre_meeting_day=False, course_setting=None,
                cushion_value=m.cushion_value if m else None,
                turf_moisture_goal=m.turf_moisture_goal if m else None,
                turf_moisture_4corner=m.turf_moisture_4corner if m else None,
                dirt_moisture_goal=m.dirt_moisture_goal if m else None,
                dirt_moisture_4corner=m.dirt_moisture_4corner if m else None,
                source_pdf=config.BABA_CUSHION_URL,
                measured_at=m.measured_at if m else None,
                going_turf=condition.going_turf if condition else None,
                going_dirt=condition.going_dirt if condition else None,
                weather=condition.weather if condition else None,
            ))
        # 開催◯日目や使用コースA/B/Cはこのページに無いので、PDF由来の値を消さないようにする
        saved = db.upsert_track_conditions(self.conn, records, keep_existing=True)
        logger.info("馬場情報: %d件（%d競馬場）", saved, len({r.venue_code for r in records}))
        return saved

    # --- 当日・前日のレース結果（JRA公式） -----------------------------------

    def fetch_jra_results(self, days: list[date] | None = None) -> int:
        """JRA公式のレース結果ページから、**レース情報とラップだけ**を取り込む。

        netkeibaの結果ページは当日・前日の分がまだ公開されないため、その日の馬場差を
        測る材料が手に入らない。JRAはレース後すぐ公開され、しかも1開催ぶんが1ページに
        まとまっているので、`2 + 2×開催数` リクエストで済む（既定の当日＋前日なら10本弱）。

        馬ごとの結果は入れない（JRAは馬のnetkeiba IDを持たないため）。その分は後から
        `keiba update` が同じレースに補完する。保存したレース数を返す。
        """
        days = days or [self.today, self.today - timedelta(days=1)]
        wanted = {d.isoformat() for d in days}

        index_html = self._jra_page(
            self._jra_result_index_cname(), "jra_result_index", self.today.isoformat()
        )
        if not index_html:
            return 0
        try:
            meetings = [m for m in jra_result.parse_result_index(index_html) if m.date in wanted]
        except LayoutError as exc:
            self._warn(f"jra_index:{self.today}", config.JRA_RESULT_URL, str(exc))
            return 0
        if not meetings:
            logger.info("JRA結果: %s の開催はありません", "・".join(sorted(wanted)))
            return 0

        saved = 0
        for meeting in meetings:
            saved += self._fetch_jra_meeting(meeting)
        logger.info("JRA結果: %d開催・%dレースを保存しました", len(meetings), saved)
        return saved

    def _jra_result_index_cname(self) -> str:
        """トップページから「レース結果」への入口トークンを拾う。"""
        try:
            html = self.client.get_text(config.JRA_TOP_URL, encoding=config.JRA_RESULT_ENCODING)
        except requests.RequestException as exc:
            logger.error("JRAトップページの取得に失敗しました: %s", exc)
            return config.JRA_RESULT_INDEX_CNAME
        cname = jra_result.find_index_cname(html or "")
        if cname is None:
            self._warn("jra_top", config.JRA_TOP_URL, "トップページにレース結果への入口がありません")
            return config.JRA_RESULT_INDEX_CNAME
        return cname

    def _jra_page(self, cname: str, kind: str, key: str, url: str | None = None) -> str | None:
        """`cname` をPOSTしてページを取り、HTMLを保存して返す（出馬表は `url` を変える）。"""
        url = url or config.JRA_RESULT_URL
        try:
            html = self.client.post_text(
                url, {"cname": cname}, encoding=config.JRA_RESULT_ENCODING
            )
        except requests.RequestException as exc:
            logger.error("JRAのページ取得に失敗しました: %s: %s", cname, exc)
            return None
        if html:
            self.store.put(kind, key, html)
        return html

    def _fetch_jra_meeting(self, meeting: jra_result.Meeting) -> int:
        """1開催ぶん（レース選択 → 結果一覧）を取り込み、保存したレース数を返す。"""
        menu_html = self._jra_page(meeting.cname, "jra_race_menu", meeting.key)
        if not menu_html:
            return 0
        list_cname = jra_result.parse_race_menu(menu_html)
        if list_cname is None:
            self._warn(f"jra_menu:{meeting.key}", config.JRA_RESULT_URL,
                       "レース選択ページに結果一覧へのリンクがありません")
            return 0

        list_html = self._jra_page(list_cname, "jra_result_list", meeting.key)
        if not list_html:
            return 0
        try:
            races = jra_result.parse_meeting_results(list_html, meeting)
        except LayoutError as exc:
            self._warn(f"jra_list:{meeting.key}", config.JRA_RESULT_URL, str(exc))
            return 0

        saved = 0
        for race in races:
            for message in race.warnings:
                self._warn(race.race["race_id"], config.JRA_RESULT_URL, message)
            db.save_jra_race(self.conn, race.race, race.laps)
            saved += 1
        self.n_races_saved += saved
        # レース選択ページには、レースごとのJRAのページへのリンクも載っている
        # （馬柱の映像リンクに使う）。レースを保存したあとに入れる。
        db.save_jra_race_links(self.conn, jra_result.parse_race_links(menu_html, meeting))
        logger.info(
            "JRA結果 %s %s: %dレース（ラップ%d件）",
            meeting.date, meeting.venue_name or meeting.venue_code, saved,
            sum(1 for r in races if r.laps),
        )
        return saved

    def fetch_jra_race_links(self, start: date, end: date | None = None) -> int:
        """過去レースぶんの「JRA公式の結果ページへのリンク」を集める。

        馬柱の各過去走から、そのレースのJRAのページ（＝レース映像が見られるページ）へ
        飛べるようにするための下ごしらえ。リンクのトークンは末尾がチェックサムなので
        **自力では組み立てず**、開催ごとの「レース選択」ページから拾う。

        リクエストは `2 + 月数 + 開催数`。まだリンクが入っていない開催だけを回るので、
        `--max-requests` で途中で止めても、次の実行が続きから進む。保存した件数を返す。
        """
        end = end or self.today
        needed = db.races_without_jra_link(self.conn, start.isoformat(), end.isoformat())
        if not needed:
            logger.info("JRAリンク: %s〜%s は埋まっています", start, end)
            return 0

        months = self._jra_month_links()
        if not months:
            return 0

        saved = 0
        for year_month in sorted({day[:7] for day, _ in needed}):
            cname = months.get(year_month)
            if cname is None:
                self._warn(f"jra_month:{year_month}", config.JRA_RESULT_URL, "その月のリンクがありません")
                continue
            month_html = self._jra_page(cname, "jra_month", year_month)
            if not month_html:
                continue
            try:
                meetings = jra_result.parse_result_index(month_html)
            except LayoutError as exc:
                self._warn(f"jra_month:{year_month}", config.JRA_RESULT_URL, str(exc))
                continue
            for meeting in meetings:
                if (meeting.date, meeting.venue_code) not in needed:
                    continue
                menu_html = self._jra_page(meeting.cname, "jra_race_menu", meeting.key)
                if not menu_html:
                    continue
                links = jra_result.parse_race_links(menu_html, meeting)
                saved += db.save_jra_race_links(self.conn, links)
                logger.info("JRAリンク %s %s: %d件", meeting.date, meeting.venue_name or "", len(links))
        logger.info("JRAリンク: %dレース分を保存しました", saved)
        return saved

    def _jra_month_links(self) -> dict[str, str]:
        """過去レース結果検索ページから、月ごとの開催一覧へのトークンを取る。"""
        try:
            top_html = self.client.get_text(config.JRA_TOP_URL, encoding=config.JRA_RESULT_ENCODING)
        except requests.RequestException as exc:
            logger.error("JRAトップページの取得に失敗しました: %s", exc)
            return {}
        cname = jra_result.find_past_search_cname(top_html or "")
        if cname is None:
            self._warn("jra_top", config.JRA_TOP_URL, "トップページに過去レース結果検索がありません")
            return {}
        html = self._jra_page(cname, "jra_past_search", self.today.isoformat())
        return jra_result.parse_month_links(html) if html else {}

    def fetch_jra_entries(self, days: list[date] | None = None) -> int:
        """JRA公式の出馬表から、**血統・馬主・生産牧場**を取り込む。

        netkeibaの出馬表にはこれらが無いので、JRA側から補う。辿り方は結果と同じ3段
        （トップ → 開催選択 → レース選択 → 各レースの出馬表）で、
        リクエストは `2 + 開催数 + レース数`。保存できた頭数を返す。
        """
        wanted = {d.isoformat() for d in days} if days else None

        cname = self._jra_entry_index_cname()
        index_html = self._jra_page(
            cname, "jra_entry_index", self.today.isoformat(), url=config.JRA_ENTRY_URL
        )
        if not index_html:
            return 0
        try:
            meetings = jra_result.parse_entry_index(index_html)
        except LayoutError as exc:
            self._warn(f"jra_entries:{self.today}", config.JRA_ENTRY_URL, str(exc))
            return 0
        if wanted is not None:
            meetings = [m for m in meetings if m.date in wanted]
        if not meetings:
            logger.info("JRA出馬表: 対象の開催がありません")
            return 0

        saved = 0
        for meeting in meetings:
            saved += self._fetch_jra_entry_meeting(meeting)
        logger.info("JRA出馬表: %d開催・%d頭の血統と馬主を保存しました", len(meetings), saved)
        return saved

    def _jra_entry_index_cname(self) -> str:
        """トップページから「出馬表」への入口トークンを拾う。"""
        try:
            html = self.client.get_text(config.JRA_TOP_URL, encoding=config.JRA_RESULT_ENCODING)
        except requests.RequestException as exc:
            logger.error("JRAトップページの取得に失敗しました: %s", exc)
            return config.JRA_ENTRY_INDEX_CNAME
        cname = jra_result.find_index_cname(html or "", marker="dli")
        if cname is None:
            self._warn("jra_top", config.JRA_TOP_URL, "トップページに出馬表への入口がありません")
            return config.JRA_ENTRY_INDEX_CNAME
        return cname

    def _fetch_jra_entry_meeting(self, meeting: jra_result.Meeting) -> int:
        """1開催ぶん（レース選択 → 各レースの出馬表）。保存できた頭数を返す。"""
        menu_html = self._jra_page(meeting.cname, "jra_entry_menu", meeting.key, url=config.JRA_ENTRY_URL)
        if not menu_html:
            return 0
        links = jra_result.parse_race_links(menu_html, meeting, kind="dde")
        if not links:
            self._warn(f"jra_entry_menu:{meeting.key}", config.JRA_ENTRY_URL,
                       "レース選択ページに出馬表へのリンクがありません")
            return 0
        db.save_jra_entry_links(self.conn, links)   # 1レースだけ取り直すときに使い回す

        saved = 0
        for race_id, cname in sorted(links.items()):
            saved += self._fetch_jra_entry_page(race_id, cname) or 0
        logger.info("JRA出馬表 %s %s: %d頭", meeting.date, meeting.venue_name or "", saved)
        return saved

    def fetch_jra_entry_race(self, race_id: str) -> int:
        """1レースぶんのJRA公式出馬表から、血統・馬主・生産牧場と**馬体重**を取り込む。

        馬体重はnetkeibaの出馬表では埋まらないので、こちらから入れる（当日発表なので、
        発走の1時間ほど前より早いとまだ載っていない＝「—」のままが正常）。

        トークン（cname）がDBにあれば**1リクエスト**。無ければオッズと同じ3段
        （トップ → 開催選択 → レース選択）でその開催の全レースぶんをためてから取りに行く。
        保存できた頭数を返す。
        """
        cname = db.get_jra_entry_link(self.conn, race_id)
        if cname is not None:
            # ためてあるトークンで読めないのは「古くなった」ことが多いので、
            # ここでは構造変化の警告を出さない（辿り直してから判断する）
            saved = self._fetch_jra_entry_page(race_id, cname, warn=False)
            if saved is not None:
                return saved
            logger.info("JRA出馬表 %s: トークンが古いようなので辿り直します", race_id)
        cname = self._fetch_jra_entry_links(race_id)
        if cname is None:
            return 0
        return self._fetch_jra_entry_page(race_id, cname) or 0

    def _fetch_jra_entry_links(self, race_id: str) -> str | None:
        """そのレースの出馬表を開くトークンを、開催選択 → レース選択と辿って集める。

        `race_id` に場・開催回・日目が入っているので、日付を知らなくても開催を選べる
        （オッズの `_fetch_odds_links` と同じ考え方）。集めたトークンはそのレースだけでなく
        **その開催の全レースぶん**をためる。
        """
        info = decode_race_id(race_id)
        index_html = self._jra_page(
            self._jra_entry_index_cname(), "jra_entry_index", self.today.isoformat(),
            url=config.JRA_ENTRY_URL,
        )
        if not index_html:
            return None
        try:
            meetings = jra_result.parse_entry_index(index_html)
        except LayoutError as exc:
            self._warn(f"jra_entries:{self.today}", config.JRA_ENTRY_URL, str(exc))
            return None
        found = [
            m for m in meetings
            if (m.venue_code, m.kaiji, m.nichime) == (info.venue_code, info.kaiji, info.nichime)
        ]
        if not found:
            logger.info("JRA出馬表 %s: その開催の出馬表は今出ていません", race_id)
            return None

        menu_html = self._jra_page(
            found[0].cname, "jra_entry_menu", found[0].key, url=config.JRA_ENTRY_URL
        )
        if not menu_html:
            return None
        links = jra_result.parse_race_links(menu_html, found[0], kind="dde")
        if not links:
            self._warn(f"jra_entry_menu:{found[0].key}", config.JRA_ENTRY_URL,
                       "レース選択ページに出馬表へのリンクがありません")
            return None
        db.save_jra_entry_links(self.conn, links)
        return links.get(race_id)

    def _fetch_jra_entry_page(self, race_id: str, cname: str, *, warn: bool = True) -> int | None:
        """出馬表1ページを取って保存する。保存できた頭数、読めなければ `None` を返す。

        `None` は「ためてあるトークンが古いかもしれない」の合図で、
        呼び手が辿り直すために使う（取れた頭数0とは区別する）。
        `warn=False` のときは読めなくても構造変化の警告を出さない。
        """
        url = config.JRA_RACE_ENTRY_URL.format(cname=cname)
        try:
            html = self.client.get_text(url, encoding=config.JRA_RESULT_ENCODING)
        except requests.RequestException as exc:
            logger.error("JRAの出馬表の取得に失敗しました: %s: %s", race_id, exc)
            return None
        if not html:
            return None
        self.store.put("jra_shutuba", race_id, html)
        try:
            profiles = jra_result.parse_shutuba_profiles(html)
        except LayoutError as exc:
            if warn:
                self._warn(race_id, url, str(exc))
            return None
        saved = db.save_horse_profiles(self.conn, race_id, profiles)
        weights = db.save_upcoming_weights(self.conn, race_id, profiles)
        logger.info("JRA出馬表 %s: %d頭（馬体重 %d頭）", race_id, saved, weights)
        return saved

    # --- オッズ（JRA公式） -----------------------------------------------------

    def fetch_odds(self, race_id: str, bet_types: list[str] | None = None) -> dict[str, int]:
        """1レースのオッズを券種ごとに取り込む。`{券種: 点数}` を返す。

        トークン（cname）がすでにDBにあれば**券種1つ＝1リクエスト**で済む。無ければ
        トップ → 開催選択 → レース選択 の3リクエストで、その開催の全レース×全券種ぶんの
        トークンをまとめて拾ってから取りに行く。

        単勝と複勝は同じページなので、両方を指定してもリクエストは1回。
        まだ発売されていない券種はトークンが無いので、黙って飛ばす（前日は重賞だけ、など）。
        """
        keys = list(bet_types or [bet.key for bet in jra_odds.BET_TYPES])
        links = db.get_jra_odds_links(self.conn, race_id)
        if any(key not in links for key in keys):
            links = {**links, **self._fetch_odds_links(race_id)}

        saved: dict[str, int] = {}
        pages: dict[str, str] = {}  # cname → HTML（単複は1ページで2券種ぶん読む）
        for key in keys:
            bet = jra_odds.BET_TYPE_BY_KEY[key]
            cname = links.get(key)
            if cname is None:
                logger.info("オッズ %s %s: まだ発売されていません", race_id, bet.label)
                continue
            html = pages.get(cname)
            if html is None:
                html = self._jra_page(
                    cname, f"jra_odds_{key}", race_id, url=config.JRA_ODDS_URL
                )
                if not html:
                    continue
                pages[cname] = html
            try:
                page = jra_odds.parse_odds(html, key, race_id)
            except LayoutError as exc:
                self._warn(f"odds:{race_id}:{key}", config.JRA_ODDS_URL, str(exc))
                continue
            for message in page.warnings:
                self._warn(f"odds:{race_id}:{key}", config.JRA_ODDS_URL, message)
            saved[key] = db.save_odds(self.conn, page)
            # そのページに載っていた7券種ぶんのリンクで、ためてあるトークンを直し続ける
            if page.links:
                db.save_jra_odds_links(self.conn, {race_id: page.links})
                links = {**links, **page.links}
            logger.info(
                "オッズ %s %s: %d点（%s）", race_id, bet.label, saved[key],
                page.odds_label or "時刻不明",
            )
        return saved

    def _fetch_odds_links(self, race_id: str) -> dict[str, str]:
        """そのレースのオッズを開くトークンを、開催選択→レース選択と辿って集める。

        `race_id` には場・開催回・日目が入っているので、開催選択ページの一覧と
        突き合わせれば日付を知らなくても開催を選べる。
        """
        info = decode_race_id(race_id)
        index_html = self._jra_page(
            self._jra_odds_index_cname(), "jra_odds_index", self.today.isoformat(),
            url=config.JRA_ODDS_URL,
        )
        if not index_html:
            return {}
        try:
            meetings = jra_odds.parse_odds_index(index_html)
        except LayoutError as exc:
            self._warn(f"odds_index:{self.today}", config.JRA_ODDS_URL, str(exc))
            return {}
        found = [
            m for m in meetings
            if (m.venue_code, m.kaiji, m.nichime) == (info.venue_code, info.kaiji, info.nichime)
        ]
        if not found:
            logger.info("オッズ %s: その開催は今オッズを出していません", race_id)
            return {}

        menu_html = self._jra_page(
            found[0].cname, "jra_odds_menu", found[0].key, url=config.JRA_ODDS_URL
        )
        if not menu_html:
            return {}
        links = jra_odds.parse_race_links(menu_html, found[0])
        db.save_jra_odds_links(self.conn, links)
        logger.info(
            "オッズのリンク %s %s: %dレースぶん",
            found[0].date, found[0].venue_name or found[0].venue_code, len(links),
        )
        return links.get(race_id, {})

    def _jra_odds_index_cname(self) -> str:
        """トップページから「オッズ」への入口トークンを拾う。"""
        try:
            html = self.client.get_text(config.JRA_TOP_URL, encoding=config.JRA_RESULT_ENCODING)
        except requests.RequestException as exc:
            logger.error("JRAトップページの取得に失敗しました: %s", exc)
            return config.JRA_ODDS_INDEX_CNAME
        cname = jra_odds.find_index_cname(html or "")
        if cname is None:
            self._warn("jra_top", config.JRA_TOP_URL, "トップページにオッズへの入口がありません")
            return config.JRA_ODDS_INDEX_CNAME
        return cname

    # --- 血統（JRA公式の競走馬検索） -------------------------------------------

    def fetch_horse_pedigree(self, limit_horses: int | None = None) -> int:
        """父がまだ入っていない馬の血統を、JRA公式の競走馬検索から埋める。

        手元のDBには2023年以降の全走があるので、**父さえ埋まれば**種牡馬ごとの
        距離適性・BMS相性・代表産駒がそのまま出せる。

        2段階で進む:

        1. **頭文字でまとめて検索**して、馬ごとの詳細ページのトークンを拾う
           （「で始まる」検索なので、2文字の頭で最大200頭ぶんが1リクエストで返る）
        2. トークンが取れた馬の詳細ページを1頭1リクエストで読む

        途中で止まっても、次に呼んだときは**父が空の馬**から始まるので続きから進む。
        `PoliteSession` のリクエスト上限に当たると `RequestBudgetExceeded` が飛ぶ。
        """
        pending = db.horses_without_pedigree(self.conn, limit_horses)
        if not pending:
            logger.info("血統: 埋めるべき馬はありません")
            return 0
        done, total = db.count_pedigree(self.conn)
        logger.info("血統: %d頭/%d頭が済み。残り%d頭のうち%d頭を対象にします",
                    done, total, total - done, len(pending))

        links = db.get_jra_horse_links(self.conn, [h["horse_id"] for h in pending])
        self._prefix_state = db.searched_prefixes(self.conn)

        # **検索と詳細を交互に進める。** 先に全部の検索を済ませようとすると、
        # 頭数が多いうちはリクエスト上限がそこで尽きて「1頭も保存できない実行」になる。
        # 頭文字のまとまりごとに「検索 → その場で詳細」とすれば、途中で止まっても
        # そこまでのぶんは必ず保存されている。
        saved = self._fetch_horse_pages(
            [h for h in pending if h["horse_id"] in links], links
        )                                   # トークンが前の実行で取れている馬が先
        rest = [h for h in pending if h["horse_id"] not in links]
        for prefix, group in _group_by_prefix(rest, config.JRA_HORSE_PREFIX_MIN):
            self._resolve_prefix(prefix, group, links)
            saved += self._fetch_horse_pages(
                [h for h in group if h["horse_id"] in links], links
            )
        logger.info("血統: %d頭ぶんを保存しました", saved)
        return saved

    def _fetch_horse_pages(self, horses: list[dict], links: dict[str, str]) -> int:
        """トークンが取れている馬の詳細ページをまとめて取り込む。保存できた頭数を返す。

        **1頭ごとに `n_races_saved` を進める。** リクエスト上限に当たると
        `RequestBudgetExceeded` がここから飛び出すので、まとめて足すと
        「保存0件」と記録されてしまう（DBには入っているのに）。
        """
        saved = 0
        for horse in horses:
            cname = links.get(horse["horse_id"])
            if cname is not None and self._fetch_horse_page(horse, cname):
                saved += 1
                self.n_races_saved += 1     # 実行履歴の「保存件数」は頭数で数える
        return saved

    def _resolve_prefix(self, prefix: str, targets: list[dict], links: dict[str, str]) -> None:
        """1つの頭文字ぶん。上限に達していたら頭を1文字伸ばして掘り直す。"""
        targets = [h for h in targets if h["horse_id"] not in links]
        if not targets:
            return
        capped = self._prefix_state.get(prefix)
        if capped is None:                     # まだ引いていない頭文字
            page = self._search_horses(prefix)
            if page is None:
                return
            self._match_hits(page.hits, targets, links)
            db.save_jra_horse_search(self.conn, prefix, len(page.hits), page.capped)
            self._prefix_state[prefix] = page.capped
            capped = page.capped
        if not capped:
            # 取りこぼしが無い検索で見つからなかった＝JRAの競走馬検索に居ない
            # （地方・海外のみの馬など）。これ以上掘っても出てこない
            return

        remaining = [h for h in targets if h["horse_id"] not in links]
        if not remaining:
            return
        if len(prefix) >= config.JRA_HORSE_PREFIX_MAX:
            # これ以上頭を伸ばさず、残りは馬名そのもので1頭ずつ引く
            for horse in remaining:
                page = self._search_horses(horse["horse_name"])
                if page is not None:
                    self._match_hits(page.hits, [horse], links)
            return
        for longer, group in _group_by_prefix(remaining, len(prefix) + 1):
            self._resolve_prefix(longer, group, links)

    def _search_horses(self, word: str):
        """競走馬検索を1回。トークンは組み立てられるが、**cp932で送る**必要がある。"""
        try:
            cname = jra_horse.build_search_cname(word, config.JRA_HORSE_SEARCH_CNAME)
        except ValueError as exc:
            self._warn(f"pedigree:{word}", config.JRA_HORSE_SEARCH_URL, str(exc))
            return None
        try:
            html = self.client.post_text(
                config.JRA_HORSE_SEARCH_URL,
                {"cname": cname.encode(config.JRA_RESULT_ENCODING, "replace")},
                encoding=config.JRA_RESULT_ENCODING,
            )
        except requests.RequestException as exc:
            logger.error("競走馬検索に失敗しました: %s: %s", word, exc)
            return None
        if not html:
            return None
        self.store.put("jra_horse_search", _store_key(word), html)
        try:
            page = jra_horse.parse_horse_search(html, word, config.JRA_HORSE_SEARCH_LIMIT)
        except LayoutError as exc:
            self._warn(f"pedigree:{word}", config.JRA_HORSE_SEARCH_URL, str(exc))
            return None
        for message in page.warnings:
            self._warn(f"pedigree:{word}", config.JRA_HORSE_SEARCH_URL, message)
        logger.debug("競走馬検索 %s: %d頭%s", word, len(page.hits), "（上限）" if page.capped else "")
        return page

    def _match_hits(self, hits: list, targets: list[dict], links: dict[str, str]) -> None:
        """検索結果を手元の馬に突き合わせる。**決め手は馬名＋生年**。

        JRAは同じ世代に同じ馬名を付けないので、馬名と生年が合えば1頭に決まる
        （実例: コントレイルは2017年生の牡と2010年生の牝の2頭いる）。
        生年は血統登録番号の先頭4桁で、**抹消馬の「馬齢」は使えない**（抹消時点で
        止まっているため）。生年が分からないときだけ性別で絞る。
        """
        by_name: dict[str, list] = {}
        for hit in hits:
            by_name.setdefault(hit.name, []).append(hit)

        found: dict[str, tuple[str, str]] = {}
        for horse in targets:
            candidates = by_name.get(horse["horse_name"])
            if not candidates:
                continue
            if horse.get("birth_year"):
                candidates = [c for c in candidates if c.birth_year == horse["birth_year"]]
            elif horse.get("sex") and len(candidates) > 1:
                candidates = [c for c in candidates if c.sex == horse["sex"]]
            if len(candidates) != 1:
                self._warn(
                    f"pedigree:{horse['horse_name']}", config.JRA_HORSE_SEARCH_URL,
                    f"{horse['horse_name']}（{horse.get('birth_year')}年生）に当たる馬が"
                    f"{len(candidates)}頭あり、決められませんでした",
                )
                continue
            hit = candidates[0]
            links[horse["horse_id"]] = hit.cname
            found[horse["horse_id"]] = (hit.cname, hit.horse_no)
        db.save_jra_horse_links(self.conn, found)

    def _fetch_horse_page(self, horse: dict, cname: str) -> bool:
        """競走馬の詳細ページを1頭ぶん取り込む。保存できたらTrue。

        **すでに保存してあるHTMLがあれば取りに行かない。** 血統は後から変わらないので、
        パーサを直したあとの取り直しがリクエスト0回で済む（`--force` で取り直せる）。
        """
        url = config.JRA_HORSE_PAGE_URL.format(cname=cname)
        html = None if self.force else self.store.get("jra_horse", horse["horse_id"])
        if html is None:
            try:
                html = self.client.get_text(url, encoding=config.JRA_RESULT_ENCODING)
            except requests.RequestException as exc:
                logger.error("競走馬ページの取得に失敗しました: %s: %s", horse["horse_name"], exc)
                return False
            if not html:
                return False
            self.store.put("jra_horse", horse["horse_id"], html)
        try:
            detail = jra_horse.parse_horse_profile(html)
        except LayoutError as exc:
            self._warn(f"pedigree:{horse['horse_name']}", url, str(exc))
            return False
        # 別の馬のページを開いていないか、馬名で確かめる
        if detail.profile.horse_name and detail.profile.horse_name != horse["horse_name"]:
            self._warn(
                f"pedigree:{horse['horse_name']}", url,
                f"{horse['horse_name']}のはずが{detail.profile.horse_name}のページでした",
            )
            return False
        db.save_horse_pedigree(self.conn, horse["horse_id"], detail)
        return True

    # --- 5代血統表（netkeiba） -------------------------------------------------

    def fetch_bloodlines(self, limit_horses: int | None = None) -> int:
        """出走した馬の5代血統表を取り込む。**保存できた頭数**を返す。

        手元の `horse_id` が血統登録番号そのものなので、検索を挟まず
        **1頭1リクエスト**で開ける。血統は後から変わらないので、保存済みHTMLが
        あれば取りに行かない（`--force` で取り直せる）。

        途中で止まっても、次は血統表がまだ入っていない馬から始まるので続きから進む。
        """
        pending = db.horses_without_bloodline(self.conn, limit_horses)
        if not pending:
            logger.info("5代血統表: 埋めるべき馬はありません")
            return 0
        done, total = db.count_bloodline(self.conn)
        logger.info("5代血統表: %d頭/%d頭が済み。残り%d頭のうち%d頭を対象にします",
                    done, total, total - done, len(pending))

        saved = 0
        for horse in pending:
            if self._fetch_bloodline(horse):
                saved += 1
                # **1頭ごとに数える。** まとめて足すと、リクエスト上限に当たったときに
                # 「保存0件」と記録されてしまう（DBには入っているのに）
                self.n_races_saved += 1
        logger.info("5代血統表: %d頭ぶんを保存しました", saved)
        return saved

    def _fetch_bloodline(self, horse: dict) -> bool:
        """1頭ぶんの5代血統表を取り込む。保存できたらTrue。"""
        horse_id = horse["horse_id"]
        url = config.NETKEIBA_PED_URL.format(horse_id=horse_id)
        html = None if self.force else self.store.get("horse_ped", horse_id)
        if html is None:
            try:
                html = self.client.get_text(url, encoding=config.NETKEIBA_PED_ENCODING)
            except requests.RequestException as exc:
                logger.error("5代血統表の取得に失敗しました: %s: %s", horse.get("horse_name"), exc)
                return False
            if not html:
                return False
            self.store.put("horse_ped", horse_id, html)
        try:
            cells = netkeiba_ped.parse_pedigree(html)
        except LayoutError as exc:
            self._warn(f"bloodline:{horse.get('horse_name')}", url, str(exc))
            return False
        db.save_horse_pedigree_tree(self.conn, horse_id, cells)
        self._fill_close_family(horse_id, cells)
        return True

    def _fill_close_family(self, horse_id: str, cells: list) -> None:
        """血統表の父(`f`)・母(`m`)・母の父(`mf`)で、`horses` の**空欄だけ**を埋める。

        JRA公式の取り込み（`keiba pedigree`）が追いつく前でも、種牡馬分析の
        距離適性やBMS相性が使えるようにするため。
        **すでに値が入っている列は触らない**（JRA由来の表記を正とする）。
        列ごとに独立して埋めたいので `CASE WHEN` で1列ずつ見る。
        """
        by_path = {c.path: c.name for c in cells}
        columns = {"sire": by_path.get("f"), "dam": by_path.get("m"),
                   "broodmare_sire": by_path.get("mf")}
        filled = {column: name for column, name in columns.items() if name}
        if not filled:
            return
        sets = ", ".join(
            f"{column} = CASE WHEN {column} IS NULL OR {column} = '' THEN ? ELSE {column} END"
            for column in filled
        )
        with self.conn:
            self.conn.execute(
                f"UPDATE horses SET {sets} WHERE horse_id = ?", (*filled.values(), horse_id)
            )

    # --- 種牡馬リーディング（JRA公式） -----------------------------------------

    def fetch_sire_leading(
        self, years: list[int], kind: str = jra_sire.KIND_ALL, *, pages: int | None = None,
    ) -> int:
        """種牡馬リーディングを年度ごとに取り込む。**保存した行数**を返す。

        1年ぶん＝5ページ（上位100頭）。トークン（cname）がすでにDBにあれば入口を
        辿らずに済むので、1年あたり5リクエストちょうどで終わる。

        終わった年は数字が変わらないので、**すでに100頭ぶん入っている年は飛ばす**
        （`--force` を付けると取り直す）。当年だけは集計途中なので毎回取り直す。
        """
        pages = pages or config.SIRE_LEADING_PAGES
        links = db.get_jra_sire_links(self.conn, kind)
        wanted = sorted(set(years))
        todo = [y for y in wanted if self.force or not self._sire_year_done(kind, y, pages)]
        if not todo:
            logger.info("種牡馬リーディング(%s): %s年は取り込み済みです", kind, "・".join(map(str, wanted)))
            return 0
        if any(year not in links for year in todo):
            links = {**links, **self._fetch_sire_year_links(kind)}

        saved = 0
        for year in todo:
            cname = (links.get(year) or {}).get(1)
            if cname is None:
                logger.info("種牡馬リーディング(%s): %d年のリンクがありません（遡れる範囲外）", kind, year)
                continue
            saved += self._fetch_sire_year(kind, year, cname, pages)
        return saved

    def _sire_year_done(self, kind: str, year: int, pages: int) -> bool:
        """その年がもう埋まっているか。当年は集計途中なので常に未完とみなす。"""
        if year >= self.today.year:
            return False
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sire_leading WHERE kind = ? AND year = ?", (kind, year)
        ).fetchone()
        return bool(row) and row["n"] >= pages * 20

    def _fetch_sire_year(self, kind: str, year: int, cname: str, pages: int) -> int:
        """1年ぶん（1ページ目 → ページャを辿って残り）を取り込む。"""
        page_html = self._jra_page(cname, "jra_sire_leading", f"{kind}{year}p1",
                                   url=config.JRA_SIRE_URL)
        if not page_html:
            return 0
        first = self._parse_sire_page(page_html, kind, year, 1)
        if first is None:
            return 0
        # 1ページ目が残りのページのトークンを持っている（自力では組み立てられない）
        db.save_jra_sire_links(self.conn, kind, {year: {1: cname, **first.page_cnames}})
        saved = db.save_sire_leading(self.conn, first)

        for page in range(2, pages + 1):
            page_cname = first.page_cnames.get(page)
            if page_cname is None:
                break                      # 100頭に満たない年（古い年度）はここで終わる
            html = self._jra_page(page_cname, "jra_sire_leading", f"{kind}{year}p{page}",
                                  url=config.JRA_SIRE_URL)
            if not html:
                break
            parsed = self._parse_sire_page(html, kind, year, page)
            if parsed is None:
                break
            saved += db.save_sire_leading(self.conn, parsed)
        logger.info("種牡馬リーディング(%s) %d年: %d頭（%s現在）", kind, year, saved,
                    first.as_of or "時点不明")
        self.n_races_saved += 1            # 実行履歴の「保存件数」は年単位で数える
        return saved

    def _parse_sire_page(self, html: str, kind: str, year: int, page: int):
        """パースして警告を記録する。構造が変わっていたらNoneを返す。"""
        key = f"sire:{kind}:{year}:{page}"
        try:
            parsed = jra_sire.parse_sire_leading(html)
        except LayoutError as exc:
            self._warn(key, config.JRA_SIRE_URL, str(exc))
            return None
        for message in parsed.warnings:
            self._warn(key, config.JRA_SIRE_URL, message)
        if parsed.year != year:
            # 年度のトークンが指していた年と見出しが食い違う（JRA側の作りが変わった疑い）
            self._warn(key, config.JRA_SIRE_URL,
                       f"{year}年のはずが見出しは{parsed.year}年でした")
            return None
        return parsed

    def _fetch_sire_year_links(self, kind: str) -> dict[int, dict[int, str]]:
        """リーディング情報のページ → 種牡馬リーディング と辿って、年度ごとのトークンを集める。

        種牡馬リーディングのページは**年度セレクタに全年分のトークンを持っている**ので、
        入口を1回辿るだけで2009年まで一気に手に入る。
        """
        marker = jra_sire.BMS_MARKER if kind == jra_sire.KIND_BMS else jra_sire.SIRE_MARKER
        try:
            index_html = self.client.get_text(
                config.JRA_LEADING_INDEX_URL, encoding=config.JRA_RESULT_ENCODING
            )
        except requests.RequestException as exc:
            logger.error("リーディング情報のページの取得に失敗しました: %s", exc)
            index_html = None
        cname = jra_sire.find_index_cname(index_html or "", marker)
        if cname is None:
            self._warn("jra_leading", config.JRA_LEADING_INDEX_URL,
                       "リーディング情報のページに種牡馬リーディングへの入口がありません")
            cname = config.JRA_SIRE_INDEX_CNAME

        html = self._jra_page(cname, "jra_sire_index", kind, url=config.JRA_SIRE_URL)
        if not html:
            return {}
        try:
            parsed = jra_sire.parse_sire_leading(html)
        except LayoutError as exc:
            self._warn(f"sire_index:{kind}", config.JRA_SIRE_URL, str(exc))
            return {}
        # 「2歳」への切り替えも同じページに載っている。今の入口が求める種類と違えば辿り直す
        if parsed.kind != kind:
            if kind not in parsed.kind_cnames:
                # 辿り着けない種類（ブルードメアサイヤーなど）。**別の種類の数字を
                # その種類として保存してはいけない**ので、ここで諦める
                self._warn(f"sire_index:{kind}", config.JRA_SIRE_URL,
                           f"{jra_sire.KIND_LABELS.get(kind, kind)}のリーディングへ辿れません")
                return {}
            html = self._jra_page(parsed.kind_cnames[kind], "jra_sire_index", kind,
                                  url=config.JRA_SIRE_URL)
            if not html:
                return {}
            try:
                parsed = jra_sire.parse_sire_leading(html)
            except LayoutError as exc:
                self._warn(f"sire_index:{kind}", config.JRA_SIRE_URL, str(exc))
                return {}
            if parsed.kind != kind:
                self._warn(f"sire_index:{kind}", config.JRA_SIRE_URL,
                           f"{kind}を求めたのに{parsed.kind}のページが返りました")
                return {}

        links = {year: {1: c} for year, c in parsed.year_cnames.items()}
        if parsed.year is not None:
            links.setdefault(parsed.year, {1: cname})   # 年度セレクタに当年が無い場合の保険
        db.save_jra_sire_links(self.conn, kind, links)
        logger.info("種牡馬リーディング(%s)のリンク: %d年ぶん（%s〜%s）", kind, len(links),
                    min(links, default="-"), max(links, default="-"))
        return links

    # --- 未開催レースの出馬表 -------------------------------------------------

    def fetch_upcoming_days(self, days: list[date]) -> None:
        for day in days:
            self.fetch_upcoming_day(day)

    def fetch_upcoming_day(self, day: date) -> int:
        """その日のレース一覧と、各レースの出馬表を取得する。保存できたレース数を返す。"""
        key = day.isoformat()
        url = config.RACE_LIST_URL.format(yyyymmdd=day.strftime("%Y%m%d"))
        try:
            html = self.client.get_text(url, encoding="utf-8")
        except requests.RequestException as exc:
            logger.error("レース一覧の取得に失敗しました: %s: %s", key, exc)
            return 0
        races = parse_race_list_details(html) if html else []
        if not races:
            # 開催週になるまでレース一覧は公開されない（構造変化ではないので警告にしない）
            logger.info("%s: レース一覧がまだ公開されていません", key)
            return 0

        db.save_upcoming_race_list(self.conn, day, races)
        logger.info("%s: レース一覧 %d件", key, len(races))

        finished = db.saved_race_ids(self.conn, [r["race_id"] for r in races])
        n_saved = 0
        for race in races:
            race_id = race["race_id"]
            if race_id in finished:
                continue  # 結果が確定済みのレースは確定データ側にあるので取りに行かない
            if self.fetch_upcoming_race(race_id, day):
                n_saved += 1
        logger.info("%s: 出馬表 %d件を保存しました", key, n_saved)
        return n_saved

    def fetch_upcoming_race(self, race_id: str, day: date | None = None) -> bool:
        """1レースの出馬表を取得して保存する（毎回上書き）。保存できたらTrueを返す。"""
        url = config.SHUTUBA_URL.format(race_id=race_id)
        try:
            html = self.client.get_text(url, encoding="utf-8")
        except requests.RequestException as exc:
            logger.error("出馬表の取得に失敗しました: %s: %s", race_id, exc)
            return False
        if html is None:
            logger.info("%s: 出馬表ページが見つかりません(404)", race_id)
            return False

        try:
            page = parse_shutuba(html, race_id, day.isoformat() if day else None)
        except LayoutError as exc:
            self._warn(race_id, url, str(exc))
            return False
        if page is None:
            logger.info("%s: 出馬表がまだ公開されていません", race_id)
            return False

        for message in page.warnings:
            self._warn(race_id, url, message)
        try:
            db.save_upcoming_shutuba(self.conn, page)
        except (ValueError, sqlite3.Error) as exc:
            self._warn(race_id, url, f"保存に失敗しました: {exc}")
            return False
        self.n_races_saved += 1
        logger.debug("出馬表を保存: %s %s %d頭", race_id, page.race.get("race_name"), len(page.entries))
        return True
