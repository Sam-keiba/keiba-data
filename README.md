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

詳しくは `uv run keiba-data --help` を参照。
