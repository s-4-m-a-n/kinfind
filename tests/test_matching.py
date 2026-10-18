from app.matching import (location_similarity, rank_found, score_match, text_similarity,
                          SHOW_THRESHOLD)

ANCHOR_MAN = {
    "sex": "male", "age_min": 30, "age_max": 40, "height_cm": 170, "hair_color": "black",
    "clothing": "blue checked shirt, black trousers", "date": "2026-09-14",
    "features": [{"kind": "tattoo", "location": "left forearm", "text": "anchor with rope, MAA below"}],
}


def missing(**kw):
    base = {"sex": "male", "age_min": 33, "age_max": 37, "height_cm": 172, "hair_color": "black",
            "date": "2026-09-12",
            "features": [{"kind": "tattoo", "location": "left forearm", "text": "anchor tattoo with word MAA"}]}
    return {**base, **kw}


def test_matching_tattoo_scores_high():
    res = score_match(missing(clothing="blue shirt"), ANCHOR_MAN)
    assert res["score"] >= 70
    assert any("Tattoo on left forearm" in r for r in res["reasons"])


def test_different_tattoo_design_scores_lower():
    same = score_match(missing(), ANCHOR_MAN)["score"]
    other = score_match(missing(features=[{"kind": "tattoo", "location": "left forearm",
                                           "text": "dragon breathing fire"}]), ANCHOR_MAN)["score"]
    assert other < same - 20


def test_mirrored_side_still_partially_matches():
    res = score_match(missing(features=[{"kind": "tattoo", "location": "right forearm",
                                         "text": "anchor MAA"}]), ANCHOR_MAN)
    assert SHOW_THRESHOLD <= res["score"] < score_match(missing(), ANCHOR_MAN)["score"]


def test_sex_mismatch_excluded():
    assert score_match(missing(sex="female"), ANCHOR_MAN) is None


def test_found_before_last_seen_excluded():
    assert score_match(missing(date="2026-09-20"), ANCHOR_MAN) is None


def test_age_far_outside_estimate_excluded():
    assert score_match(missing(age_min=68, age_max=72), ANCHOR_MAN) is None


def test_generic_traits_alone_do_not_look_strong():
    bare = {"sex": "male", "age_min": 33, "age_max": 37, "height_cm": 170}
    assert score_match(bare, ANCHOR_MAN)["score"] < 70


def test_text_similarity_synonyms_and_typos():
    assert text_similarity("red rose", "rose flower red") > 0.6
    assert text_similarity("dragn on back", "dragon") > 0.4
    assert text_similarity("", "anything") is None


def test_location_similarity():
    assert location_similarity("left forearm", "left forearm") == 1.0
    assert location_similarity("left forearm", "left wrist") > location_similarity("left forearm", "left shoulder")
    assert location_similarity("left forearm", "chest") == 0.0
    assert location_similarity("unknown", "chest") == 0.6


def test_rank_found_orders_and_filters():
    other = {**ANCHOR_MAN, "ref": "B", "features": [{"kind": "scar", "location": "chest", "text": "burn"}],
             "clothing": "white kurta"}
    female = {**ANCHOR_MAN, "ref": "C", "sex": "female"}
    ranked = rank_found(missing(), [other, {**ANCHOR_MAN, "ref": "A"}, female])
    refs = [r["record"]["ref"] for r in ranked]
    assert refs[0] == "A" and "C" not in refs
