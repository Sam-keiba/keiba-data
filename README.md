# keiba-data

JRA公式・netkeibaからのレースデータ取得と、SQLiteへの保存を担うデータ基盤層。

`keiba`プロジェクトを `keiba-data` / `keiba-analysis` / `keiba-app` の3リポジトリへ分割した際の、
データ取得・保存・DBスキーマ・ソース非依存の「事実」集計を担当するリポジトリ。
ダッシュボード・分析ロジックは含まない（`keiba-analysis` / `keiba-app` 側）。

## セットアップ

```bash
uv sync
uv run pytest
```

## CLI

```bash
uv run keiba-data refresh       # いつもの更新: update → entries → baba → bloodline --new → publish をまとめて実行
uv run keiba-data update        # 前回の続き〜今日のレース結果を取得
uv run keiba-data backfill --from 2023-01-01
uv run keiba-data status
uv run keiba-data target-import --merge-only   # Targetの取り込み分から本体・付記の無いレース名・名寄せキーを作り直す
uv run keiba-data bloodline-local              # 手元のデータだけで5代血統表を広げる（ネットにアクセスしない）
uv run keiba-data trainer-meikan               # 現役調教師の正式名・読み・生年月日・免許取得年（JRA調教師名鑑）
uv run keiba-data trainer-stalls-import data/trainer_stalls/2026-03-04.csv   # 貸付馬房数（確かめたCSVから）
```

貸付馬房数のCSVは `effective_date,stable,name,stalls`（1行1調教師・UTF-8）。JRAの発表PDFはスキャン画像なので、
読み取ったものを確かめてから入れる。結びつかない名前は取り込まずに一覧で出る。

いつもの更新は keiba フォルダ直下から `./refresh` でも流せる。直下にリンクを置いておく（最初の1回だけ）:

```bash
cd ~/Desktop/keiba && ln -s keiba-data/refresh refresh
```

## 自動更新

```bash
./schedule/install     # Macに予約を登録（最初の1回だけ。確認画面で「許可」を押す）
./schedule/uninstall   # 予約を外す
uv run keiba-data auto --dry-run   # いま更新が要るかだけ確かめる
```

毎日18時に判定し、**開催日の前日15時以降に未更新**か、**2日以上前の開催日の結果が揃っていない**ときだけ
refresh を流して、閲覧用DBのアップロードまで行う。Macが閉じていて時刻を逃したら、次に開いたときに追いつく。
結果はMacの通知で届く。記録は `~/Library/Logs/keiba-refresh.log` と `logs/`。

詳しくは `uv run keiba-data --help` を参照。
