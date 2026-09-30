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
```

詳しくは `uv run keiba-data --help` を参照。
