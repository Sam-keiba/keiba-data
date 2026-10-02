-- keiba データ基盤 スキーマ
-- 日付は 'YYYY-MM-DD'、日時は 'YYYY-MM-DD HH:MM:SS'（ローカル時刻）の TEXT で保持する。

PRAGMA foreign_keys = ON;

-- 競馬場マスタ（JRA10場。init時に投入）
CREATE TABLE IF NOT EXISTS venues (
    venue_code TEXT PRIMARY KEY,          -- '01'〜'10'
    venue_name TEXT NOT NULL              -- 札幌/函館/福島/新潟/東京/中山/中京/京都/阪神/小倉
);

-- レース（1レース1行）
CREATE TABLE IF NOT EXISTS races (
    race_id         TEXT PRIMARY KEY,     -- netkeibaの12桁ID: 年(4)+場(2)+回(2)+日目(2)+R(2)
    race_date       TEXT NOT NULL,
    venue_code      TEXT NOT NULL REFERENCES venues(venue_code),
    kaiji           INTEGER NOT NULL,     -- 第◯回
    nichime         INTEGER NOT NULL,     -- ◯日目
    race_no         INTEGER NOT NULL,
    race_name       TEXT,
    grade           TEXT,                 -- GI/GII/GIII/JGI/JGII/JGIII/L、それ以外はNULL
    surface         TEXT,                 -- turf/dirt/jump
    direction       TEXT,                 -- right/left/straight
    distance_m      INTEGER,
    course_detail   TEXT,                 -- 外/内/内2周/芝 ダート 等
    weather         TEXT,                 -- 晴/曇/小雨/雨/小雪/雪
    going_turf      TEXT,                 -- 芝の馬場状態 良/稍重/重/不良
    going_dirt      TEXT,                 -- ダートの馬場状態
    post_time       TEXT,                 -- 'HH:MM'
    age_condition   TEXT,                 -- 2歳/3歳/3歳以上/4歳以上
    class_condition TEXT,                 -- 新馬/未勝利/1勝クラス/2勝クラス/3勝クラス/オープン 等
    race_conditions TEXT,                 -- 国際,指,馬齢 等（カンマ区切り）
    condition_raw   TEXT,                 -- パース元の表記
    n_runners       INTEGER,              -- 出走頭数（取消・除外を除く）
    jra_cname       TEXT,                 -- JRA公式のレース結果ページのトークン（馬柱の映像リンクに使う）
    winner_corner   TEXT,                 -- 勝ち馬のコーナー通過順位（JRA公式由来。開催の傾向表示に使う）
    fetched_at      TEXT NOT NULL,        -- 最初に保存した日時
    updated_at      TEXT NOT NULL,        -- 最後に保存した日時
    source          TEXT NOT NULL DEFAULT 'scrape', -- scrape=netkeiba/JRA公式 / target=Target（target-import）で作った行
    -- 回次・格・クラスの付記を落としたレース名（race_names.py）。'スプリンターズS' '3歳以上500万下'
    -- Target由来は略称を2023年以降の同じレースの名前に寄せたもの。寄せられなかった略称は切れたまま
    race_name_plain TEXT
);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(race_date);

CREATE TABLE IF NOT EXISTS horses (
    horse_id       TEXT PRIMARY KEY,
    horse_name     TEXT,
    sex            TEXT,                  -- 牡/牝/セ（最後に出走したときの値）
    sire           TEXT,                  -- 父（JRA公式の出馬表から）
    dam            TEXT,                  -- 母
    broodmare_sire TEXT,                  -- 母の父
    owner_name     TEXT,                  -- 馬主（最後に取り込んだ時点）
    breeder        TEXT,                  -- 生産牧場
    -- ここから下はJRA公式の競走馬の詳細ページから（`keiba pedigree`）
    birth_date         TEXT,              -- 生年月日 'YYYY-MM-DD'
    sire_no            TEXT,              -- 父の血統登録番号（同名の種牡馬を取り違えないため）
    broodmare_sire_no  TEXT,              -- 母の父の血統登録番号
    trainer_name       TEXT,              -- 調教師名（詳細ページの表記）
    updated_at     TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'scrape', -- scrape / target（races.source と同じ）
    -- 父・母の父の名寄せキー（stallions.sire_key）。カナ・英字の表記ゆれがあっても同じ馬なら同じ値
    sire_key           TEXT,
    broodmare_sire_key TEXT,
    -- 母の繁殖登録番号（Target の horse_data 由来）。同名の別馬（95組）があるので名前では束ねない。
    -- horse_data に無い馬（1995年前後生まれ、書き出し後に入った新馬）は空
    dam_key            TEXT
);

