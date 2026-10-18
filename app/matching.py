"""Description-based matching between missing-person reports and found records.

Pure functions only — no database access — so the scoring is easy to test and tune.

A record is a dict with any of these keys (missing values are simply skipped):
    sex          "male" | "female" | "unknown"
    age_min/max  int years (missing reports give an age, stored as a +/-2 band)
    height_cm    int
    hair_color   str
    clothing     free text
    description  free text
    features     [{"kind": "tattoo", "location": "left forearm", "text": "anchor with rope"}]
    date         ISO date — last seen (missing) / found (found)
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# Points each component is worth when both sides have data for it.
WEIGHTS = {
    "features": 50,  # tattoos, scars, birthmarks... the strongest evidence
    "clothing": 12,
    "age": 10,
    "description": 10,
    "height": 8,
    "sex": 6,
    "hair": 4,
}
# A record that only agrees on a couple of generic traits must not look like a
# strong match, so the score is divided by at least this many points.
MIN_EVIDENCE = 60
SHOW_THRESHOLD = 35  # matches below this % are not shown

FEATURE_KINDS = ["tattoo", "scar", "birthmark", "mole", "piercing", "amputation",
                 "dental", "surgical implant", "other"]
SIMILAR_KINDS = {frozenset({"birthmark", "mole"}): 0.6}

# Body parts grouped by region, ordered so neighbours sit next to each other.
REGIONS = {
    "head": ["head", "face", "neck"],
    "torso": ["chest", "abdomen", "back"],
    "arm": ["shoulder", "upper arm", "forearm", "wrist", "hand"],
    "leg": ["thigh", "knee", "lower leg", "ankle", "foot"],
}
SIDED = {"arm", "leg"}
BODY_LOCATIONS = (
    REGIONS["head"] + REGIONS["torso"]
    + [f"{side} {part}" for region in ("arm", "leg") for side in ("left", "right")
       for part in REGIONS[region]]
    + ["unknown"]
)

HAIR_COLORS = ["black", "dark brown", "brown", "blonde", "red", "grey", "white", "bald", "dyed"]
SIMILAR_HAIR = {frozenset({"black", "dark brown"}), frozenset({"dark brown", "brown"}),
                frozenset({"grey", "white"})}

STOPWORDS = set("""a an and the of on in with at to is was has have had her his their
    its it near small large big some very wearing wore worn around about like one
    tattoo tattooed word words written says saying below above under underneath over beside
    side part body""".split())
# Expand specific words with a broader term so "rose" can meet "flower".
SYNONYMS = {
    "rose": "flower", "lotus": "flower", "lily": "flower", "tulip": "flower",
    "snake": "serpent", "cobra": "serpent", "serpent": "snake",
    "lion": "cat", "tiger": "cat",
    "eagle": "bird", "hawk": "bird", "dove": "bird", "phoenix": "bird",
    "shirt": "top", "tshirt": "top", "t-shirt": "top", "kurta": "top", "blouse": "top",
    "jeans": "trousers", "pants": "trousers", "trouser": "trousers",
    "necklace": "chain", "pendant": "chain", "bangle": "bracelet",
    "grey": "gray", "gray": "grey", "navy": "blue",
    "script": "text", "letters": "text", "lettering": "text", "writing": "text",
}


# --- text -------------------------------------------------------------------

def tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    out: set[str] = set()
    for word in re.findall(r"[\w'-]+", text.lower()):
        word = word.strip("'-")
        if len(word) < 2 or word in STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.add(word)
        if word in SYNONYMS:
            out.add(SYNONYMS[word])
    return out


def _best_token_match(tok: str, others: set[str]) -> float:
    if tok in others:
        return 1.0
    best = max((SequenceMatcher(None, tok, o).ratio() for o in others), default=0.0)
    return best if best >= 0.8 else 0.0  # tolerate typos like "dragn" / "dragon"


def text_similarity(a: str | None, b: str | None) -> float | None:
    """Symmetric fuzzy token overlap in [0, 1]; None when either side is empty."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return None
    forward = sum(_best_token_match(t, tb) for t in ta) / len(ta)
    backward = sum(_best_token_match(t, ta) for t in tb) / len(tb)
    # Two people rarely describe the same thing with the same detail, so lean on
    # the better-covered direction ("anchor MAA" vs "anchor with rope, MAA below").
    return 0.65 * max(forward, backward) + 0.35 * min(forward, backward)


# --- distinctive features ----------------------------------------------------

def _parse_location(loc: str | None) -> tuple[str | None, str | None, str | None]:
    """'left forearm' -> ('left', 'arm', 'forearm')."""
    if not loc or loc == "unknown":
        return None, None, None
    side, part = None, loc
    if loc.startswith(("left ", "right ")):
        side, part = loc.split(" ", 1)
    for region, parts in REGIONS.items():
        if part in parts:
            return side, region, part
    return side, None, part


def location_similarity(a: str | None, b: str | None) -> float:
    sa, ra, pa = _parse_location(a)
    sb, rb, pb = _parse_location(b)
    if ra is None or rb is None:
        return 0.6  # location not recorded on one side: neither confirms nor rules out
    if ra != rb:
        return 0.0
    if pa == pb:
        # Left/right is often mixed up when describing someone else's body.
        return 1.0 if sa == sb else 0.35
    gap = abs(REGIONS[ra].index(pa) - REGIONS[rb].index(pb))
    same_side = sa == sb or ra not in SIDED
    if gap == 1:
        return 0.7 if same_side else 0.25
    return 0.4 if same_side else 0.1


