"""種牡馬分析の集計（DBの `sire_leading` を読んで、画面に出す数字を作る）。

出どころはJRA公式の種牡馬リーディングだけ（`scrapers/jra_sire.py`）。
Streamlitに依存しない純粋な関数にしてあるので、そのままテストできる。

## E・I（アーニングインデックス＝AEI）の扱い

JRAのE・Iは「その種牡馬の**1頭平均賞金** ÷ 全出走馬の1頭平均賞金」で、**1.00が平均**。
表には上位100頭しか載らないが、載っている行の `1頭平均賞金 ÷ E・I` はどれも同じ値
（＝全出走馬の1頭平均賞金）になるので、**中央値を取れば分母が手に入る**。
E・Iが小数2桁に丸められているぶんの誤差を均すために、平均ではなく中央値を使う。

この分母があれば、**何年ぶんでもまとめた通算AEI**を自分で出せる:

    通算AEI = (Σ賞金 ÷ Σ出走頭数) ÷ (年ごとの分母を出走頭数で重み付けした平均)

ここでの「Σ出走頭数」は**のべ**（複数年走った産駒は年の数だけ数える）。JRAが年ごとに
しか出していない以上これ以外に揃え方がないので、画面にもそう書く。

## 全種牡馬平均

「全種牡馬平均」は、その年のリーディング上位100頭のうち**出走頭数が
`MIN_FIELD_HORSES` 頭以上**の種牡馬だけを母数にした値。数頭しか走っていない種牡馬は
数字が跳ねるので、比較の物差しからは外す。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from statistics import median

from keiba_data.scrapers.jra_sire import KIND_ALL

# 全種牡馬平均・順位付けの母数に入れる最低出走頭数（これ未満は数字が跳ねるので外す）
MIN_FIELD_HORSES = 20
# 主要指標の段階（1〜9。5が全種牡馬のまんなか）
GRADES = 9


@dataclass(frozen=True)
class SireYear:
    """ある年のある種牡馬の産駒成績（`sire_leading` の1行）。"""

    year: int
    rank: int | None
    n_horses: int | None
    n_winners: int | None
    n_starts: int | None
    n_wins: int | None
    prize_yen: int | None
    prize_per_horse: int | None
    win_rate: float | None
    ei: float | None
    as_of: str | None


@dataclass(frozen=True)
class Totals:
    """何年かをまとめた合計（出走頭数は**のべ**）。"""

    years: tuple[int, ...]
    n_horses: int
    n_winners: int
    n_starts: int
    n_wins: int
    prize_yen: int

    @property
    def win_rate(self) -> float | None:
        """勝ち上がり率（勝馬頭数 ÷ 出走頭数）。"""
        return self.n_winners / self.n_horses if self.n_horses else None

    @property
    def starts_per_horse(self) -> float | None:
        """年間平均出走回数（出走回数 ÷ のべ出走頭数）。"""
        return self.n_starts / self.n_horses if self.n_horses else None

    @property
    def prize_per_horse(self) -> float | None:
        """1頭平均賞金（円）。"""
        return self.prize_yen / self.n_horses if self.n_horses else None


@dataclass(frozen=True)
class Metric:
    """画面に出す指標ひとつ。値・全種牡馬平均・分布の中の位置をまとめて持つ。"""

    key: str
    label: str
    value: float | None
    field_value: float | None          # 全種牡馬平均
    percentile: float | None           # 0.0〜1.0（全種牡馬の中で下から何割か）
    low_label: str                     # ゲージの左端
    high_label: str                    # ゲージの右端
    description: str
    unit: str = ""

    @property
    def grade(self) -> int | None:
        """1〜9の段階（5が全種牡馬のまんなか）。"""
        if self.percentile is None:
            return None
        return min(GRADES, int(self.percentile * GRADES) + 1)

    @property
    def ratio_to_field(self) -> float | None:
        """全種牡馬平均を1.0としたときの倍率。"""
        if self.value is None or not self.field_value:
            return None
        return self.value / self.field_value


def _row_to_year(row: sqlite3.Row) -> SireYear:
    return SireYear(
        year=row["year"], rank=row["rank"], n_horses=row["n_horses"],
        n_winners=row["n_winners"], n_starts=row["n_starts"], n_wins=row["n_wins"],
        prize_yen=row["prize_yen"], prize_per_horse=row["prize_per_horse"],
        win_rate=row["win_rate"], ei=row["ei"], as_of=row["as_of"],
    )


def available_years(conn: sqlite3.Connection, kind: str = KIND_ALL) -> list[int]:
    """DBに入っている年（新しい順）。まだ取り込んでいなければ空。"""
    rows = conn.execute(
        "SELECT DISTINCT year FROM sire_leading WHERE kind = ? ORDER BY year DESC", (kind,)
    )
    return [r["year"] for r in rows]


def recent_years(conn: sqlite3.Connection, n: int, kind: str = KIND_ALL) -> list[int]:
    """直近n年（古い順）。グラフの横軸と集計の対象に使う。"""
    return sorted(available_years(conn, kind)[:n])


def list_sires(conn: sqlite3.Connection, years: list[int], kind: str = KIND_ALL) -> list[dict]:
    """選択UIに出す種牡馬の一覧。**いちばん新しい年の出走頭数が多い順**。

    リーディングは年ごとに上位100頭なので、5年ぶんを合わせると150〜200頭ほどになる。
    それに加えて、**手元DBで産駒が見つかっている種牡馬**（リーディングに載らなかった
    種牡馬）も選べるようにする。手元DBにしかいない種牡馬はリーディング由来の数字が
    出ないが、距離適性などのタブは見られる。
    """
    rows: dict[str, dict] = {}
    if years:
        marks = ", ".join("?" * len(years))
        for row in conn.execute(
            f"""
            SELECT sire_name,
                   MAX(year) AS latest_year,
                   SUM(n_horses) AS n_horses,
                   MAX(birth_year) AS birth_year
              FROM sire_leading
             WHERE kind = ? AND year IN ({marks})
          GROUP BY sire_name
            """,
            (kind, *years),
        ):
            rows[row["sire_name"]] = {**dict(row), "in_leading": True}

    # 手元DBで産駒が見つかっている種牡馬（リーディングに載っていないものを足す）
    for row in conn.execute(
        "SELECT sire AS sire_name, COUNT(*) AS local_horses FROM horses "
        "WHERE sire IS NOT NULL AND sire <> '' GROUP BY sire"
    ):
        current = rows.get(row["sire_name"])
        if current is None:
            rows[row["sire_name"]] = {
                "sire_name": row["sire_name"], "latest_year": None, "n_horses": 0,
                "birth_year": None, "in_leading": False,
                "local_horses": row["local_horses"],
            }
        else:
            current["local_horses"] = row["local_horses"]

    return sorted(
        rows.values(),
        key=lambda r: (-(r["latest_year"] or 0), -(r["n_horses"] or 0),
                       -(r.get("local_horses") or 0), r["sire_name"]),
    )


def sire_years(
    conn: sqlite3.Connection, sire_name: str, years: list[int], kind: str = KIND_ALL,
) -> list[SireYear]:
    """その種牡馬の年ごとの成績（古い順）。載っていない年は入らない。"""
    if not years:
        return []
    marks = ", ".join("?" * len(years))
    rows = conn.execute(
        f"SELECT * FROM sire_leading WHERE kind = ? AND sire_name = ? AND year IN ({marks}) "
        "ORDER BY year",
        (kind, sire_name, *years),
    )
    return [_row_to_year(r) for r in rows]


def field_prize_per_horse(conn: sqlite3.Connection, year: int, kind: str = KIND_ALL) -> float | None:
    """その年の**全出走馬の1頭平均賞金**（＝E・Iの分母）を逆算する。

    上位100頭の `1頭平均賞金 ÷ E・I` はどれも同じ値になるはずなので、
    E・Iの丸め誤差を均すために中央値を取る。
    """
    values = [
        r["prize_per_horse"] / r["ei"]
        for r in conn.execute(
            "SELECT prize_per_horse, ei FROM sire_leading "
            "WHERE kind = ? AND year = ? AND ei > 0 AND prize_per_horse > 0",
            (kind, year),
        )
    ]
    return median(values) if values else None


def totals(rows: list[SireYear]) -> Totals:
    """年ごとの行を合計する（欠けている値は0として足す）。"""
    def add(key: str) -> int:
        return sum(getattr(r, key) or 0 for r in rows)

    return Totals(
        years=tuple(r.year for r in rows),
        n_horses=add("n_horses"), n_winners=add("n_winners"),
        n_starts=add("n_starts"), n_wins=add("n_wins"), prize_yen=add("prize_yen"),
    )


def cumulative_aei(
    conn: sqlite3.Connection, rows: list[SireYear], kind: str = KIND_ALL,
) -> float | None:
    """何年かをまとめた通算AEI。分母は年ごとの全馬1頭平均賞金を出走頭数で重み付けした平均。"""
    denominators = {r.year: field_prize_per_horse(conn, r.year, kind) for r in rows}
    return _aei_from(rows, denominators)


# --- 全種牡馬の分布 ---------------------------------------------------------------

def field_rows(
    conn: sqlite3.Connection, years: list[int], kind: str = KIND_ALL,
) -> dict[str, list[SireYear]]:
    """その年範囲に載っている種牡馬ぜんぶ `{種牡馬名: 年ごとの行}`。1クエリで読む。"""
    if not years:
        return {}
    marks = ", ".join("?" * len(years))
    rows = conn.execute(
        f"SELECT * FROM sire_leading WHERE kind = ? AND year IN ({marks}) ORDER BY sire_name, year",
        (kind, *years),
    )
    per_sire: dict[str, list[SireYear]] = {}
    for row in rows:
        per_sire.setdefault(row["sire_name"], []).append(_row_to_year(row))
    return per_sire


def _aei_from(rows: list[SireYear], denominators: dict[int, float | None]) -> float | None:
    """年ごとの分母（全馬の1頭平均賞金）を渡して通算AEIを出す。"""
    total = totals(rows)
    if not total.n_horses:
        return None
    weighted = sum((denominators.get(r.year) or 0) * (r.n_horses or 0) for r in rows)
    weight = sum(r.n_horses or 0 for r in rows if denominators.get(r.year))
    if not weight:
        return None
    return (total.prize_yen / total.n_horses) / (weighted / weight)


def _percentile(values: list[float], value: float) -> float:
    """`values` の中で `value` が下から何割の位置にあるか（0.0〜1.0）。"""
    if not values:
        return 0.5
    below = sum(1 for v in values if v < value)
    same = sum(1 for v in values if v == value)
    return (below + same / 2) / len(values)


# 画面に出す指標の並びと説明。value_of は Totals から値を取り出す
_METRIC_SPECS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "win_rate", "勝ち上がり率", "低い", "高い", "%",
        "出走した産駒のうち、1勝でも挙げた頭数の割合（JRAの「勝馬率」そのもの）。"
        "未勝利で終わらない産駒がどれだけ出るかで、一口出資でいちばん効いてくる数字。",
    ),
    (
        "starts_per_horse", "年間平均出走回数", "使われない", "よく使われる", "回",
        "出走回数 ÷ のべ出走頭数。1年に何回使えるか＝丈夫さと順調さの目安。"
        "少ないと、故障や体質で間隔が空いている産駒が多いことを示す。",
    ),
    (
        "prize_per_horse", "1頭平均賞金", "低い", "高い", "円",
        "賞金 ÷ のべ出走頭数。E・Iの元になっている数字そのもの。"
        "大物が1頭出ると大きく跳ねるので、平均より上でも「全体が走る」とは限らない。"
        "併記している全種牡馬平均は、リーディング掲載馬だけでなく**全出走馬**の1頭平均賞金"
        "（E・Iの分母）なので、この値をそれで割るとE・Iになる。",
    ),
    (
        "aei", "E・I（AEI）", "低い", "高い", "",
        "1頭平均賞金 ÷ 全出走馬の1頭平均賞金。**1.00が全種牡馬の平均**で、"
        "2.00なら平均の倍を稼いでいる。1頭平均賞金を年ごとの水準で割り戻したもの。",
    ),
    (
        "n_horses", "産駒の規模", "少ない", "多い", "頭",
        "のべ出走頭数（年をまたぐ産駒は年の数だけ数える）。多いほど数字が安定する一方、"
        "クラブの募集でも競争相手が多くなる。",
    ),
)


def _value_of(key: str, total: Totals, aei: float | None) -> float | None:
    if key == "win_rate":
        return total.win_rate
    if key == "starts_per_horse":
        return total.starts_per_horse
    if key == "prize_per_horse":
        return total.prize_per_horse
    if key == "n_horses":
        return float(total.n_horses) if total.n_horses else None
    if key == "aei":
        return aei
    return None


def _field_value(
    key: str, whole: Totals, n_sires: int, denominators: dict[int, float | None],
    years: list[int], everyone: dict[str, list[SireYear]],
) -> float | None:
    """その指標の「全種牡馬平均」。指標ごとに意味のある揃え方が違う。

    - E・I … 定義上 1.00 が平均
    - 1頭平均賞金 … **全出走馬**の1頭平均賞金（E・Iの分母そのもの）。
      リーディング掲載馬だけの平均にすると、E・Iの1.00と食い違ってしまう
    - 産駒の規模 … 1種牡馬あたりの平均（合計ではない）
    - それ以外 … 合計どうしの比（頭数で重み付けした平均と同じ）
    """
    if key == "aei":
        return 1.0
    if key == "prize_per_horse":
        # 年ごとの分母を、その年の掲載馬ののべ出走頭数で重み付けして均す
        weights = {
            year: sum(r.n_horses or 0 for rows in everyone.values() for r in rows if r.year == year)
            for year in years
        }
        weighted = sum((denominators.get(y) or 0) * w for y, w in weights.items())
        total_weight = sum(w for y, w in weights.items() if denominators.get(y))
        return weighted / total_weight if total_weight else None
    if key == "n_horses":
        return whole.n_horses / n_sires if n_sires else None
    return _value_of(key, whole, None)


def sire_metrics(
    conn: sqlite3.Connection, sire_name: str, years: list[int], kind: str = KIND_ALL,
) -> list[Metric]:
    """その種牡馬の主要指標を、全種牡馬の分布の中の位置つきで返す。

    全種牡馬平均は、出走頭数が `MIN_FIELD_HORSES` 頭以上の種牡馬だけを母数にした
    **合計どうしの比**（頭数で重み付けした平均と同じ）。E・Iだけは定義上1.00が平均。
    """
    # 分母（全馬の1頭平均賞金）は年ごとに1回だけ引く
    denominators = {year: field_prize_per_horse(conn, year, kind) for year in years}
    everyone = field_rows(conn, years, kind)
    rows = everyone.get(sire_name) or []
    if not rows:
        return []
    mine, my_aei = totals(rows), _aei_from(rows, denominators)

    field = {
        name: (total, _aei_from(items, denominators))
        for name, items in everyone.items()
        if (total := totals(items)).n_horses >= MIN_FIELD_HORSES
    }
    whole = totals([r for name in field for r in everyone[name]])

    metrics: list[Metric] = []
    for key, label, low, high, unit, description in _METRIC_SPECS:
        value = _value_of(key, mine, my_aei)
        others = [
            v for total, aei in field.values()
            if (v := _value_of(key, total, aei)) is not None
        ]
        field_value = _field_value(key, whole, len(field), denominators, years, everyone)
        metrics.append(Metric(
            key=key, label=label, value=value, field_value=field_value,
            percentile=_percentile(others, value) if value is not None else None,
            low_label=low, high_label=high, description=description, unit=unit,
        ))
    return metrics
