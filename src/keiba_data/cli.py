"""CLIエントリポイント。

  uv run keiba-data refresh                       update → entries → baba → bloodline --new → publish をまとめて実行（いつもの更新）
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
  uv run keiba-data bloodline --new               最近の馬で血統表が無い馬だけ取り込む（新馬など。refresh でも流れる）
  uv run keiba-data bloodline-local               手元のデータだけで5代血統表を広げる（ネットにアクセスしない）
  uv run keiba-data sire-fetch                    種牡馬リーディング（AEI）をJRA公式から取り込む
  uv run keiba-data target-import                Targetから書き出したCSVを取り込む（足りない行と空欄だけを本体へ）
  uv run keiba-data publish                       閲覧用DBを作ってGitHubのリリースへ上げる（クラウド版に反映）
  uv run keiba-data status                        DBの件数・期間・直近の実行結果を表示

ダッシュボード（board/sire/club）はこのパッケージには無い。keiba-app 側から
keiba-data を読み取り専用で利用する。
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from keiba_data import (
    bloodline_local, config, cushion, db, publish as publish_module, sire_keys, target_import, trainer_stalls,
)
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
        elif args.command == "trainer-meikan":
            _fetch_trainer_meikan(conn, updater)
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
    # 新しく入った馬に、父・母の父の名寄せキーを付ける（名前で引くだけなので軽い）
    if args.command in ("update", "backfill", "reparse", "pedigree"):
        filled = sire_keys.fill_missing_sire_keys(conn)
        if filled:
            logger.info("名寄せキーを%d件付けました", filled)

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
    path, uploaded = publish_module.publish(
        repo=args.repo, do_upload=not args.no_upload,
    )
    print(f"閲覧用DB: {path}（{path.stat().st_size / 1e6:.1f}MB）")
    if args.no_upload:
        print("上げていません。上げるときは --no-upload を外して実行してください。")
        return EXIT_OK
    if not uploaded:
        print("上げられませんでした（上のログを見てください）。クラウド版は前のDBのままです。")
        return EXIT_FAILED
    return EXIT_OK


def _refresh(args: argparse.Namespace) -> int:
    """いつもの更新: update → entries → baba → bloodline --new → publish を順に流す。

    どれかが失敗・中断したら、そこで止めて上げない（半端なDBをクラウドに出さない）。
    ただし baba（クッション値）だけは失敗しても先へ進む。発表前に流すこともあり、
    その日の値はあとからスマホの更新マークで取り直せるので、結果・出馬表を上げない理由にならない。
    血統表がリクエスト上限に当たったときは一時停止（成功扱い）なので、そのまま上げる。
    残りは次の refresh で続きから取る。
    """
    fetch = ["--interval", str(args.interval)]
    publish = ["--no-upload"] if args.no_upload else []
    steps = (["update", *fetch], ["entries", *fetch], ["baba", *fetch],
             ["bloodline", "--new", *fetch], ["publish", *publish])
    conn = db.connect(config.DB_PATH)
    first_run_id = conn.execute("SELECT COALESCE(MAX(run_id), 0) FROM runs").fetchone()[0] + 1
    conn.close()
    codes: dict[str, int] = {}
    result = EXIT_OK
    for argv in steps:
        step = build_parser().parse_args(argv)
        step.raw_args = " ".join(argv)
        code = codes[argv[0]] = step.func(step)
        if code != EXIT_OK and argv[0] == "baba" and code != EXIT_INTERRUPTED:
            logger.warning("クッション値は取れませんでした（終了コード%d）。発表後にスマホの更新マークで取れます。", code)
            continue
        if code != EXIT_OK:
            logger.error("%s で止まりました（終了コード%d）。ここから先は流していません。", argv[0], code)
            result = code
            break
    _refresh_summary(first_run_id, codes, no_upload=args.no_upload, stopped=result != EXIT_OK)
    return result


REFRESH_LABELS = {"update": "結果", "entries": "出馬表", "baba": "クッション値",
                  "bloodline": "血統表", "publish": "閲覧用DB"}
RUN_STATUS_LABELS = {"failed": "失敗", "aborted": "アクセスを止められて中断", "interrupted": "中断"}


def _refresh_summary(first_run_id: int, codes: dict[str, int], *, no_upload: bool, stopped: bool) -> None:
    """refresh の最後に、手順ごとの結果を数行にまとめてログに出し、Macにも通知する。

    初回は1時間ほどかかるので、ログを遡らなくても何が入ったか分かるようにする。
    件数は実行履歴（runs）の、この refresh で増えた行から読む。
    """
    conn = db.connect(config.DB_PATH)
    try:
        runs = {r["command"]: r for r in conn.execute(
            "SELECT command, status, n_races_saved, n_warnings FROM runs WHERE run_id >= ? ORDER BY run_id",
            (first_run_id,))}
        lines = []
        for command, label in REFRESH_LABELS.items():
            if command not in codes:
                lines.append((label, "流していません"))
                continue
            run = runs.get(command) or {"status": "failed", "n_races_saved": 0, "n_warnings": 0}
            failed = codes[command] != EXIT_OK
            if command == "update":
                text = f"{run['n_races_saved']:,}件" + (f"・警告{run['n_warnings']}件" if run["n_warnings"] else "")
            elif command == "entries":
                text = f"{run['n_races_saved']:,}レース"
            elif command == "baba":
                text = _today_cushion_text(conn)
            elif command == "bloodline":
                left = len(db.recent_horses_without_bloodline(conn))
                text = f"+{run['n_races_saved']:,}頭" + (f"（残り{left:,}頭は次の refresh で）" if left else "")
            else:
                text = "作っただけ（上げていません）" if no_upload else "上げました"
            if failed and command == "publish":
                text = "上げられませんでした"
            elif failed and command == "baba":
                text = "取れませんでした（スマホの更新マークで取れます）"
            elif failed:
                text = f"{RUN_STATUS_LABELS.get(run['status'], run['status'])}（{text}）"
            lines.append((label, text))
    finally:
        conn.close()
    title = "refresh 途中で止まりました" if stopped else "refresh 完了"
    logger.info("%s", title)
    for label, text in lines:
        logger.info("  %-6s %s", label, text)
    _mac_notify(title, " / ".join(f"{label} {text}" for label, text in lines if text != "流していません"))


def _today_cushion_text(conn) -> str:
    """今日以降に発表されたクッション値を競馬場ごとに（例「東京 9.8、京都 10.1」）。"""
    rows = conn.execute(
        "SELECT v.venue_name, t.cushion_value FROM track_conditions t JOIN venues v USING (venue_code) "
        " WHERE t.date = (SELECT MAX(date) FROM track_conditions WHERE date >= ? AND cushion_value IS NOT NULL) "
        "   AND t.cushion_value IS NOT NULL ORDER BY t.venue_code",
        (date.today().isoformat(),),
    ).fetchall()
    if not rows:
        return "まだ発表なし（スマホの更新マークで取れます）"
    return "、".join(f"{r['venue_name']} {r['cushion_value']:.1f}" for r in rows)


def _target_import(args: argparse.Namespace) -> int:
    """Targetから書き出したCSVを取り込み、本体（races/entries/results/horses…）へ写す。

    ネットにはアクセスしない。何度流しても同じ結果になる（中身の変わらないファイルは読み飛ばす）。
    """
    if args.merge_only and args.no_merge:
        logger.error("--merge-only と --no-merge は同時に指定できません")
        return EXIT_FAILED
    conn = db.connect(config.DB_PATH)
    run_id = None if args.dry_run else db.start_run(conn, "target-import", args.raw_args)
    kinds = ("race", "horse") if args.kind == "all" else (args.kind,)
    status, message, n_warnings = "failed", None, 0
    try:
        results = []
        if not args.merge_only:
            results = target_import.load_files(conn, args.dir, kinds=kinds, only=args.file, run_id=run_id,
                                               force=args.force, dry_run=args.dry_run)
            n_warnings = sum(len(r.warnings) for r in results)
        merged = None
        if not args.no_merge:
            merged = target_import.merge(conn, dry_run=args.dry_run)
        loaded = sum(r.n_loaded for r in results if not r.unchanged)
        message = (f"取込{loaded:,}行 スキップ{sum(r.n_skipped for r in results):,}行"
                   + (f" / 本体に追加 {dict(merged.inserted)} / 空欄を埋めた{sum(merged.filled.values()):,}欄"
                      if merged else ""))
        status = "success"
        logger.info("target-import: %s%s", message, "（dry-run: DBは変更していません）" if args.dry_run else "")
        return EXIT_OK
    except KeyboardInterrupt:
        status = "interrupted"
        return EXIT_INTERRUPTED
    finally:
        if run_id is not None:
            db.finish_run(conn, run_id, status, 0, 0, n_warnings, message)
        conn.close()


def _bloodline_local(args: argparse.Namespace) -> int:
    """手元のデータだけで5代血統表を広げる（netkeibaの血統表＋Targetのhorse_data）。ネットにはアクセスしない。"""
    conn = db.connect(config.DB_PATH)
    try:
        if args.check:
            bloodline_local.holdout_check(conn)
            return EXIT_OK
        run_id = None if args.dry_run else db.start_run(conn, "bloodline-local", args.raw_args)
        result = bloodline_local.rebuild(conn, dry_run=args.dry_run)
        message = f"{result.horses:,}頭 平均{result.cells / max(result.horses, 1):.1f}マス"
        if run_id is not None:
            db.finish_run(conn, run_id, "success", 0, 0, 0, message)
        logger.info("bloodline-local: %s%s", message, "（dry-run: DBは変更していません）" if args.dry_run else "")
        return EXIT_OK
    finally:
        conn.close()


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


def _fetch_trainer_meikan(conn, updater: Updater) -> None:
    """現役の調教師の正式名・読み・生年月日・免許取得年をJRAの調教師名鑑から取り込む。"""
    saved = updater.fetch_trainer_meikan()
    logger.info("調教師名鑑: %d人を保存しました", len(saved))
    # 結果ページに名前が出てこない＝まだ1走もしていない（新規開業など）の人は、行ごと足している
    fresh = [f"{r['trainer_id']} {r['full_name']}" for r in conn.execute(
        "SELECT trainer_id, full_name FROM trainers WHERE meikan_updated_at IS NOT NULL "
        "AND trainer_name IS NULL ORDER BY trainer_id")]
    if fresh:
        logger.info("出走の記録がまだ無い調教師（%d人）: %s", len(fresh), "、".join(fresh))


def _trainer_stalls_import(args: argparse.Namespace) -> int:
    """調教師別の貸付馬房数を、人が確かめたCSVから取り込む（ネットにアクセスしない）。"""
    conn = db.connect(config.DB_PATH)
    code = EXIT_OK
    try:
        for path in args.csv:
            try:
                result = trainer_stalls.import_csv(conn, path)
            except (OSError, ValueError) as exc:
                logger.error("%s", exc)
                code = EXIT_FAILED
                continue
            for (day, stable), total in sorted(result.totals.items()):
                print(f"{path.name}: {day} {stable} 馬房数の合計 {total:,}")
            print(f"{path.name}: {result.rows}行中 {result.saved}人を保存")
            if result.unmatched:
                code = EXIT_FAILED
                print(f"結びつかなかった名前（{len(result.unmatched)}行。取り込んでいません）:")
                for row in result.unmatched:
                    print(f"  {row['stable']} {row['name']} {row['stalls']}  … {row['reason']}")
    finally:
        conn.close()
    return code


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
    if args.new:
        updater.fetch_bloodlines(args.horses, recent_only=True)
        left = len(db.recent_horses_without_bloodline(conn))
        logger.info("5代血統表（最近の馬）: 残り%d頭%s", left,
                    "（`uv run keiba-data bloodline --new` をもう一度実行すると続きから進みます）" if left else "")
        return
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
    _mac_notify("keiba", f"{command} {label}: {detail}")


def _mac_notify(title: str, message: str) -> None:
    """macOSの通知を出す。出せない環境（macOS以外など）では黙って何もしない。"""
    # AppleScript の文字列は "…" で、\" と \\ のエスケープが効く（日本語はそのまま渡す）
    message, title = (json.dumps(text, ensure_ascii=False) for text in (message, title))
    script = f'display notification {message} with title {title} sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass          # 通知が出せなくても取り込みの結果には関係ない


def _bar(done: int, total: int, width: int = 24) -> str:
    """進み具合の帯。`keiba status` で取り込みの残りをひと目で見るため。"""
    if not total:
        return "-" * width
    filled = round(width * done / total)
    return "#" * filled + "." * (width - filled)


def _print_backfill_progress(conn) -> None:
    """血統の揃い具合。**最近の馬（2023年以降に走った馬・出馬表の馬）**で足りないものだけを出す。

    全期間で数えると1990年代の馬（Targetにも無い）まで「残り」に入り、何十時間もかかるように
    見えてしまうので、全体は1行の数字だけにする。5代血統表の残りは3秒間隔での見込み時間も出す。
    """
    recent = db.count_recent_bloodline(conn)
    total = recent["total"]
    print("血統（2023年以降に走った馬・出馬表の馬）:")
    for label, done in (("父・母・母の父", recent["sire"]), ("5代血統表", recent["tree"])):
        missing = total - done
        percent = (done / total * 100) if total else 0.0
        if not missing:
            left = "揃っています"
        elif label == "5代血統表":
            minutes = missing * config.REQUEST_INTERVAL_SEC / 60
            left = f"無いのは{missing:,}頭（約{minutes:.0f}分）。次の refresh で埋まります"
        else:
            left = f"無いのは{missing:,}頭"
        print(f"  {label:<10} [{_bar(done, total)}] {done:>6,}/{total:,} ({percent:4.1f}%) {left}")
    # 全期間の残りは大半が1990年代の馬（Targetにも無い）で、取らなくても困らないので数字だけ
    any_done, all_total = db.count_any_bloodline(conn)
    netkeiba, _ = db.count_bloodline(conn)
    local, avg_cells = db.count_local_bloodline(conn)
    print(f"  全体（1995年〜）: {any_done:,}/{all_total:,}頭"
          f"（netkeiba {netkeiba:,}頭・手元で組んだもの {local:,}頭 平均{avg_cells:.1f}/62マス）")


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

    p_refresh = sub.add_parser("refresh", help="update → entries → baba → bloodline --new → publish をまとめて実行する（いつもの更新）")
    p_refresh.add_argument(
        "--interval", type=float, default=config.REQUEST_INTERVAL_SEC,
        help=f"リクエスト間隔(秒)。デフォルト{config.REQUEST_INTERVAL_SEC}",
    )
    p_refresh.add_argument("--no-upload", action="store_true", help="閲覧用DBを作るだけで上げない")
    p_refresh.set_defaults(func=_refresh)

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
        "--new", action="store_true",
        help="2023年以降に走った馬と出馬表の馬のうち、血統表が1マスも無い馬だけ（手元で組んだ馬は済み扱い）",
    )
    p_bloodline.add_argument(
        "--max-requests", type=int, default=config.DEFAULT_BLOODLINE_MAX_REQUESTS,
        help=f"1回の実行で送るリクエスト数の上限（デフォルト{config.DEFAULT_BLOODLINE_MAX_REQUESTS}）",
    )
    add_fetch_options(p_bloodline)
    p_bloodline.set_defaults(func=_run)

    p_bloodline_local = sub.add_parser(
        "bloodline-local", help="手元のデータだけで5代血統表を広げる（netkeibaの血統表＋Targetのhorse_data。ネットにアクセスしない）")
    p_bloodline_local.add_argument("--dry-run", action="store_true", help="DBを変えずに、頭数と平均マス数だけ出す")
    p_bloodline_local.add_argument("--check", action="store_true",
                                   help="netkeibaの血統表がある馬の1割を外して組み直し、一致率を出す（DBは変えない）")
    p_bloodline_local.set_defaults(func=_bloodline_local)

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

    p_trainer = sub.add_parser(
        "trainer-meikan", help="現役の調教師の正式名・読み・生年月日・免許取得年をJRAの調教師名鑑から取り込む")
    add_fetch_options(p_trainer)
    p_trainer.set_defaults(func=_run, max_requests=None)

    p_stalls = sub.add_parser(
        "trainer-stalls-import",
        help="調教師別の貸付馬房数を、確かめたCSV（effective_date,stable,name,stalls）から取り込む")
    p_stalls.add_argument("csv", type=Path, nargs="+", help="CSVファイル（例 data/trainer_stalls/2026-03-04.csv）")
    p_stalls.set_defaults(func=_trainer_stalls_import)

    p_publish = sub.add_parser(
        "publish", help="閲覧用DBを作ってGitHubのリリースへ上げる（クラウド版に反映）")
    p_publish.add_argument("--no-upload", action="store_true", help="ファイルを作るだけで上げない")
    p_publish.add_argument("--repo", default=None, help=f"送り先（既定 {config.PUBLISH_REPO}）")
    p_publish.set_defaults(func=_publish)

    p_target = sub.add_parser(
        "target-import", help="Targetから書き出したCSVを取り込み、本体の足りない行・空欄を埋める（ネットにアクセスしない）")
    p_target.add_argument("--kind", default="all", choices=["all", "race", "horse"],
                          help="race=race_data / horse=horse_data（既定は両方）")
    p_target.add_argument("--file", default=None, help="このファイルだけ取り込む（例 race_data_2024.csv）")
    p_target.add_argument("--dir", type=Path, default=config.TARGET_DATASETS_DIR,
                          help=f"CSVの置き場所（既定 {config.TARGET_DATASETS_DIR}）")
    p_target.add_argument("--force", action="store_true", help="前回と同じ中身のファイルも読み直す")
    p_target.add_argument("--dry-run", action="store_true", help="DBを変えずに、件数とスキップ理由だけ出す")
    p_target.add_argument("--no-merge", action="store_true", help="target_* に入れるだけで、本体には写さない")
    p_target.add_argument("--merge-only", action="store_true", help="CSVは読まず、target_* から本体へ写すだけ")
    p_target.set_defaults(func=_target_import)

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
