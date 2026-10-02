"""定数設定: 競馬場コード表・HTTPアクセスポリシー・保存先パス。"""

from __future__ import annotations

import os
from pathlib import Path

# --- 競馬場コード（race_idの5-6桁目）。JRA10場のみ（11以降は地方競馬） ---
VENUE_CODES: dict[str, str] = {
    "01": "札幌",
    "02": "函館",
    "03": "福島",
    "04": "新潟",
    "05": "東京",
    "06": "中山",
    "07": "中京",
    "08": "京都",
    "09": "阪神",
    "10": "小倉",
}

# JRA公式サイトのアーカイブPDFのファイル名に使われる競馬場のローマ字表記
VENUE_ENGLISH_NAMES: dict[str, str] = {
    "01": "sapporo", "02": "hakodate", "03": "fukushima", "04": "niigata", "05": "tokyo",
    "06": "nakayama", "07": "chukyo", "08": "kyoto", "09": "hanshin", "10": "kokura",
}
VENUE_ENGLISH_TO_CODE: dict[str, str] = {en: code for code, en in VENUE_ENGLISH_NAMES.items()}

# --- HTTPアクセスポリシー ---
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# 2026年9月に1秒間隔・約1時間の連続アクセスでnetkeiba(CloudFront)からIP単位のブロック
# (空ボディの400)を受けた実績があるため、3秒を下限の目安とする。
REQUEST_INTERVAL_SEC = 3.0
MIN_RECOMMENDED_INTERVAL_SEC = 3.0
REQUEST_TIMEOUT_SEC = 20
MAX_RETRIES = 3
# 400/403/429がこの回数連続したらブロックされたとみなして処理を中止する
BLOCK_STATUS_CODES = frozenset({400, 403, 429})
MAX_CONSECUTIVE_BLOCK_RESPONSES = 3
# backfillの1回あたりのリクエスト上限（約1.5時間）
DEFAULT_BACKFILL_MAX_REQUESTS = 1500
# entriesコマンドが既定で先読みする日数
DEFAULT_ENTRIES_AHEAD_DAYS = 7
# DBが空のときにupdateがさかのぼる日数
DEFAULT_UPDATE_LOOKBACK_DAYS = 14

# ダッシュボードのポート番号は keiba-app 側の設定に移した（このパッケージはUIを持たない）。

# --- URL ---
CALENDAR_URL = "https://race.netkeiba.com/top/calendar.html?year={year}&month={month}"
RACE_LIST_URL = "https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={yyyymmdd}"
RACE_RESULT_URL = "https://db.netkeiba.com/race/{race_id}/"
SHUTUBA_URL = "https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"

# netkeibaの5代血統表（血統クロス分析の土台）。**個人で見るためだけに使い、再配布はしない。**
# JRA公式は日本で走っていない祖先のページを持たず4〜5代目が埋まらない（実測で5代目は1〜2割）。
# JBISは規約で「加工・編集することなくあるがまま」の利用しか認めておらず、
# robots.txt にも `Crawl-delay: 600` があるため使えない。ここだけnetkeibaに頼る。
# 手元の horse_id が血統登録番号そのものなので、**検索せずに直接開ける**。
NETKEIBA_PED_URL = "https://db.netkeiba.com/horse/ped/{horse_id}/"
NETKEIBA_PED_ENCODING = "euc_jp"
# `keiba bloodline` の既定のリクエスト上限（約1.5時間ぶん。backfillと同じ考え方）
DEFAULT_BLOODLINE_MAX_REQUESTS = 1500

# JRAの馬場情報ページ（robots.txtは全許可）。当日のクッション値・含水率はここから取る。
# ページ本体はJavaScriptで値を埋めるので、その元になっている素のHTMLを直接読む。
BABA_CUSHION_URL = "https://www.jra.go.jp/keiba/baba/_data_cushion.html"
BABA_MOISTURE_URL = "https://www.jra.go.jp/keiba/baba/_data_moist.html"
BABA_ENCODING = "shift_jis"
# 当日の馬場状態・天候（馬場情報ページがPOSTで読んでいるJSON。全場ぶんが1回で返る）
BABA_CONDITION_URL = "https://www.jra.go.jp/JRADB/accessJ.html"
BABA_CONDITION_CNAME = "pw01iwtS3/CD"

# JRA公式のレース結果ページ。netkeibaのdbページは当日・前日の結果がまだ公開されないため、
# 当日・前日のラップはこちらから取る。`cname` をPOSTして辿る作りで、GETでは開けない。
JRA_TOP_URL = "https://www.jra.go.jp/"
JRA_RESULT_URL = "https://www.jra.go.jp/JRADB/accessS.html"
# `shift_jis` ではデコードに失敗するページがあるため cp932（Windows拡張を含む）で読む
JRA_RESULT_ENCODING = "cp932"
# トップページから入口のトークンを拾えなかったときの保険（2026-09時点の値）
JRA_RESULT_INDEX_CNAME = "pw01sli00/AF"
# レース1件の結果ページ。トークンはレース選択ページのリンクから集める（自力では組み立てない）
JRA_RACE_PAGE_URL = "https://www.jra.go.jp/JRADB/accessS.html?CNAME={cname}"
# 出馬表（血統・馬主・生産牧場はここにしか無い）。結果と同じ作りで、入口だけ別。
JRA_ENTRY_URL = "https://www.jra.go.jp/JRADB/accessD.html"
JRA_RACE_ENTRY_URL = "https://www.jra.go.jp/JRADB/accessD.html?CNAME={cname}"
JRA_ENTRY_INDEX_CNAME = "pw01dli00/F3"

