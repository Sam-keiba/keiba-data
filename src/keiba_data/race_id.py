"""race_idのデコード・判定。

race_idは12桁で以下の構造を持つ（実データで検証済み: 2026年中山4回4日目11R = 202606040411）:
    1-4桁:  開催年
    5-6桁:  競馬場コード（config.VENUE_CODES参照。01-10がJRA）
    7-8桁:  開催回（第◯回）
    9-10桁: 開催日目
    11-12桁: レース番号
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from keiba_data import config

_BARE_RACE_ID_RE = re.compile(r"^\d{12}$")


@dataclass(frozen=True)
class RaceIdInfo:
    race_id: str
    year: int
    venue_code: str
    venue_name: str
    kaiji: int  # 開催回（第◯回）
    nichime: int  # 開催日目
    race_no: int  # レース番号


def is_jra_race_id(race_id: str) -> bool:
    return bool(_BARE_RACE_ID_RE.match(race_id)) and race_id[4:6] in config.VENUE_CODES


def compose_race_id(year: int, venue_code: str, kaiji: int, nichime: int, race_no: int) -> str:
    """年・競馬場コード・開催回・開催日目・レース番号から12桁のrace_idを組み立てる。

    JRA公式サイトの結果ページには netkeiba の race_id が無いので、こちらで組み立てて
    同じレースの行（races.race_id）に突き合わせる。`decode_race_id` と対になる。
    """
    if venue_code not in config.VENUE_CODES:
        raise ValueError(f"JRAの競馬場コードではありません: {venue_code!r}")
    if not (1 <= race_no <= 12 and 1 <= kaiji <= 99 and 1 <= nichime <= 99):
        raise ValueError(f"開催回・日目・レース番号が範囲外です: {kaiji}回{nichime}日目{race_no}R")
    return f"{year:04d}{venue_code}{kaiji:02d}{nichime:02d}{race_no:02d}"


def decode_race_id(race_id: str) -> RaceIdInfo:
    """12桁のrace_idを年・競馬場コード・開催回・開催日目・レース番号に分解する。"""
    if not _BARE_RACE_ID_RE.match(race_id):
        raise ValueError(f"race_idは12桁の数字である必要があります: {race_id!r}")

    venue_code = race_id[4:6]
    venue_name = config.VENUE_CODES.get(venue_code)
    if venue_name is None:
        raise ValueError(
            f"JRAの競馬場コードではありません: {venue_code!r} (地方競馬のrace_idの可能性があります)"
        )

    return RaceIdInfo(
        race_id=race_id,
        year=int(race_id[0:4]),
        venue_code=venue_code,
        venue_name=venue_name,
        kaiji=int(race_id[6:8]),
        nichime=int(race_id[8:10]),
        race_no=int(race_id[10:12]),
    )
