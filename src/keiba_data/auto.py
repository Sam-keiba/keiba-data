"""予約実行（`keiba-data auto`）が「いま更新するか」を決める。

Macの予約は毎日同じ時刻に起きるが、更新が要るのは次のときだけ:

- 出馬表: 開催日の**前日15時**を過ぎてから、まだ更新していない
  （枠順が出そろう前に取っても意味がないので、15時より前は数えない）
- 結果: 2日以上前の開催日に、結果の揃っていない日がある
  （netkeibaのdbページは当日・前日の結果をまだ載せない）

Macが閉じていて時刻を逃しても、次に開いたときの判定で追いつける。
「いつ更新したか」だけで決まるので、手で `./refresh` した分も数えられる。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

ENTRIES_READY_AT = time(15, 0)
RESULT_DELAY_DAYS = 2
# これより古い開催日は update の担当から外す（中止などで揃わない日に毎日反応しないように）
RESULT_LOOKBACK_DAYS = 14


def entries_due_since(now: datetime, race_days: Iterable[date]) -> datetime | None:
    """まだ終わっていない開催日のうち、出馬表を取れるようになった最新の時刻（無ければNone）。"""
    ready = [
        datetime.combine(day - timedelta(days=1), ENTRIES_READY_AT)
        for day in race_days if day >= now.date()
    ]
    passed = [moment for moment in ready if moment <= now]
    return max(passed, default=None)


def due_reason(
    now: datetime, race_days: Iterable[date], incomplete_days: Iterable[date], last_ok: datetime | None,
) -> str | None:
    """更新する理由を返す。今は要らなければNone。

    race_days は開催日、incomplete_days は結果の揃っていない開催日、
    last_ok は最後にアップロードまで成功した時刻。
    """
    since = entries_due_since(now, race_days)
    if since is not None and (last_ok is None or last_ok < since):
        return "出馬表"
    today = now.date()
    pending = [
        day for day in incomplete_days
        if today - timedelta(days=RESULT_LOOKBACK_DAYS) <= day <= today - timedelta(days=RESULT_DELAY_DAYS)
    ]
    if pending and (last_ok is None or last_ok.date() < today):
        return f"{max(pending).isoformat()} の結果"
    return None
