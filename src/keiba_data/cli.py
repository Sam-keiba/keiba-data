"""CLIエントリポイント。

  uv run keiba-data update                        前回の続き〜今日のレース結果を取得（週末のレース後に実行）
  uv run keiba-data backfill --from 2023-01-01    過去レースをさかのぼって取得（何回かに分けて実行）
  uv run keiba-data reparse                       保存済みHTMLからDBを作り直す（ネットにアクセスしない）
  uv run keiba-data entries                       今後のレースの出馬表を取得（未開催レース）
  uv run keiba-data jra                           当日・前日のレース結果（ラップ）をJRA公式から取り込む
  uv run keiba-data jra --links --from 2023-01-01 過去レースのJRA公式ページへのリンクを集める（馬柱の映像リンク用）
  uv run keiba-data baba                          当日のクッション値・含水率をJRA公式の馬場情報ページから取り込む
  uv run keiba-data cushion                       クッション値・含水率をJRA公式PDFから取り込む
  uv run keiba-data pedigree                      馬の父・母・母の父をJRA公式から取り込む（種牡馬分析の土台）
  uv run keiba-data bloodline                     5代血統表を取り込む（血統クロス分析の土台）
  uv run keiba-data sire-fetch                    種牡馬リーディング（AEI）をJRA公式から取り込む
  uv run keiba-data publish                       閲覧用DBを作ってGitHubのリリースへ上げる（クラウド版に反映）
  uv run keiba-data status                        DBの件数・期間・直近の実行結果を表示

ダッシュボード（board/sire/club）はこのパッケージには無い。keiba-app 側から
keiba-data を読み取り専用で利用する。
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta

from keiba_data import config, cushion, db, publish as publish_module
from keiba_data.html_store import HtmlStore
from keiba_data.http import BlockedError, PoliteSession, RequestBudgetExceeded
from keiba_data.scrapers import jra_odds, jra_sire
from keiba_data.updater import Updater

logger = logging.getLogger("keiba")

EXIT_OK, EXIT_FAILED, EXIT_BLOCKED, EXIT_INTERRUPTED = 0, 1, 2, 130


def _setup_logging(verbose: bool) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_DIR / f"keiba_{datetime.now():%Y%m%d}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    file = logging.FileHandler(log_path, encoding="utf-8")
    file.setFormatter(fmt)
    file.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = [stream, file]
    for noisy in ("urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日付はYYYY-MM-DD形式で指定してください: {text!r}") from exc


def _run(args: argparse.Namespace) -> int:
    """update/backfill/reparse の共通処理: 実行履歴の記録と中断・エラー時の後始末。"""
    conn = db.connect(config.DB_PATH)
    run_id = db.start_run(conn, args.command, args.raw_args)

    def on_fetch(record) -> None:
        db.log_fetch(conn, run_id, record.url, record.status_code, record.ok, record.message)

    if args.command not in ("reparse",) and args.interval < config.MIN_RECOMMENDED_INTERVAL_SEC:
        logger.warning(
            "アクセス間隔%.1f秒は推奨値(%.1f秒)より短いです。過去に1秒間隔でnetkeibaから"
            "IPブロックを受けています。", args.interval, config.MIN_RECOMMENDED_INTERVAL_SEC,
        )
    session = PoliteSession(
        interval_sec=getattr(args, "interval", config.REQUEST_INTERVAL_SEC),
        max_requests=getattr(args, "max_requests", None),
        on_fetch=on_fetch,
    )
    updater = Updater(
        conn, session, HtmlStore(config.HTML_ROOT), run_id=run_id, force=getattr(args, "force", False)
    )

    status, message, code = "success", None, EXIT_OK
    try:
        if args.command == "update":
            updater.run_update()
        elif args.command == "backfill":
            updater.run_backfill(args.from_date, args.to_date)
        elif args.command == "reparse":
            updater.run_reparse()
        elif args.command == "entries":
            _fetch_entries(updater, args)
        elif args.command == "cushion":
            _fetch_cushion(conn, session, args)
        elif args.command == "baba":
            _fetch_baba(updater, conn)
        elif args.command == "jra":
            _fetch_jra(updater, args)
        elif args.command == "odds":
            _fetch_odds(updater, args)
        elif args.command == "sire-fetch":
            _fetch_sire(updater, args)
        elif args.command == "pedigree":
            _fetch_pedigree(conn, updater, args)
        elif args.command == "bloodline":
            _fetch_bloodline(conn, updater, args)
    except RequestBudgetExceeded as exc:
        status, message = "paused", str(exc)
        logger.info("%s 同じコマンドを再実行すると続きから再開します。", exc)
    except BlockedError as exc:
        status, message, code = "aborted", str(exc), EXIT_BLOCKED
        logger.error("%s", exc)
    except KeyboardInterrupt:
        status, message, code = "interrupted", "Ctrl+Cで中断", EXIT_INTERRUPTED
        logger.info("中断しました。保存済みの分は残っています。再実行すると続きから再開します。")
    except Exception as exc:  # noqa: BLE001 - 実行履歴に失敗を残してから終了する
        status, message, code = "failed", f"{type(exc).__name__}: {exc}", EXIT_FAILED
        logger.exception("処理に失敗しました")
    finally:
        db.finish_run(
            conn, run_id, status, session.n_requests, updater.n_races_saved, updater.n_warnings, message
        )

    logger.info(
        "%s 終了(%s): 保存%d件 / リクエスト%d回 / 警告%d件",
        args.command, status, updater.n_races_saved, session.n_requests, updater.n_warnings,
    )
    if getattr(args, "notify", False):
        _notify(args.command, status, updater.n_races_saved, conn)
    if updater.n_warnings:
        logger.warning(
            "警告が%d件あります。HTML構造が変わった可能性があるので、ログかparse_warningsテーブルを確認してください。",
            updater.n_warnings,
        )
    conn.close()
    return code


def _fetch_entries(updater: Updater, args: argparse.Namespace) -> None:
    """未開催レースの出馬表を取得する。--race-id / --date / 既定（今日から--days日先まで）。

    そのあと、netkeibaの出馬表には無い**血統・馬主・生産牧場と馬体重**をJRA公式から補う
    （`--no-jra` で省ける）。
    """
    if args.race_id:
        updater.fetch_upcoming_race(args.race_id)
        # 1レースだけなら、その開催をまるごと辿らずに済む（最大4リクエスト）
        if not getattr(args, "no_jra", False):
            saved = updater.fetch_jra_entry_race(args.race_id)
            logger.info("JRA公式から血統・馬主・馬体重を%d頭ぶん取り込みました。", saved)
        return
    if args.date:
        updater.fetch_upcoming_days([args.date])
        _fetch_jra_profiles(updater, args, [args.date])
        return
    today = updater.today
    end = today + timedelta(days=args.days)
    updater.sync_calendar(today, end)
    days = db.kaisai_days_between(updater.conn, today, end)
    if not days:
        logger.info("%s〜%s に開催日がありません", today, end)
        return
    logger.info("出馬表の取得対象: %s", ", ".join(d.isoformat() for d in days))
    updater.fetch_upcoming_days(days)
    _fetch_jra_profiles(updater, args, days)


def _fetch_jra_profiles(updater: Updater, args: argparse.Namespace, days=None) -> None:
    """JRA公式の出馬表から血統・馬主・生産牧場と馬体重を補う（netkeibaの出馬表には無い項目）。"""
    if getattr(args, "no_jra", False):
        return
    saved = updater.fetch_jra_entries(days)
    logger.info("JRA公式から血統・馬主・馬体重を%d頭ぶん取り込みました。", saved)


def _fetch_jra(updater, args) -> None:
    """JRA公式から当日・前日のレース結果（レース情報とラップ）を取り込む。

    netkeibaのdbページは当日・前日の結果がまだ公開されないため、その日の馬場差を
    測るにはこちらを使う。馬ごとの結果は後から `keiba update` が補完する。

    `--links` のときは、馬柱から飛ぶための「JRA公式の結果ページへのリンク」だけを
    過去にさかのぼって集める（開催1つにつき1リクエスト。時間がかかるので別コマンド扱い）。
    """
    if args.links:
        saved = updater.fetch_jra_race_links(args.from_date, args.to_date)
        logger.info("JRA公式のレースページへのリンクを%d件保存しました。", saved)
        return
    saved = updater.fetch_jra_results(args.date or None)
    logger.info("JRA公式から%dレース（レース情報とラップ）を保存しました。", saved)


def _fetch_odds(updater: Updater, args: argparse.Namespace) -> None:
    """1レースのオッズを券種ごとに取り込む。

    券種は日本語の名前（「馬連」）でも英字のキー（`umaren`）でも指定できる。
    単勝と複勝は同じページなので、両方を指定してもリクエストは1回で済む。
    """
    by_label = {bet.label: bet.key for bet in jra_odds.BET_TYPES}
    keys = None
    if args.bet_type:
        keys = [by_label.get(name, name) for name in args.bet_type]
        unknown = [k for k in keys if k not in jra_odds.BET_TYPE_BY_KEY]
        if unknown:
            raise SystemExit(
                f"知らない券種です: {'・'.join(unknown)}"
                f"（指定できるのは {'/'.join(by_label)}）"
            )
    saved = updater.fetch_odds(args.race_id, keys)
    if not saved:
        logger.info("オッズ %s: 取り込めた券種はありません（まだ発売されていない可能性）", args.race_id)
        return
    logger.info(
        "オッズ %s: %s",
        args.race_id,
        " / ".join(
            f"{jra_odds.BET_TYPE_BY_KEY[k].label} {n}点" for k, n in saved.items()
        ),
    )


def _fetch_baba(updater, conn) -> None:
    """JRAの馬場情報ページから、当日を含む直近のクッション値・含水率を取り込む。

    アーカイブPDF（`keiba cushion`）は開催後の公開なので、**当日や直近の開催**は
    こちらでしか埋まらない。リクエストは2本だけ。
    """
    saved = updater.fetch_track_conditions()
    covered, all_days = db.cushion_coverage(conn)
    logger.info(
        "馬場情報を%d件保存しました。DBの開催日 %d件のうち %d件（%.1f%%）にクッション値が付いています。",
        saved, all_days, covered, (covered / all_days * 100) if all_days else 0.0,
    )


def _fetch_cushion(conn, session: PoliteSession, args: argparse.Namespace) -> None:
    """JRA公式のアーカイブPDFからクッション値・含水率を取り込む。"""
    this_year = date.today().year
    years = range(args.from_year or args.year or this_year, (args.year or this_year) + 1)
    total = 0
    for year in years:
        if not args.no_download:
            cushion.download_missing_pdfs(session, year)
        pdfs = cushion.local_pdfs(year)
        if not pdfs:
            logger.info("%d年のPDFがありません（%s）", year, cushion.year_dir(year))
            continue
        for pdf in pdfs:
            try:
                records = cushion.parse_pdf(pdf, year)
            except Exception as exc:  # noqa: BLE001 - 1ファイルの失敗で全体を止めない
                logger.warning("PDFの解析に失敗しました: %s: %s", pdf.name, exc)
                continue
            if not records:
                logger.debug("%s: レコードが取れませんでした（クッション値の公表前の年など）", pdf.name)
                continue
            total += db.upsert_track_conditions(conn, records)
        logger.info("%d年: PDF %d件を取り込みました", year, len(pdfs))

    covered, all_days = db.cushion_coverage(conn)
    logger.info(
        "馬場情報を%d件保存しました。DBの開催日 %d件のうち %d件（%.1f%%）にクッション値が付いています。",
        total, all_days, covered, (covered / all_days * 100) if all_days else 0.0,
    )


def _publish(args: argparse.Namespace) -> int:
    """閲覧用DBを作って、クラウド版が読むリリースへ置く。"""
    path = publish_module.publish(
        repo=args.repo, do_upload=not args.no_upload,
    )
    print(f"閲覧用DB: {path}（{path.stat().st_size / 1e6:.1f}MB）")
    if args.no_upload:
        print("上げていません。上げるときは --no-upload を外して実行してください。")
    return EXIT_OK


def _fetch_sire(updater: Updater, args: argparse.Namespace) -> None:
    """種牡馬リーディング（E・I＝AEI）をJRA公式から取り込む。

    既定は直近 `config.SIRE_LEADING_YEARS` 年ぶん。1年＝5ページなので、5年でも25リクエスト。
    終わった年は数字が変わらないので、取り込み済みの年は飛ばす（`--force` で取り直す）。
    """
    this_year = date.today().year
    start = args.from_year or (this_year - config.SIRE_LEADING_YEARS + 1)
    end = args.to_year or this_year
    if start > end:
        logger.error("--from-year は --to-year 以下にしてください（%d > %d）", start, end)
        return
    years = list(range(start, end + 1))
    saved = updater.fetch_sire_leading(years, kind=args.kind)
    logger.info("種牡馬リーディング: %d〜%d年 / %d頭ぶんを保存しました", start, end, saved)


def _fetch_pedigree(conn, updater: Updater, args: argparse.Namespace) -> None:
    """馬の父・母・母の父をJRA公式の競走馬検索から取り込む。

    出走した馬は24,000頭を超えるので、1回では終わらない。
    **父が空の馬から始まる**ので、同じコマンドを何度も叩けば続きから進む。
    """
    before, total = db.count_pedigree(conn)
    updater.fetch_horse_pedigree(args.horses)
    after, _ = db.count_pedigree(conn)
    remaining = total - after
    logger.info(
        "血統: %d頭 → %d頭（+%d）。残り%d頭%s",
        before, after, after - before, remaining,
        "（`uv run keiba-data pedigree` をもう一度実行すると続きから進みます）" if remaining else "",
    )


def _fetch_bloodline(conn, updater: Updater, args: argparse.Namespace) -> None:
    """5代血統表をnetkeibaから取り込む（血統クロス分析の土台）。

    手元の horse_id が血統登録番号そのものなので検索が要らず、1頭1リクエスト。
    出走した馬は24,000頭を超えるので、何回かに分けて流す。
    **血統表がまだ入っていない馬から始まる**ので、同じコマンドで続きから進む。
    """
    before, total = db.count_bloodline(conn)
    updater.fetch_bloodlines(args.horses)
    after, _ = db.count_bloodline(conn)
    remaining = total - after
    logger.info(
        "5代血統表: %d頭 → %d頭（+%d）。残り%d頭%s",
        before, after, after - before, remaining,
        "（`uv run keiba-data bloodline` をもう一度実行すると続きから進みます）" if remaining else "",
    )


def _status(args: argparse.Namespace) -> int:
    conn = db.connect(config.DB_PATH)
    print(f"DB: {config.DB_PATH}")
    lo, hi, n_days = db.race_date_range(conn)
    print(f"期間: {lo or '-'} 〜 {hi or '-'}（{n_days}開催日）")
    for table, n in db.table_counts(conn).items():
        print(f"  {table:<10} {n:>9,}")
    up = conn.execute(
        "SELECT SUM(entry_status = 'entries') AS confirmed, "
        "SUM(entry_status = 'registered') AS registered, "
        "(SELECT COUNT(*) FROM upcoming_entries) AS entries, "
        "MAX(race_date) AS last_day FROM upcoming_races"
    ).fetchone()
    print(
        f"未開催レースの出馬表: 枠順確定{up['confirmed'] or 0}レース / 枠順未確定{up['registered'] or 0}レース "
        f"/ {up['entries']}頭（最終日 {up['last_day'] or '-'}）"
    )
    incomplete = conn.execute(
        "SELECT COUNT(*) AS n FROM kaisai_days WHERE completed = 0 AND kaisai_date <= date('now', 'localtime')"
    ).fetchone()["n"]
    print(f"未完了の開催日（今日まで・既知の範囲）: {incomplete}")
    _print_backfill_progress(conn)
    print("直近の実行:")
    for r in db.recent_runs(conn):
        print(
            f"  #{r['run_id']} {r['started_at']} {r['command']:<8} {r['status']:<11} "
            f"保存{r['n_races_saved']} リクエスト{r['n_requests']} 警告{r['n_warnings']}"
            + (f" | {r['message']}" if r["message"] else "")
        )
    conn.close()
    return EXIT_OK


def _notify(command: str, status: str, saved: int, conn) -> None:
    """終わったことをmacOSの通知で知らせる（`--notify`）。

    血統の取り込みは全部で20時間以上かかるので、ターミナルを見張らずに済むようにする。
    通知が出せない環境（macOS以外など）では黙って何もしない。
    """
    detail = f"{saved:,}頭を保存"
    progress = {"pedigree": db.count_pedigree, "bloodline": db.count_bloodline}.get(command)
    if progress is not None:
        done, total = progress(conn)
        remaining = max(total - done, 0)
        detail += f"／{done:,}/{total:,}頭 済み"
        detail += "（完了）" if not remaining else f"（残り{remaining:,}頭）"
    label = {"paused": "上限に達して一時停止", "success": "完了"}.get(status, status)
    message = f"{command} {label}: {detail}"
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {message!r} with title "keiba" sound name "Glass"'],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass          # 通知が出せなくても取り込みの結果には関係ない


def _bar(done: int, total: int, width: int = 24) -> str:
    """進み具合の帯。`keiba status` で取り込みの残りをひと目で見るため。"""
    if not total:
        return "-" * width
    filled = round(width * done / total)
    return "#" * filled + "." * (width - filled)


def _print_backfill_progress(conn) -> None:
    """血統（pedigree）と5代血統表（bloodline）の進み具合。

    どちらも全部で20時間以上かかるので、**いつでも残りが分かる**ようにしておく。
    残りリクエスト数から、3秒間隔での見込み時間も出す。
    """
    jobs = (
        ("血統（父・母・馬主・生産牧場）", db.count_pedigree(conn), "keiba-data pedigree"),
        ("5代血統表（血統クロス）", db.count_bloodline(conn), "keiba-data bloodline"),
    )
    print("取り込みの進み具合:")
    for label, (done, total), command in jobs:
        remaining = max(total - done, 0)
        percent = (done / total * 100) if total else 0.0
        hours = remaining * config.REQUEST_INTERVAL_SEC / 3600
        left = "完了" if not remaining else f"残り{remaining:,}頭（約{hours:.1f}時間）"
        print(f"  {label:<26} [{_bar(done, total)}] {done:>6,}/{total:,} ({percent:4.1f}%) {left}")
        if remaining:
            print(f"  {'':<26}  続き: uv run {command} --max-requests 5000")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keiba-data", description="netkeibaからJRAのレース結果を取得してSQLiteに蓄積するツール"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細ログを画面にも表示する")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_fetch_options(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--interval", type=float, default=config.REQUEST_INTERVAL_SEC,
            help=f"リクエスト間隔(秒)。デフォルト{config.REQUEST_INTERVAL_SEC}",
        )
        p.add_argument("--force", action="store_true", help="保存済みのレース・HTMLも取得し直す")
        p.add_argument(
            "--notify", action="store_true",
            help="終わったらmacOSの通知を出す（血統の取り込みなど、何時間もかかるとき用）",
        )

    p_update = sub.add_parser("update", help="前回の続きから今日までのレース結果を取得する")
    add_fetch_options(p_update)
    p_update.set_defaults(func=_run, max_requests=None)

    p_backfill = sub.add_parser("backfill", help="過去のレース結果をさかのぼって取得する（中断・再開可）")
    p_backfill.add_argument("--from", dest="from_date", type=_parse_date, default=date(2023, 1, 1),
                            help="開始日 YYYY-MM-DD（デフォルト2023-01-01）")
    p_backfill.add_argument("--to", dest="to_date", type=_parse_date, default=None,
                            help="終了日 YYYY-MM-DD（デフォルト今日）")
    p_backfill.add_argument(
        "--max-requests", type=int, default=config.DEFAULT_BACKFILL_MAX_REQUESTS,
        help=f"1回の実行でのリクエスト上限（デフォルト{config.DEFAULT_BACKFILL_MAX_REQUESTS}、約1.5時間）",
    )
    add_fetch_options(p_backfill)
    p_backfill.set_defaults(func=_run)

    p_entries = sub.add_parser("entries", help="未開催レースの出馬表を取得する（再取得で最新に上書き）")
    p_entries.add_argument("--date", type=_parse_date, default=None, help="対象の開催日 YYYY-MM-DD")
    p_entries.add_argument("--race-id", default=None, help="このレースの出馬表だけを取得し直す")
    p_entries.add_argument(
        "--no-jra", action="store_true",
        help="JRA公式からの血統・馬主・生産牧場の取り込みを省く（netkeibaの出馬表だけ取る）",
    )
    p_entries.add_argument(
        "--days", type=int, default=config.DEFAULT_ENTRIES_AHEAD_DAYS,
        help=f"今日から何日先までの開催日を対象にするか（デフォルト{config.DEFAULT_ENTRIES_AHEAD_DAYS}）",
    )
    add_fetch_options(p_entries)
    p_entries.set_defaults(func=_run, max_requests=None)

    p_jra = sub.add_parser("jra", help="当日・前日のレース結果（ラップ）をJRA公式から取り込む")
    p_jra.add_argument(
        "--date", type=_parse_date, action="append", default=None,
        help="対象の開催日（複数指定可。既定は今日と昨日）",
    )
    p_jra.add_argument(
        "--links", action="store_true",
        help="結果ではなく、馬柱から飛ぶJRA公式ページへのリンクを集める（過去分の穴埋め）",
    )
    p_jra.add_argument("--from", dest="from_date", type=_parse_date, default=date(2023, 1, 1),
                       help="--links のときの開始日（既定は2023-01-01）")
    p_jra.add_argument("--to", dest="to_date", type=_parse_date, default=None,
                       help="--links のときの終了日（既定は今日）")
    add_fetch_options(p_jra)
    p_jra.set_defaults(func=_run, max_requests=None)
    p_odds = sub.add_parser("odds", help="1レースのオッズ（単勝〜3連単）をJRA公式から取り込む")
    p_odds.add_argument("--race-id", required=True, help="対象のrace_id（例 202606040811）")
    p_odds.add_argument(
        "--bet-type", action="append", default=None,
        help=f"取り込む券種（複数指定可。既定は全券種）: {'/'.join(b.label for b in jra_odds.BET_TYPES)}",
    )
    add_fetch_options(p_odds)
    p_odds.set_defaults(func=_run, max_requests=None)

    p_baba = sub.add_parser("baba", help="当日のクッション値・含水率をJRA公式の馬場情報ページから取り込む")
    add_fetch_options(p_baba)
    p_baba.set_defaults(func=_run, max_requests=None)
    p_cushion = sub.add_parser("cushion", help="クッション値・含水率をJRA公式のアーカイブPDFから取り込む")
    p_cushion.add_argument("--year", type=int, default=None, help="対象の年（既定は今年）")
    p_cushion.add_argument("--from-year", type=int, default=None, help="この年から今年までをまとめて取り込む（初回は2023）")
    p_cushion.add_argument("--no-download", action="store_true", help="ダウンロードせず、手元のPDFだけ取り込む")
    add_fetch_options(p_cushion)
    p_cushion.set_defaults(func=_run, max_requests=None)

    p_pedigree = sub.add_parser(
        "pedigree", help="馬の父・母・母の父をJRA公式の競走馬検索から取り込む（種牡馬分析の土台）")
    p_pedigree.add_argument("--horses", type=int, default=None,
                            help="対象にする頭数（既定は全部。動作確認には --horses 200 が手ごろ）")
    p_pedigree.add_argument(
        "--max-requests", type=int, default=config.DEFAULT_PEDIGREE_MAX_REQUESTS,
        help=f"1回の実行で送るリクエスト数の上限（デフォルト{config.DEFAULT_PEDIGREE_MAX_REQUESTS}）",
    )
    add_fetch_options(p_pedigree)
    p_pedigree.set_defaults(func=_run)

    p_bloodline = sub.add_parser(
        "bloodline", help="5代血統表を取り込む（血統クロス分析の土台）")
    p_bloodline.add_argument("--horses", type=int, default=None,
                             help="対象にする頭数（既定は全部。動作確認には --horses 20 が手ごろ）")
    p_bloodline.add_argument(
        "--max-requests", type=int, default=config.DEFAULT_BLOODLINE_MAX_REQUESTS,
        help=f"1回の実行で送るリクエスト数の上限（デフォルト{config.DEFAULT_BLOODLINE_MAX_REQUESTS}）",
    )
    add_fetch_options(p_bloodline)
    p_bloodline.set_defaults(func=_run)

    p_sire_fetch = sub.add_parser(
        "sire-fetch", help="種牡馬リーディング（E・I＝AEI）をJRA公式から取り込む")
    p_sire_fetch.add_argument("--from-year", type=int, default=None,
                              help=f"開始年（既定は{config.SIRE_LEADING_YEARS}年前）")
    p_sire_fetch.add_argument("--to-year", type=int, default=None, help="終了年（既定は今年）")
    # ブルードメアサイヤーのリーディングは静的ページ（bm2025.html）側にあり、
    # この入口からは辿れないので、いまは選べるようにしていない
    p_sire_fetch.add_argument("--kind", default=jra_sire.KIND_ALL,
                              choices=[jra_sire.KIND_ALL, jra_sire.KIND_TWO],
                              help="all=全馬 / two=2歳")
    add_fetch_options(p_sire_fetch)
    p_sire_fetch.set_defaults(func=_run, max_requests=None)

    p_publish = sub.add_parser(
        "publish", help="閲覧用DBを作ってGitHubのリリースへ上げる（クラウド版に反映）")
    p_publish.add_argument("--no-upload", action="store_true", help="ファイルを作るだけで上げない")
    p_publish.add_argument("--repo", default=None, help=f"送り先（既定 {config.PUBLISH_REPO}）")
    p_publish.set_defaults(func=_publish)

    p_reparse = sub.add_parser("reparse", help="保存済みHTMLからDBを作り直す（ネットにアクセスしない）")
    p_reparse.set_defaults(func=_run)

    p_status = sub.add_parser("status", help="DBの状態を表示する")
    p_status.set_defaults(func=_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    args.raw_args = " ".join(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
