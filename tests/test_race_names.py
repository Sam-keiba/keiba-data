"""付記の無いレース名（race_names）。"""

from __future__ import annotations

from keiba_data import race_names as rn


def test_plain_scrape_name():
    assert rn.plain_scrape_name("第57回スプリンターズS") == "スプリンターズS"
    assert rn.plain_scrape_name("アクアマリンS(3勝)") == "アクアマリンS"
    assert rn.plain_scrape_name("関門橋S(OP)") == "関門橋S"
    assert rn.plain_scrape_name("第170回天皇賞(秋)") == "天皇賞(秋)"      # (秋) は名前の一部
    assert rn.plain_scrape_name("シリウスステークス") == "シリウスS"
    assert rn.plain_scrape_name("3歳以上1勝クラス") == "3歳以上1勝クラス"
    assert rn.plain_scrape_name(None) is None


def test_strip_target_suffixes():
    assert rn.strip_target_suffixes("皐月賞G1") == "皐月賞"
    assert rn.strip_target_suffixes("福島記念HG3") == "福島記念H"
    assert rn.strip_target_suffixes("福島記念HG3", handicap=True) == "福島記念"
    assert rn.strip_target_suffixes("アクアH1600", handicap=True) == "アクア"
    assert rn.strip_target_suffixes("500万下・牝*") == "500万下"
    assert rn.strip_target_suffixes("ジュライ(L)") == "ジュライ"
    assert rn.strip_target_suffixes("阪神ジャJG3") == "阪神ジャ"


def test_generic_races_follow_netkeiba_wording():
    assert rn.generic_name("500万下", "3歳以上", "500万下 牝", "turf") == "3歳以上500万下"
    assert rn.generic_name("障害・オープ", "4歳以上", "オープン", "jump") == "障害4歳以上OP"
    assert rn.generic_name("未勝利", "2歳", "未勝利", "dirt") == "2歳未勝利"
    assert rn.generic_name("スワンS", "3歳以上", "オープン", "turf") is None


def test_match_modern_drops_sponsor_and_needs_a_single_hit():
    assert rn.match_modern("スワンS", ["MBS賞スワンS"]) == "スワンS"
    assert rn.match_modern("シンザン", ["日刊スポシンザン記念"]) == "シンザン記念"
    assert rn.match_modern("天皇賞秋", ["天皇賞(秋)"]) == "天皇賞(秋)"
    assert rn.match_modern("京成杯", ["京成杯", "京成杯オータムH"]) == "京成杯"     # 同じ名前を優先
    assert rn.match_modern("スプリン", ["スプリンターズS", "スプリングS"]) is None   # 2つ当たれば採らない


def test_target_plain_name_stages_and_overrides():
    pool = rn.CandidatePool([("06", "turf", "GI", "スプリンターズS"), ("06", "turf", "GII", "スプリングS"),
                             ("09", "dirt", "GIII", "シリウスS"), ("05", "turf", None, "紫野特別")])
    kwargs = dict(handicap=False, age_condition="3歳以上", class_condition="オープン")
    # 同じ競馬場・馬場・格なら1つに決まる
    assert rn.target_plain_name("スプリンG1", surface="turf", stages=pool.stages("06", "turf", "GI"),
                                **kwargs) == ("スプリンターズS", "matched")
    # 開催替え（別の競馬場）でも、同じ馬場・格まで広げて引ける
    assert rn.target_plain_name("シリウスG3", surface="dirt", stages=pool.stages("08", "dirt", "GIII"),
                                **kwargs) == ("シリウスS", "matched")
    # 切り詰められた「特」は「特別」に補う
    assert rn.target_plain_name("紫野特H・2勝", surface="turf", stages=pool.stages("08", "turf", None),
                                **kwargs) == ("紫野特別", "matched")
    assert rn.target_plain_name("WASJ第41000", surface="turf", stages=[], **kwargs) == (
        "ワールドオールスタージョッキーズ第4戦", "override")
    assert rn.target_plain_name("むかしのレH", surface="turf", stages=[], **kwargs) == ("むかしのレH", "stripped")
