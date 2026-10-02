"""閲覧用（クラウド版）のDBを作って、GitHubのリリースへ送り出す。

クラウドの競馬新聞は**見るだけ**なので、取り込みの記録や払戻のような
画面に出さない表は要らない。落として詰め直すと79MB→66MB、gzipで約20MBになる。

DBをgitに入れないのは、SQLiteが**差分の効かない1つのファイル**だから。
中身が少し変わっただけでも更新のたびに丸ごと履歴に積み上がり、
リポジトリがすぐ太る。リリースの添付ファイルなら、置き換えても履歴に残らない。
"""

from __future__ import annotations

import gzip
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path

from keiba_data import config

logger = logging.getLogger(__name__)

# 閲覧では使わない表。**子から先に**並べる（外部キーの向きに合わせる）。
# Target の取り込み層（target_*）は個人で使うためだけのもので、分析に要るもの（付記の無いレース名・
# 書き出し時点の馬主・名寄せキー・母のキー）は本体の列に写してあるので出さない。
# 5代血統表（horse_ancestors・pedigree_horses）は手元の血統クロス分析専用で、競馬新聞は読まない
# （netkeiba 由来で再配布しないものでもある。閲覧用DBの半分を占めていた）。
# 貸付馬房数（trainer_stalls）も手元の厩舎分析専用
VIEWER_DROP = ("payouts", "fetch_log", "parse_warnings",
               "target_horse_siblings", "target_runs", "target_races", "target_horses",
               "target_fills", "target_import_files", "runs",
               "horse_ancestors", "pedigree_horses", "trainer_stalls")

RELEASE_TAG = "db"
ASSET_NAME = "keiba-viewer.db.gz"


def build_viewer_db(source: Path, destination: Path, drop: tuple[str, ...] = VIEWER_DROP) -> Path:
    """閲覧に要らない表を落とした複製を作る（元のDBは読むだけ）。

    `VACUUM INTO` で**使っている最中でも安全に複製**できる（WALの途中も畳んでくれる）。
    落としたあとにもう一度 VACUUM して、空いた場所を詰める。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)      # VACUUM INTO は行き先が在ると失敗する

    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as reader:
        reader.execute("VACUUM INTO ?", (str(destination),))

    copy = sqlite3.connect(destination)
    try:
        for table in drop:
            copy.execute(f"DROP TABLE IF EXISTS {table}")
        copy.commit()
        copy.execute("VACUUM")
    finally:
        copy.close()
    return destination


def compress(path: Path, destination: Path | None = None) -> Path:
    """gzipで固める（クラウドは受け取ってから展開する）。"""
    destination = destination or path.with_suffix(path.suffix + ".gz")
    with path.open("rb") as source, gzip.open(destination, "wb", compresslevel=6) as out:
        shutil.copyfileobj(source, out)
    return destination


def upload(path: Path, repo: str, tag: str = RELEASE_TAG) -> bool:
    """GitHubのリリースへ添付する（同じ名前のものは差し替える）。

    `gh` が無い・認証していないときはFalseを返し、手で上げる道を案内できるようにする。
    """
    if shutil.which("gh") is None:
        logger.error("gh コマンドが見つかりません。手動でリリースに添付してください: %s", path)
        return False
    exists = subprocess.run(
        ["gh", "release", "view", tag, "-R", repo],
        capture_output=True, text=True, check=False,
    ).returncode == 0
    if not exists:
        logger.info("リリース %s を作ります", tag)
        created = subprocess.run(
            ["gh", "release", "create", tag, "-R", repo, "--title", "閲覧用DB",
             "--notes", "クラウド版の競馬新聞が読むDB（個人利用。再配布しません）"],
            capture_output=True, text=True, check=False,
        )
        if created.returncode != 0:
            logger.error("リリースを作れませんでした: %s", created.stderr.strip())
            return False
    done = subprocess.run(
        ["gh", "release", "upload", tag, str(path), "-R", repo, "--clobber"],
        capture_output=True, text=True, check=False,
    )
    if done.returncode != 0:
        logger.error("アップロードに失敗しました: %s", done.stderr.strip())
        return False
    return True


def publish(
    db_path: Path | None = None, out_dir: Path | None = None,
    repo: str | None = None, do_upload: bool = True,
) -> Path:
    """閲覧用DBを作って（必要なら）リリースへ上げる。作ったファイルを返す。"""
    db_path = Path(db_path or config.DB_PATH)
    out_dir = Path(out_dir or config.DATA_ROOT / "dist")
    repo = repo or config.PUBLISH_REPO

    viewer = build_viewer_db(db_path, out_dir / "keiba-viewer.db")
    packed = compress(viewer, out_dir / ASSET_NAME)
    logger.info(
        "閲覧用DBを作りました: %.1fMB → %.1fMB（圧縮 %.1fMB）: %s",
        db_path.stat().st_size / 1e6, viewer.stat().st_size / 1e6,
        packed.stat().st_size / 1e6, packed,
    )
    if do_upload and upload(packed, repo):
        logger.info("リリース %s に %s を置きました（%s）", RELEASE_TAG, ASSET_NAME, repo)
        logger.info("クラウド版は、次に開いたときに自動で取り込みます。")
    return packed
