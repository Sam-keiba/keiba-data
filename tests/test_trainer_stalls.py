"""調教師別の貸付馬房数（人が確かめたCSV）の取り込み。"""

import pytest

from keiba_data import trainer_stalls


@pytest.fixture
def trainers(conn):
    rows = [
        ("01137", "中内田充", "栗東", "中内田 充正"),
        ("00271", "二本柳俊", "美浦", "二本柳 俊一"),
        ("00370", "二本柳俊", "美浦", "二本柳 俊夫"),
        ("01001", "高橋義忠", "栗東", None),          # 名鑑に無い（正式名が空）→ 4文字で照合
        ("01002", "鈴木慎太", "美浦", None),
        ("01003", "鈴木慎太", "栗東", None),          # 4文字が同じでも所属が違えば別人
    ]
    with conn:
        conn.executemany("INSERT INTO trainers (trainer_id, trainer_name, stable, full_name, updated_at) "
                         "VALUES (?, ?, ?, ?, '2026-01-01')", rows)
    return conn


def write(path, lines):
    path.write_text("effective_date,stable,name,stalls\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def stalls(conn):
    return dict(conn.execute("SELECT trainer_id, stalls FROM trainer_stalls"))


def test_matches_full_name_then_four_letters(trainers, tmp_path):
    path = write(tmp_path / "s.csv", [
        "2026-03-04,栗東,中内田　充正,22",        # 全角空白でも正式名で引ける
        "2026-03-04,美浦,二本柳 俊夫,14",         # 4文字が同じ2人を正式名で見分ける
        "2026-03-04,栗東,髙橋 義忠,18",           # 字体の揺れ（髙/高）をならし、4文字で引く
        "2026-03-04,美浦,鈴木 慎太郎,16",
    ])
    result = trainer_stalls.import_csv(trainers, path)
    assert result.unmatched == []
    assert stalls(trainers) == {"01137": 22, "00370": 14, "01001": 18, "01002": 16}
    assert result.totals == {("2026-03-04", "栗東"): 40, ("2026-03-04", "美浦"): 30}


def test_unmatched_names_are_listed_not_saved(trainers, tmp_path):
    path = write(tmp_path / "s.csv", [
        "2026-03-04,美浦,二本柳 俊,10",           # 正式名で決まらず、4文字だと2人 → 決めない
        "2026-03-04,美浦,知らない 人,10",
        "2026-03-04,栗東,中内田 充正,20",
    ])
    result = trainer_stalls.import_csv(trainers, path)
    assert [r["name"] for r in result.unmatched] == ["二本柳 俊", "知らない 人"]
    assert stalls(trainers) == {"01137": 20}


def test_reimport_replaces_the_same_date_and_stable(trainers, tmp_path):
    trainer_stalls.import_csv(trainers, write(tmp_path / "a.csv", [
        "2026-03-04,栗東,中内田 充正,20", "2026-03-04,美浦,二本柳 俊夫,14"]))
    trainer_stalls.import_csv(trainers, write(tmp_path / "b.csv", ["2026-03-04,栗東,中内田 充正,24"]))
    # 栗東だけ入れ直した。美浦の行は残る
    assert stalls(trainers) == {"01137": 24, "00370": 14}


def test_bad_rows_stop_the_import(trainers, tmp_path):
    with pytest.raises(ValueError, match="所属"):
        trainer_stalls.import_csv(trainers, write(tmp_path / "s.csv", ["2026-03-04,地方,中内田 充正,20"]))
    with pytest.raises(ValueError, match="馬房数"):
        trainer_stalls.import_csv(trainers, write(tmp_path / "s.csv", ["2026-03-04,栗東,中内田 充正,二十"]))
    with pytest.raises(ValueError, match="日付"):
        trainer_stalls.import_csv(trainers, write(tmp_path / "s.csv", ["2026/3/4,栗東,中内田 充正,20"]))
    assert stalls(trainers) == {}