CREATE TABLE IF NOT EXISTS jockeys (
    jockey_id   TEXT PRIMARY KEY,
    jockey_name TEXT,                     -- 結果ページの表記（例: ルメール）
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trainers (
    trainer_id   TEXT PRIMARY KEY,
    trainer_name TEXT,                    -- netkeibaの表記。**4文字で切れている**（「中内田充」）
    stable       TEXT,                    -- 美浦/栗東/地方/海外
    updated_at   TEXT NOT NULL,
    -- JRAの調教師名鑑（`keiba-data trainer-meikan`）から。名鑑に載る現役の調教師だけ埋まる
    full_name         TEXT,               -- 正式名（「中内田 充正」。姓と名の間に空白）
    kana              TEXT,               -- 読み（「ナカウチダ ミツマサ」）
    birth_date        TEXT,               -- 'YYYY-MM-DD'
    license_year      INTEGER,            -- 調教師免許の取得年（開業年の目安）
    meikan_updated_at TEXT                -- 名鑑で最後に見た日時（古いままなら引退した可能性）
);

-- 調教師別の貸付馬房数（JRAが毎年2〜3月に発表するPDF。`keiba-data trainer-stalls-import`）。
-- PDFはスキャン画像なので、人が確かめたCSVから入れる。effective_date はその馬房数が効く日（発表の「3月4日から」）
CREATE TABLE IF NOT EXISTS trainer_stalls (
    trainer_id     TEXT NOT NULL REFERENCES trainers(trainer_id),
    effective_date TEXT NOT NULL,         -- 'YYYY-MM-DD'
    stalls         INTEGER NOT NULL,      -- 貸付馬房数
    stable         TEXT NOT NULL,         -- 美浦/栗東（発表の区分）
    name_in_source TEXT NOT NULL,         -- 発表に載っていた名前（照合の確かめ用）
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (trainer_id, effective_date)
);

CREATE TABLE IF NOT EXISTS owners (
    owner_id   TEXT PRIMARY KEY,
    owner_name TEXT,
    updated_at TEXT NOT NULL
);

-- 出走表（出走馬1頭1行。取消・除外の馬も含む）
CREATE TABLE IF NOT EXISTS entries (
    race_id      TEXT NOT NULL REFERENCES races(race_id) ON DELETE CASCADE,
    umaban       INTEGER NOT NULL,        -- 馬番
    waku         INTEGER,                 -- 枠番
    horse_id     TEXT REFERENCES horses(horse_id),
    sex          TEXT,
    age          INTEGER,
    kinryo       REAL,                    -- 斤量(kg)
    jockey_id    TEXT REFERENCES jockeys(jockey_id),
    trainer_id   TEXT REFERENCES trainers(trainer_id),
    owner_id     TEXT REFERENCES owners(owner_id),   -- レース当時の馬主（netkeiba由来。2022年以前は空）
    horse_weight INTEGER,                 -- 馬体重(kg)
    weight_diff  INTEGER,                 -- 前走比増減(kg)
    -- Targetを書き出した時点の馬主（target_horses.owner_code）。**レース当時ではない**
    -- （2023年の走で約3%、転売された馬で owner_id と食い違う）。owner_id が空の走の代用に使う
    owner_id_at_export TEXT REFERENCES owners(owner_id),
    -- 減量騎手の印（▲△☆◇★。減量の無い騎乗はNULL）。Targetの書き出し（target_runs）から写し、
    -- それより後の走はJRAの出馬表（upcoming_entries）から写す（db.fill_kinryo_marks）
    kinryo_mark  TEXT,
    PRIMARY KEY (race_id, umaban)
);
CREATE INDEX IF NOT EXISTS idx_entries_horse ON entries(horse_id);
CREATE INDEX IF NOT EXISTS idx_entries_jockey ON entries(jockey_id);
CREATE INDEX IF NOT EXISTS idx_entries_trainer ON entries(trainer_id);
-- クラブ（馬主）ごとの集計用
CREATE INDEX IF NOT EXISTS idx_entries_owner ON entries(owner_id);
CREATE INDEX IF NOT EXISTS idx_entries_owner_export ON entries(owner_id_at_export);

-- 結果・オッズ（entriesと1対1）
CREATE TABLE IF NOT EXISTS results (
    race_id         TEXT NOT NULL,
    umaban          INTEGER NOT NULL,
    horse_id        TEXT,
    finish_position INTEGER,              -- 着順（取消/除外/中止/失格はNULL）
    finish_status   TEXT,                 -- 取消/除外/中止/失格/降着、通常はNULL
    time_sec        REAL,                 -- 走破タイム(秒)
    time_raw        TEXT,                 -- '2:14.0'
    margin          TEXT,                 -- 着差 'ハナ'/'1/2'/'同着' 等
    corner_passing  TEXT,                 -- コーナー通過順 '2-3-4-3'
    last_3f         REAL,                 -- 上り3F(秒)
    win_odds        REAL,                 -- 単勝オッズ（確定）
    popularity      INTEGER,              -- 単勝人気
    prize_man_yen   REAL,                 -- 本賞金(万円)
    PRIMARY KEY (race_id, umaban),
    FOREIGN KEY (race_id, umaban) REFERENCES entries(race_id, umaban) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_results_horse ON results(horse_id);

-- 払戻
CREATE TABLE IF NOT EXISTS payouts (
    race_id     TEXT NOT NULL REFERENCES races(race_id) ON DELETE CASCADE,
    bet_type    TEXT NOT NULL,            -- 単勝/複勝/枠連/馬連/ワイド/馬単/三連複/三連単
    combination TEXT NOT NULL,            -- '7' / '6-7' / '7-6-2'
    payout_yen  INTEGER,                  -- 100円あたりの払戻金
    popularity  INTEGER,
    PRIMARY KEY (race_id, bet_type, combination)
);

-- 公式ラップ（障害レースは無し）
CREATE TABLE IF NOT EXISTS race_laps (
    race_id    TEXT NOT NULL REFERENCES races(race_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,          -- 1始まりの区間番号
    distance_m INTEGER NOT NULL,          -- スタートからの累計距離（例: 200, 400, ...）
    lap_sec    REAL NOT NULL,
    PRIMARY KEY (race_id, seq)
);

-- 開催日の取得状況（バックフィルの再開と、updateの取りこぼし防止に使う）
CREATE TABLE IF NOT EXISTS kaisai_days (
    kaisai_date TEXT PRIMARY KEY,
    n_races     INTEGER,                  -- レース一覧に載っていたJRAのレース数
    n_saved     INTEGER,                  -- うちDBに保存済みの数
    completed   INTEGER NOT NULL DEFAULT 0, -- 1=この日の全レースを保存済み
    checked_at  TEXT
);

-- 取得済みカレンダー月（過去の月は一度取得すれば再取得しない）
CREATE TABLE IF NOT EXISTS calendar_months (
    year_month TEXT PRIMARY KEY,          -- 'YYYY-MM'
    is_final   INTEGER NOT NULL DEFAULT 0, -- 1=月が終わった後に取得した（以後変化しない）
    fetched_at TEXT NOT NULL
);

-- 実行履歴
CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    command       TEXT NOT NULL,          -- update/backfill/reparse
    args          TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,          -- running/success/paused/aborted/failed/interrupted
    n_requests    INTEGER NOT NULL DEFAULT 0,
    n_races_saved INTEGER NOT NULL DEFAULT 0,
    n_warnings    INTEGER NOT NULL DEFAULT 0,
    message       TEXT
);

-- HTTPアクセスの記録
CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES runs(run_id),
    fetched_at  TEXT NOT NULL,
    url         TEXT NOT NULL,
    status_code INTEGER,
    ok          INTEGER NOT NULL,
    message     TEXT
);

-- パース時の警告（HTML構造変化の検知用）
CREATE TABLE IF NOT EXISTS parse_warnings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(run_id),
    created_at TEXT NOT NULL,
    url        TEXT,
    key        TEXT,                      -- race_id や 日付
    message    TEXT NOT NULL
);