# 競走馬検索と競走馬の詳細ページ（父・母・母の父・生産牧場の出どころ）。
# 手元のDBは2023年以降のJRA全レースを持っているが、血統だけは出馬表からしか取れず
# 過去分が埋まっていない。ここから1頭ずつ埋める（`keiba pedigree`）。
JRA_HORSE_SEARCH_URL = "https://www.jra.go.jp/JRADB/accessR.html"
JRA_HORSE_PAGE_URL = "https://www.jra.go.jp/JRADB/accessU.html?CNAME={cname}"
# **検索のトークンだけはチェックサムが無く、自力で組める**（上のJRA公式の注意書きの例外）。
# 中身は pw02uli + キャッシュ時間D1 + 所属0 + 性別0 + 現役抹消0 + 検索方法0（で始まる）。
JRA_HORSE_SEARCH_CNAME = "pw02uliD10000"
# 検索1回で返る上限。ちょうどこの数なら取りこぼしを疑い、頭を1文字伸ばして引き直す
JRA_HORSE_SEARCH_LIMIT = 200
# 頭文字は2文字から始める（JRAは全角カタカナ2文字以上しか受け付けない）
JRA_HORSE_PREFIX_MIN = 2
# 1文字ずつ伸ばす上限（これを超えたら諦めて、その馬は名前そのもので引く）
JRA_HORSE_PREFIX_MAX = 4
# `keiba pedigree` の既定のリクエスト上限（約1.5時間ぶん。backfillと同じ考え方）
DEFAULT_PEDIGREE_MAX_REQUESTS = 1500

# 種牡馬リーディング（E・I＝アーニングインデックスの出どころ）。結果・オッズと同じ作りで、
# 入口が「リーディング情報」のページ、ページの種類が accessU。年度は2009年まで遡れる。
JRA_LEADING_INDEX_URL = "https://www.jra.go.jp/datafile/leading/"
JRA_SIRE_URL = "https://www.jra.go.jp/JRADB/accessU.html"
# 入口ページからトークンを拾えなかったときの保険（2026-09時点の値）
JRA_SIRE_INDEX_CNAME = "pt03hld0099993101/A1"
# 種牡馬リーディングは1年あたり5ページ（上位100頭）
SIRE_LEADING_PAGES = 5
# 画面が既定で見る年数（AEI推移グラフの横軸）
SIRE_LEADING_YEARS = 5

# オッズ（単勝〜3連単）。結果・出馬表と同じ作りで、入口とページの種類（accessO）だけ別。
# 券種ごとに1ページに分かれていて、3連単3,360点でも1ページに全部入っている。
JRA_ODDS_URL = "https://www.jra.go.jp/JRADB/accessO.html"
JRA_ODDS_INDEX_CNAME = "pw15oli00/6D"

# --- 保存先パス（<project_root>/src/keiba/config.py に置かれている前提） ---
# 環境変数 KEIBA_HOME で保存先ルートを差し替えられる（テスト用）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = Path(os.environ.get("KEIBA_HOME", PROJECT_ROOT / "data"))
DB_PATH = DATA_ROOT / "keiba.db"
HTML_ROOT = DATA_ROOT / "html"
LOG_DIR = PROJECT_ROOT / "logs"
# 閲覧用DBの送り先（クラウド版が読むGitHubリリースのあるリポジトリ）。
# 環境変数 KEIBA_REPO で差し替えられる。**プライベートのまま使う**
PUBLISH_REPO = os.environ.get("KEIBA_REPO", "Sam-keiba/keiba-data")

# Target（TARGET frontier JV）から手で書き出したCSVの置き場所（`keiba-data target-import`）。
# 3リポジトリと同じ階層の datasets_from_target/ に race_data/ と horse_data/ がある前提。
# 環境変数 KEIBA_TARGET_DIR で差し替えられる。元ファイルは読むだけで、変更しない
TARGET_DATASETS_DIR = Path(os.environ.get("KEIBA_TARGET_DIR", PROJECT_ROOT.parent / "datasets_from_target"))

# JRA公式のクッション値・含水率アーカイブPDFの置き場所
# 構成: クッション値/{年}/{競馬場ローマ字}{開催回2桁}.pdf 例: クッション値/2026/nakayama03.pdf
CUSHION_ARCHIVE_ROOT = PROJECT_ROOT / "クッション値"
