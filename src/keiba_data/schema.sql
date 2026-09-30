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
    updated_at      TEXT NOT NULL         -- 最後に保存した日時
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
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jockeys (
    jockey_id   TEXT PRIMARY KEY,
    jockey_name TEXT,                     -- 結果ページの表記（例: ルメール）
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trainers (
    trainer_id   TEXT PRIMARY KEY,
    trainer_name TEXT,
    stable       TEXT,                    -- 美浦/栗東/地方/海外
    updated_at   TEXT NOT NULL
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
    owner_id     TEXT REFERENCES owners(owner_id),
    horse_weight INTEGER,                 -- 馬体重(kg)
    weight_diff  INTEGER,                 -- 前走比増減(kg)
    PRIMARY KEY (race_id, umaban)
);
CREATE INDEX IF NOT EXISTS idx_entries_horse ON entries(horse_id);
CREATE INDEX IF NOT EXISTS idx_entries_jockey ON entries(jockey_id);
CREATE INDEX IF NOT EXISTS idx_entries_trainer ON entries(trainer_id);

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
    PRIMARY KEY (horse_id, path)
);
-- 「この祖先を5代内に持つ馬」を引くための索引（血統クロス分析の主役）
CREATE INDEX IF NOT EXISTS idx_horse_ancestors_ancestor
    ON horse_ancestors(ancestor_no, generation);
CREATE INDEX IF NOT EXISTS idx_pedigree_horses_name ON pedigree_horses(name);