-- ============================================================
-- 未開催レース（出馬表）。確定した結果のテーブルとは分けて持つ。
-- 直前まで変動するため、再取得のたびに丸ごと上書きする。
-- ============================================================

CREATE TABLE IF NOT EXISTS upcoming_races (
    race_id         TEXT PRIMARY KEY,
    race_date       TEXT NOT NULL,
    venue_code      TEXT NOT NULL REFERENCES venues(venue_code),
    kaiji           INTEGER,
    nichime         INTEGER,
    race_no         INTEGER,
    race_name       TEXT,
    grade           TEXT,
    surface         TEXT,
    direction       TEXT,
    distance_m      INTEGER,
    course_detail   TEXT,
    post_time       TEXT,
    age_condition   TEXT,
    class_condition TEXT,
    race_conditions TEXT,
    n_entries       INTEGER,
    entry_status    TEXT NOT NULL,        -- list_only=一覧のみ / registered=出走馬は判明・枠順未確定 / entries=枠順確定済み
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_upcoming_races_date ON upcoming_races(race_date);

CREATE TABLE IF NOT EXISTS upcoming_entries (
    race_id      TEXT NOT NULL REFERENCES upcoming_races(race_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,        -- 出馬表での並び順（枠順確定前は馬番が無いため主キーに使う）
    umaban       INTEGER,                 -- 枠順確定前はNULL
    waku         INTEGER,
    horse_id     TEXT,
    horse_name   TEXT,
    sex          TEXT,
    age          INTEGER,
    kinryo       REAL,
    jockey_id    TEXT,
    jockey_name  TEXT,                    -- 出馬表の表記（結果ページと違い姓名の間に空白が入る）
    trainer_id   TEXT,
    trainer_name TEXT,
    stable       TEXT,                    -- 美浦/栗東
    horse_weight INTEGER,                 -- 当日発表。前日までは空
    weight_diff  INTEGER,
    status       TEXT,                    -- 取消/除外。通常はNULL
    -- 減量騎手の印（JRAの出馬表から）。**この行の jockey_id の騎手の印**で、騎手が替われば消す
    kinryo_mark  TEXT,
    PRIMARY KEY (race_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_upcoming_entries_horse ON upcoming_entries(horse_id);
CREATE INDEX IF NOT EXISTS idx_upcoming_entries_umaban ON upcoming_entries(race_id, umaban);

-- JRA公式アーカイブPDF由来の馬場情報（競馬場×日付で1行）。
-- races.race_date + venue_code で結合して、過去走のクッション値・含水率を引く。
CREATE TABLE IF NOT EXISTS track_conditions (
    venue_code            TEXT NOT NULL REFERENCES venues(venue_code),
    date                  TEXT NOT NULL,
    nichime               INTEGER,           -- 開催◯日目（開催前日の測定はNULL）
    day_label             TEXT,
    is_pre_meeting_day    INTEGER NOT NULL DEFAULT 0,  -- 1=開催前日（金曜）の測定
    course_setting        TEXT,              -- 芝の使用コース A/B/C（2024年以前のPDFには無い）
    cushion_value         REAL,              -- 芝クッション値
    turf_moisture_goal    REAL,              -- 含水率 芝・ゴール前(%)
    turf_moisture_4corner REAL,              -- 含水率 芝・4コーナー(%)
    dirt_moisture_goal    REAL,              -- 含水率 ダート・ゴール前(%)
    dirt_moisture_4corner REAL,              -- 含水率 ダート・4コーナー(%)
    measured_at           TEXT,              -- 測定日時（JRAの馬場情報ページ由来。PDF由来はNULL）
    going_turf            TEXT,              -- 当日発表の馬場状態（芝）。馬場情報ページ由来
    going_dirt            TEXT,              -- 当日発表の馬場状態（ダート）
    weather               TEXT,              -- 当日発表の天候
    source_pdf            TEXT,              -- 取得元（アーカイブPDF、または馬場情報ページのURL）
    updated_at            TEXT NOT NULL,
    PRIMARY KEY (venue_code, date)
);


-- 予想ボード（Tier × 隊列ポジション）に置いた馬の位置とコメント。
-- 自動仮配置はここに保存せず、**手を入れた結果だけ**を残す（is_manual）。
-- 保存が無い馬は、開くたびに最新のデータで自動配置し直す。
CREATE TABLE IF NOT EXISTS board_horses (
    race_id     TEXT NOT NULL,
    horse_id    TEXT NOT NULL,
    tier        TEXT,              -- 'A'/'B'/'C'/'D'
    position    REAL,              -- 0.0=Save runner 〜 1.0=FrontRunner
    lane_offset REAL,              -- レーン内の上下（マーカーの重なりを避ける）
    comment     TEXT,
    is_manual   INTEGER NOT NULL DEFAULT 0,  -- 1=手で動かした（自動配置と見分ける）
    is_excluded INTEGER NOT NULL DEFAULT 0,  -- 1=消した馬（盤では灰色＋取消線）
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (race_id, horse_id)
);


-- JRA公式のオッズ（単勝〜3連単）。**最新だけ**を残す（取り込むたび入れ替え）。
-- combo は組み合わせを `-` でつないだ文字列。着順を見ない券種（馬連・ワイド・枠連・3連複）は
-- 小さい順にそろえてあり、着順を見る券種（馬単・3連単）は着順どおりに並んでいる。
-- 枠連だけは馬番ではなく枠番。
CREATE TABLE IF NOT EXISTS odds (
    race_id   TEXT NOT NULL,
    bet_type  TEXT NOT NULL,        -- tansho/fukusho/wakuren/umaren/wide/umatan/sanrenpuku/sanrentan
    combo     TEXT NOT NULL,        -- '7' / '3-7' / '3-7-11'
    odds_low  REAL,                 -- 「票数なし」はNULL（発売はされている）
    odds_high REAL,                 -- 幅で出る券種（複勝・ワイド）の上限。他はNULL
    PRIMARY KEY (race_id, bet_type, combo)
);

-- オッズをいつ時点のものとして取り込んだか（レース×券種で1行）。
-- odds_label はJRAの表記そのまま（「12時24分現在オッズ」「最終オッズ」）。
CREATE TABLE IF NOT EXISTS odds_updates (
    race_id    TEXT NOT NULL,
    bet_type   TEXT NOT NULL,
    odds_label TEXT,
    n_combos   INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (race_id, bet_type)
);

-- オッズのページを開くためのトークン（レース×券種で1行）。
-- 末尾2文字はチェックサムで組み立てられないので、リンクから拾ったものをためておく。
-- これがあれば券種の切り替えが1リクエストで済む。
CREATE TABLE IF NOT EXISTS jra_odds_links (
    race_id  TEXT NOT NULL,
    bet_type TEXT NOT NULL,
    cname    TEXT NOT NULL,
    PRIMARY KEY (race_id, bet_type)
);

-- JRA公式の出馬表ページを開くためのトークン（レースで1行）。オッズと同じ理由でためておく。
-- これがあれば「このレースを最新に更新」で馬体重を取り直すのが1リクエストで済む。
CREATE TABLE IF NOT EXISTS jra_entry_links (
    race_id TEXT PRIMARY KEY,
    cname   TEXT NOT NULL
);

-- 自分で組んだ「買い目」（券種ごとの組み合わせと、1点あたりの金額）。
-- 画面の右カラムで積み上げたものをそのまま残す。**購入・投票はしない**ので、
-- ここにあるのは「何を買うつもりか」の控えだけ。
-- group_id は「追加」1回ぶんのまとまり（例: 馬連のフォーメーション3点で1つ）。
CREATE TABLE IF NOT EXISTS bet_slips (
    race_id    TEXT NOT NULL,
    group_id   INTEGER NOT NULL,   -- 「追加」1回ぶんのまとまり
    bet_type   TEXT NOT NULL,      -- tansho/fukusho/wakuren/umaren/wide/umatan/sanrenpuku/sanrentan
    combo      TEXT NOT NULL,      -- '7' / '3-7' / '3-7-11'（枠連は枠番）
    amount_yen INTEGER NOT NULL DEFAULT 100,   -- **1点あたり**の金額
    kind       TEXT,               -- 買い方（通常 / フォーメーション / ながし / ボックス）
    seq        INTEGER NOT NULL,   -- まとまりの中の並び（追加したときの順）
    updated_at TEXT NOT NULL,
    PRIMARY KEY (race_id, group_id, combo)
);

-- JRA公式の種牡馬リーディング（一口出資ツールの「種牡馬分析」が読む）。
-- 1行＝その年のその種牡馬の産駒成績。E・I（アーニングインデックス＝AEI）は
-- 「1出走賞金 ÷ 全馬の1出走賞金」なので **1.00が全種牡馬の平均**。
-- 上位100頭しか載らないが、1出走賞金 ÷ E・I を逆算すると全馬の1出走賞金が出る。
-- 出どころ: https://www.jra.go.jp/datafile/leading/ （個人で見るためだけに使う）
CREATE TABLE IF NOT EXISTS sire_leading (
    year            INTEGER NOT NULL,
    kind            TEXT NOT NULL,     -- all=全馬 / two=2歳 / bms=ブルードメアサイヤー
    sire_name       TEXT NOT NULL,
    rank            INTEGER,
    birth_year      INTEGER,
    coat_color      TEXT,
    birthplace      TEXT,
    n_horses        INTEGER,           -- 出走頭数
    n_winners       INTEGER,           -- 勝馬頭数
    n_starts        INTEGER,           -- 出走回数
    n_wins          INTEGER,           -- 勝利回数
    prize_yen       INTEGER,           -- 賞金（本賞＋付加賞）
    prize_per_start INTEGER,           -- 1出走賞金
    prize_per_horse INTEGER,           -- 1頭平均賞金
    win_rate        REAL,              -- 勝馬率（＝勝ち上がり率）
    ei              REAL,              -- E・I（アーニングインデックス）
    as_of           TEXT,              -- 「◯年◯月◯日現在」。当年は集計途中
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (year, kind, sire_name)
);
CREATE INDEX IF NOT EXISTS idx_sire_leading_name ON sire_leading(kind, sire_name, year);

-- 種牡馬リーディングのページを開くトークン（年度・ページごと）。
-- オッズ・出馬表と同じ理由でためておく（末尾はチェックサムなので組み立てられない）。
CREATE TABLE IF NOT EXISTS jra_sire_links (
    kind       TEXT NOT NULL,
    year       INTEGER NOT NULL,
    page       INTEGER NOT NULL DEFAULT 1,
    cname      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, year, page)
);

-- 競走馬の詳細ページを開くトークン（馬で1行）。オッズ・出馬表と同じ理由でためておく
-- （末尾2文字がチェックサムなので組み立てられない）。検索をやり直さずに済む。
CREATE TABLE IF NOT EXISTS jra_horse_links (
    horse_id   TEXT PRIMARY KEY,
    cname      TEXT NOT NULL,
    horse_no   TEXT,              -- 血統登録番号（先頭4桁が生年）
    updated_at TEXT NOT NULL
);

-- 検索し終えた頭文字。同じ頭を何度も引かないための記録。
-- capped=1 は上限（200件）に達していて取りこぼしがある疑い＝頭を1文字伸ばして引き直した
CREATE TABLE IF NOT EXISTS jra_horse_search (
    prefix     TEXT PRIMARY KEY,
    n_hits     INTEGER NOT NULL,
    capped     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- 種牡馬分析は horses.sire で全走を絞るので、索引を付ける
CREATE INDEX IF NOT EXISTS idx_horses_sire ON horses(sire);
CREATE INDEX IF NOT EXISTS idx_horses_name ON horses(horse_name);

-- 5代血統表の祖先そのもの（名前はここだけに持ち、horse_ancestors からは番号で参照する）。
-- 出どころ: netkeiba の5代血統表。**個人で見るためだけに使い、再配布はしない。**
-- JRA公式は日本で走っていない祖先のページを持たず4〜5代目が埋まらないため、ここだけ別。
CREATE TABLE IF NOT EXISTS pedigree_horses (
    horse_no   TEXT PRIMARY KEY,   -- 日本産は血統登録番号(2010105827)、外国産は 000a00033a 形式
    name       TEXT NOT NULL,      -- 産国を外した馬名（「サンデーサイレンス」「Halo」）
    country    TEXT,               -- 「米」「愛」。日本産はNULL
    updated_at TEXT NOT NULL
);

-- 5代血統表を1行1マスで持つ（1頭62行）。
-- path は父=f・母=m を並べたもので、長さがそのまま代数になる
-- （'f'=父、'mf'=母の父、'mmmmm'=母の母の母の母の母）。
CREATE TABLE IF NOT EXISTS horse_ancestors (
    horse_id    TEXT NOT NULL,
    path        TEXT NOT NULL,
    generation  INTEGER NOT NULL,  -- 1〜5（path の長さ）
    ancestor_no TEXT NOT NULL,
    -- netkeiba=netkeibaの血統表そのもの（1頭62マス） / local=手元のデータから組んだもの（`bloodline-local`。
    -- マスが欠けることがある）。netkeiba で取り直すと local の行はその馬ごと置き換わる
    source      TEXT NOT NULL DEFAULT 'netkeiba',
    PRIMARY KEY (horse_id, path)
);
-- 「この祖先を5代内に持つ馬」を引くための索引（血統クロス分析の主役）
CREATE INDEX IF NOT EXISTS idx_horse_ancestors_ancestor
    ON horse_ancestors(ancestor_no, generation);
CREATE INDEX IF NOT EXISTS idx_pedigree_horses_name ON pedigree_horses(name);

-- 種牡馬（父・母の父として出てくる馬）の名寄せ（sire_keys.py。target-import のたびに作り直す）。
-- 同じ馬が「サンデーサイレンス」と「Sunday Silence」、国内の繁殖登録番号 1120001232 と
-- 海外記録の 1140004339 に割れているのを、(番号, 名前) のつながりで1頭にまとめる。
-- sire_key は国内の繁殖登録番号（無ければいちばん小さい番号）
CREATE TABLE IF NOT EXISTS stallions (
    sire_key TEXT PRIMARY KEY,
    name     TEXT NOT NULL             -- 代表名（2023年以降の馬が使う表記＝JRAの表記を優先）
);
CREATE TABLE IF NOT EXISTS stallion_names (
    name     TEXT PRIMARY KEY,         -- horses.sire / broodmare_sire に出てくる表記すべて
    sire_key TEXT NOT NULL REFERENCES stallions(sire_key)
);
CREATE INDEX IF NOT EXISTS idx_horses_sire_key ON horses(sire_key);
CREATE INDEX IF NOT EXISTS idx_horses_bms_key ON horses(broodmare_sire_key);
CREATE INDEX IF NOT EXISTS idx_horses_dam_key ON horses(dam_key);


-- ============================================================
-- Target（TARGET frontier JV）から書き出したCSVの取り込み先（`keiba-data target-import`）。
-- CSVを正規化しただけの「全列保存」の層で、PCI などTargetにしか無い指標はここにだけある。
-- 本体（races/entries/results/horses/...）には、ここから足りない行と空欄だけを写す
-- （既存の値は上書きしない。埋めた欄は target_fills に残す）。ほかに、ここから作るもの:
-- races.race_name_plain / entries.owner_id_at_export / stallions・horses.sire_key /
-- horse_ancestors の source='local' の行（`bloodline-local`）。
-- race_id / umaban / horse_id / jockey_id / trainer_id は本体と同じ体系（2023-01〜2026-09で全件一致を確認）。
-- 元データは個人で使うためだけのもの。**閲覧用DBやgitに出さない。**
-- ============================================================

-- レース（1レース1行）。race_id は Target の18桁ID（年月日+場+回+日+R+馬番）の [0:4]+[8:16]
CREATE TABLE IF NOT EXISTS target_races (
    race_id          TEXT PRIMARY KEY,
    race_date        TEXT NOT NULL,          -- 'YYYY-MM-DD'
    venue_code       TEXT NOT NULL REFERENCES venues(venue_code),
    kaiji            INTEGER NOT NULL,
    nichime          INTEGER NOT NULL,
    race_no          INTEGER NOT NULL,
    kaisai_label     TEXT,                   -- '5中9'（原文）
    race_name_short  TEXT,                   -- Targetの略称（正式名ではない）
    class_name       TEXT,                   -- 未勝利/1勝/500万/オープン/G3/JG1/OP(L) 等
    class_code       TEXT,
    race_symbol_code TEXT,                   -- 競走記号（JVコード。混・指・特指・国際）
    race_type_code   TEXT,                   -- 競走種別（JVコード。2歳・3歳以上 等）
    weight_type_code TEXT,                   -- 重量種別 1=ハンデ 2=別定 3=馬齢 4=定量
    track_code       TEXT,                   -- Target独自のコード（意味は未確認）
    track_code_jv    TEXT,                   -- JVのトラックコード（10〜22=芝、23〜29=ダート、51〜59=障害）
    surface          TEXT,                   -- turf/dirt/jump
    distance_m       INTEGER,
    turf_inout       TEXT,                   -- 内/外
    course_setting   TEXT,                   -- 芝の使用コース A〜D（A1/A2 もある）
    going            TEXT,                   -- 良/稍重/重/不良
    weather          TEXT,
    post_time        TEXT,                   -- 'HH:MM'
    n_registered     INTEGER,                -- 頭数（取消・除外を含む）
    n_runners        INTEGER,                -- 取消・除外を除く（races.n_runners と同じ定義）
    pci3             REAL,
    rpci             REAL,
    source_file      TEXT NOT NULL,
    imported_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_target_races_date ON target_races(race_date);

-- 出走馬（1レース1頭1行。取消・除外・中止を含む）
CREATE TABLE IF NOT EXISTS target_runs (
    race_id             TEXT NOT NULL REFERENCES target_races(race_id) ON DELETE CASCADE,
    umaban              INTEGER NOT NULL,
    waku                INTEGER,
    horse_id            TEXT NOT NULL,       -- 血統登録番号（= horses.horse_id）
    horse_name          TEXT,
    sex                 TEXT,
    age                 INTEGER,
    jockey_id           TEXT,                -- = jockeys.jockey_id
    jockey_name         TEXT,                -- Targetの短縮表記
    trainer_id          TEXT,                -- = trainers.trainer_id
    trainer_name        TEXT,
    stable              TEXT,                -- 美浦/栗東/地方/海外
    kinryo              REAL,
    kinryo_mark         TEXT,                -- 減量記号 ☆▲△◇★
    blinker             INTEGER,             -- 1=着用
    horse_mark          TEXT,                -- (父)(市)(外)(地)(抽)[地][外] 等
    horse_mark_code     TEXT,
    multi_entry         INTEGER,             -- 1=多頭出し（Targetの定義は未確認）
    abnormal_code       INTEGER,             -- 0=正常 1=取消 3=除外 4=中止 7=降着（2・5・6は未確認）
    finish_raw          TEXT,                -- 着順の原文（NFKC。消/外/止/丸数字を含む）
    finish_position     INTEGER,             -- 確定着順。取消・除外・中止はNULL
    arrival_order       INTEGER,             -- 入線順位
    time_sec            REAL,
    margin_sec          REAL,                -- 勝ち馬とのタイム差（1着は2着との差がマイナス）
    margin              TEXT,                -- 着差の文字表記（Targetの原文）
    corner1             INTEGER,
    corner2             INTEGER,
    corner3             INTEGER,
    corner4             INTEGER,
    last_3f             REAL,
    last_3f_rank        INTEGER,
    diff_at_3f          REAL,                -- 上3F地点差（Targetの定義は未確認）
    ave_3f              REAL,
    pci                 REAL,
    good_run            INTEGER,             -- 1=好走（Targetの定義は未確認）
    avg_1f_sec          REAL,
    avg_speed           REAL,
    speed_ex_last3f     REAL,
    speed_last3f        REAL,
    finishing_move      TEXT,                -- 決め手
    running_style       TEXT,                -- 脚質
    win_odds            REAL,
    popularity          INTEGER,
    horse_weight        INTEGER,
    weight_diff         INTEGER,
    prize_man_yen       REAL,                -- 本賞金のみ（results.prize_man_yen は付加賞込み）
    added_prize_man_yen REAL,
    age_days            INTEGER,             -- 生後日数
    source_file         TEXT NOT NULL,
    imported_at         TEXT NOT NULL,
    PRIMARY KEY (race_id, umaban)
);
CREATE INDEX IF NOT EXISTS idx_target_runs_horse ON target_runs(horse_id);
CREATE INDEX IF NOT EXISTS idx_target_runs_jockey ON target_runs(jockey_id);
CREATE INDEX IF NOT EXISTS idx_target_runs_trainer ON target_runs(trainer_id);

-- 競走馬（1頭1行）。祖先の番号は繁殖登録番号で、horses.sire_no（血統登録番号）とは別の体系
CREATE TABLE IF NOT EXISTS target_horses (
    horse_id                  TEXT PRIMARY KEY,
    horse_name                TEXT,
    name_en                   TEXT,
    sex                       TEXT,
    age_at_export             INTEGER,       -- 書き出した時点の馬齢
    status                    TEXT,          -- 在厩/不在/抹消
    horse_mark                TEXT,
    stable                    TEXT,
    trainer_name              TEXT,
    birth_date                TEXT,
    coat_color                TEXT,
    birthplace                TEXT,
    sire_name                 TEXT,
    sire_bms_name             TEXT,          -- 父の母の父
    dam_name                  TEXT,
    bms_name                  TEXT,          -- 母の父
    dam_dam_name              TEXT,
    dam_dam_sire_name         TEXT,
    dam_dam_dam_name          TEXT,
    sire_breed_no             TEXT,
    sire_bms_breed_no         TEXT,
    dam_breed_no              TEXT,
    bms_breed_no              TEXT,
    dam_dam_breed_no          TEXT,
    dam_dam_sire_breed_no     TEXT,
    dam_dam_dam_breed_no      TEXT,
    sire_line                 TEXT,          -- 系統名
    sire_bms_line             TEXT,
    bms_line                  TEXT,
    dam_dam_sire_line         TEXT,
    sire_age                  INTEGER,       -- 何時点の年齢かは未確認
    sire_coat                 TEXT,
    dam_age                   INTEGER,
    dam_coat                  TEXT,
    dam_dam_age               INTEGER,
    dam_dam_coat              TEXT,
    owner_code                TEXT,          -- = owners.owner_id
    owner_name                TEXT,
    silks                     TEXT,          -- 勝負服色
    breeder_name              TEXT,
    earned_prize_man_yen      REAL,          -- 収得賞金
    jump_earned_prize_man_yen REAL,
    main_prize_man_yen        REAL,          -- 本賞金
    added_prize_man_yen       REAL,
    n_1st                     INTEGER,
    n_2nd                     INTEGER,
    n_3rd                     INTEGER,
    n_other                   INTEGER,
    n_races_total             INTEGER,
    n_races_actual            INTEGER,
    n_races_jra               INTEGER,
    first_venue               TEXT,          -- 初場（意味は未確認）
    latest_venue              TEXT,          -- 新場（意味は未確認）
    first_race_key18          TEXT,
    latest_race_key18         TEXT,
    registered_date           TEXT,
    retired_date              TEXT,
    data_created_date         TEXT,          -- 同じ馬が2行あるときは新しい方を採る
    sale_price_man_yen        REAL,          -- 万円（税込の端数がある年は小数）
    sale_price_note           TEXT,          -- '(他)' 等
    sale_name                 TEXT,
    name_origin               TEXT,          -- 馬名の意味由来
    sibling_n                 INTEGER,
    sibling_prize_sum_man_yen REAL,
    sibling_prize_avg_man_yen REAL,
    source_file               TEXT NOT NULL,
    imported_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_target_horses_sire_no ON target_horses(sire_breed_no);
CREATE INDEX IF NOT EXISTS idx_target_horses_owner ON target_horses(owner_code);

-- 収得賞金上位の兄弟（1頭最大5行）
CREATE TABLE IF NOT EXISTS target_horse_siblings (
    horse_id     TEXT NOT NULL REFERENCES target_horses(horse_id) ON DELETE CASCADE,
    rank         INTEGER NOT NULL,
    sibling_name TEXT NOT NULL,
    n_wins       INTEGER,
    PRIMARY KEY (horse_id, rank)
);

-- 取り込んだファイル（中身の sha256 が同じなら取り込み直さない）
CREATE TABLE IF NOT EXISTS target_import_files (
    file_name   TEXT PRIMARY KEY,            -- 'race_data/race_data_2024.csv'
    kind        TEXT NOT NULL,               -- race/horse
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    n_rows      INTEGER NOT NULL,            -- ヘッダーを除く行数
    n_loaded    INTEGER NOT NULL,
    n_skipped   INTEGER NOT NULL,
    run_id      INTEGER REFERENCES runs(run_id),
    imported_at TEXT NOT NULL
);

-- 本体の空欄を Target で埋めた記録（どの行のどの欄を埋めたか）。元に戻すときの手がかり
CREATE TABLE IF NOT EXISTS target_fills (
    table_name  TEXT NOT NULL,
    row_key     TEXT NOT NULL,               -- 主キーを '|' でつないだもの
    column_name TEXT NOT NULL,
    filled_at   TEXT NOT NULL,
    PRIMARY KEY (table_name, row_key, column_name)
);
