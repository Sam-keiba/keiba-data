"""閲覧用DB（クラウド版が読むもの）の作り方。"""

import gzip
import sqlite3

import pytest

from keiba_data import db, publish
from keiba_data.scrapers.race_result import parse_race_result
from keiba_data.scrapers.shutuba import parse_shutuba
from tests.conftest import load_fixture


@pytest.fixture
def filled(tmp_path):
    """結果1レース・出馬表1レース・予想ボード・買い目が入ったDBを作る。"""
    path = tmp_path / "keiba.db"
    conn = db.connect(path)
    db.save_race_page(conn, parse_race_result(load_fixture("result_202606040411_g2.html"), "202606040411"))
    db.save_upcoming_shutuba(
        conn, parse_shutuba(load_fixture("shutuba_202606040611.html"), "202606040611", "2026-09-20")
    )
    db.save_board(conn, "202606040611", [{
        "horse_id": "2021102800", "tier": "A", "position": 0.8, "lane_offset": 0.5,
        "comment": "メモ", "is_manual": True, "is_excluded": False,
    }])
    db.save_bet_slips(conn, "202606040611", [{"bet": "umaren", "combos": ["1-2"], "amount": 100}])
    db.start_run(conn, "update", "テスト")
    conn.close()
    return path


def tables(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    return names


def test_the_viewer_db_drops_what_the_screen_never_shows(filled, tmp_path):
    viewer = publish.build_viewer_db(filled, tmp_path / "out" / "viewer.db")
    left = tables(viewer)
    assert not (left & set(publish.VIEWER_DROP))      # 払戻・取り込みの記録は入れない
    assert "target_horses" not in left and "target_runs" not in left   # Target の取り込み層は出さない
    assert "horse_ancestors" not in left and "pedigree_horses" not in left   # 血統表は手元専用
    # 画面が読む表はそのまま残る
    for table in ("races", "entries", "results", "race_laps", "horses", "upcoming_races",
                  "upcoming_entries", "track_conditions", "odds", "board_horses", "bet_slips"):
        assert table in left, table


def test_the_viewer_db_keeps_the_rows(filled, tmp_path):
    """落とすのは表ごと。残した表の中身は1行も減らさない（画面の見え方を変えないため）。"""
    viewer = publish.build_viewer_db(filled, tmp_path / "viewer.db")
    before = sqlite3.connect(f"file:{filled}?mode=ro", uri=True)
    after = sqlite3.connect(f"file:{viewer}?mode=ro", uri=True)
    for table in ("races", "entries", "results", "upcoming_entries", "board_horses", "bet_slips"):
        n_before = before.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        n_after = after.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert n_before == n_after > 0, table
    # 予想ボードのメモも渡る（PCで書いたものをiPhoneで読むため）
    assert after.execute("SELECT comment FROM board_horses").fetchone()[0] == "メモ"
    before.close()
    after.close()


def test_the_original_is_not_touched(filled, tmp_path):
    before = filled.read_bytes()
    publish.build_viewer_db(filled, tmp_path / "viewer.db")
    assert filled.read_bytes() == before      # 手元のDBは読むだけ


def test_building_twice_is_fine(filled, tmp_path):
    """VACUUM INTO は行き先が在ると失敗するので、作り直せることを確かめる。"""
    destination = tmp_path / "viewer.db"
    publish.build_viewer_db(filled, destination)
    publish.build_viewer_db(filled, destination)
    assert "races" in tables(destination)


def test_compress_makes_a_smaller_file_that_opens_again(filled, tmp_path):
    viewer = publish.build_viewer_db(filled, tmp_path / "viewer.db")
    packed = publish.compress(viewer)
    assert packed.stat().st_size < viewer.stat().st_size
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(packed.read_bytes()))
    assert "races" in tables(restored)


def test_publish_can_stop_before_uploading(filled, tmp_path, monkeypatch):
    """`--no-upload` はファイルを作るだけ（ネットに出ない）。"""
    called = []
    monkeypatch.setattr(publish, "upload", lambda *a, **k: called.append(a) or True)
    path = publish.publish(db_path=filled, out_dir=tmp_path / "dist", do_upload=False)
    assert path.name == publish.ASSET_NAME and path.exists()
    assert called == []


def test_publish_uploads_the_compressed_file(filled, tmp_path, monkeypatch):
    seen = {}

    def fake_upload(path, repo, tag=publish.RELEASE_TAG):
        seen.update(path=path, repo=repo, tag=tag)
        return True

    monkeypatch.setattr(publish, "upload", fake_upload)
    publish.publish(db_path=filled, out_dir=tmp_path / "dist", repo="me/repo")
    assert seen["path"].name == publish.ASSET_NAME
    assert (seen["repo"], seen["tag"]) == ("me/repo", "db")


def test_upload_creates_the_release_when_it_is_missing(tmp_path, monkeypatch):
    """タグがまだ無いときは作ってから上げる（初回の1回だけ通る道）。"""
    calls = []

    class Done:
        returncode = 0
        stderr = ""

    class Missing:
        returncode = 1
        stderr = "release not found"

    def fake_run(argv, **kwargs):
        calls.append(argv[1:4])
        return Missing() if argv[1:3] == ["release", "view"] else Done()

    monkeypatch.setattr(publish.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    assert publish.upload(tmp_path / "x.gz", "me/repo") is True
    assert calls == [["release", "view", "db"], ["release", "create", "db"],
                     ["release", "upload", "db"]]


def test_upload_says_no_when_gh_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(publish.shutil, "which", lambda name: None)
    assert publish.upload(tmp_path / "x.gz", "me/repo") is False