def kind_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SIMILAR_KINDS.get(frozenset({a, b}), 0.0)


def feature_similarity(fa: dict, fb: dict) -> float:
    kind = kind_similarity(fa.get("kind", "other"), fb.get("kind", "other"))
    if not kind:
        return 0.0
    loc = location_similarity(fa.get("location"), fb.get("location"))
    text = text_similarity(fa.get("text"), fb.get("text"))
    # Same kind + same place with an unrelated description ("anchor" vs "rose") stays low.
    text_factor = 0.5 if text is None else 0.2 + 0.8 * text
    return kind * loc * text_factor


def features_similarity(missing: list[dict], found: list[dict]):
    """Return (score, [(missing_feature, best_score)]) or (None, []) if not comparable."""
    if not missing or not found:
        return None, []
    pairs = [(f, max(feature_similarity(f, g) for g in found)) for f in missing]
    best = [s for _, s in pairs]
    # One clearly matching tattoo matters more than an average dragged down by
    # features that may simply not have been visible / recorded.
    score = 0.65 * max(best) + 0.35 * (sum(best) / len(best))
    return score, pairs


# --- physical traits ---------------------------------------------------------

def _range_gap(a_min, a_max, b_min, b_max) -> float:
    if a_max < b_min:
        return b_min - a_max
    if b_max < a_min:
        return a_min - b_max
    return 0.0


def age_similarity(m: dict, f: dict) -> float | None:
    if m.get("age_min") is None or f.get("age_min") is None:
        return None
    gap = _range_gap(m["age_min"], m.get("age_max", m["age_min"]),
                     f["age_min"], f.get("age_max", f["age_min"]))
    return max(0.0, 1 - gap / 10)


def height_similarity(m: dict, f: dict) -> float | None:
    if not m.get("height_cm") or not f.get("height_cm"):
        return None
    diff = abs(m["height_cm"] - f["height_cm"])
    return 1.0 if diff <= 5 else max(0.0, 1 - (diff - 5) / 15)


def hair_similarity(m: dict, f: dict) -> float | None:
    a, b = m.get("hair_color"), f.get("hair_color")
    if not a or not b:
        return None
    if a == b:
        return 1.0
    return 0.6 if frozenset({a, b}) in SIMILAR_HAIR else 0.0


def _known_sex(rec: dict) -> str | None:
    s = rec.get("sex")
    return s if s in ("male", "female") else None


# --- overall -----------------------------------------------------------------

def is_excluded(missing: dict, found: dict) -> bool:
    """Hard rules that make a match impossible."""
    sm, sf = _known_sex(missing), _known_sex(found)
    if sm and sf and sm != sf:
        return True
    dm, df = missing.get("date"), found.get("date")
    if dm and df and df < dm:  # found before they went missing
        return True
    age = age_similarity(missing, found)
    return age is not None and age == 0.0  # more than 10 years outside the estimate


def score_match(missing: dict, found: dict) -> dict | None:
    """Score a missing report against a found record.

    Returns {"score": 0-100, "reasons": [...]} or None when excluded.
    """
    if is_excluded(missing, found):
        return None

    feat_score, feat_pairs = features_similarity(missing.get("features") or [],
                                                 found.get("features") or [])
    sims = {
        "features": feat_score,
        "clothing": text_similarity(missing.get("clothing"), found.get("clothing")),
        "description": text_similarity(missing.get("description"), found.get("description")),
        "age": age_similarity(missing, found),
        "height": height_similarity(missing, found),
        "hair": hair_similarity(missing, found),
        "sex": 1.0 if _known_sex(missing) and _known_sex(found) else None,
    }

    earned = possible = 0.0
    for key, sim in sims.items():
        if sim is None:
            continue
        earned += WEIGHTS[key] * sim
        possible += WEIGHTS[key]
    score = round(100 * earned / max(possible, MIN_EVIDENCE))

    reasons = []
    for feat, s in sorted(feat_pairs, key=lambda p: -p[1]):
        if s >= 0.25:
            where = f" on {feat['location']}" if feat.get("location") not in (None, "", "unknown") else ""
            strength = "strong" if s >= 0.7 else "partial"
            reasons.append(f"{feat['kind'].capitalize()}{where} — {strength} match")
    labels = {"clothing": "Clothing / belongings", "description": "General description",
              "age": "Age", "height": "Height", "hair": "Hair colour", "sex": "Sex"}
    for key, label in labels.items():
        sim = sims[key]
        if sim is not None and sim >= 0.5:
            reasons.append(f"{label} — {'consistent' if sim >= 0.8 else 'similar'}")

    return {"score": score, "reasons": reasons}


def _ranked(pairs) -> list[dict]:
    results = []
    for record, res in pairs:
        if res and res["score"] >= SHOW_THRESHOLD:
            results.append({"record": record, **res})
    return sorted(results, key=lambda r: -r["score"])


def rank_found(missing: dict, found_records: list[dict]) -> list[dict]:
    """Found records likely to be this missing person, best first, above threshold."""
    return _ranked((f, score_match(missing, f)) for f in found_records)


def rank_missing(found: dict, missing_reports: list[dict]) -> list[dict]:
    """Missing reports likely to describe this found person, best first."""
    return _ranked((m, score_match(m, found)) for m in missing_reports)
