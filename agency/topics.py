"""Topic-aware clip verdicts: what is the streamer TALKING about?

The insight from the operator's reference clips: loudness/chat spikes only
catch chaos moments. A GTA 6 *tip* ("to auto drive, hold down on the d-pad...")
has zero spikes yet is a great short. So the primary signal is the rolling
transcript: WHAT is being said, classified into clippable topic types.

Calibration set (operator's references, C:\\agency\\references):
    ref A  tip        "to auto drive, hold down on the d-pad, press options,
                       hold circle -> cinematic camera -> go drink a beer"
    ref B  chaos      "get out get out get out ..." (repetition)
    ref C  discovery  "'Detailed' is an understatement ... full-blown airport,
                       full TSA system ... Run, Jason, Run!" (hook + callback)

Verdict dict:
    {score, topic_type, hook, quote, moment}   moment = stream seconds the
    matched phrase starts (the cause anchor; preroll is applied by the caller).
"""
from __future__ import annotations

import re
from collections import Counter

STOP = set(
    "the a an and or to of is it in i you that this on for with was are be so "
    "but not my me we he she they what just like at as if do get it's im i'm "
    "don't can't gonna okay yeah right now here there there's thats that's".split()
)
WORD_RE = re.compile(r"[\w']{2,}")

# (category, compiled pattern, weight per hit)
PATTERNS: list[tuple[str, re.Pattern, float]] = [
    # ---- tip: instructional, how-to, useful -------------------------------
    ("tip", re.compile(r"(?i)\bhold (?:down|on|the)\b"), 1.3),
    ("tip", re.compile(r"(?i)\bpress\b"), 1.1),
    ("tip", re.compile(r"(?i)\b(?:auto|self)[- ]?driv\w*\b"), 1.5),
    ("tip", re.compile(r"(?i)\bhow (?:to|about)\b"), 1.3),
    ("tip", re.compile(r"(?i)\bdid you know\b"), 1.5),
    ("tip", re.compile(r"(?i)\byou can\b"), 1.0),
    ("tip", re.compile(r"(?i)\ball you (?:have|need) to do\b"), 1.5),
    ("tip", re.compile(r"(?i)\bmake sure\b"), 1.1),
    ("tip", re.compile(r"(?i)\b(?:go )?(?:to )?(?:the )?(?:settings|menu|map|store|shop|garage)\b"), 0.8),
    ("tip", re.compile(r"(?i)\b(?:cinematic|photo) (?:camera|mode)\b"), 1.4),
    ("tip", re.compile(r"(?i)\b(?:trick|tip|shortcut|cheat|secret|hidden)\b"), 1.3),
    ("tip", re.compile(r"(?i)\binstead of\b"), 1.0),
    ("tip", re.compile(r"(?i)\bwithout\b"), 0.7),
    ("tip", re.compile(r"(?i)\bit (?:will|would) (?:take|save) you\b"), 1.2),
    # ---- discovery: seeing something new / impressive ----------------------
    ("discovery", re.compile(r"(?i)\blook at (?:this|that)\b"), 1.4),
    ("discovery", re.compile(r"(?i)\bno way\b"), 1.3),
    ("discovery", re.compile(r"(?i)\bis that\b"), 0.9),
    ("discovery", re.compile(r"(?i)\bthere(?:'s| is) (?:a|an|actually)\b"), 1.1),
    ("discovery", re.compile(r"(?i)\bfull(?:y|-blown)?\b"), 0.8),
    ("discovery", re.compile(r"(?i)\bdetail(?:ed|s)?\b"), 1.5),
    ("discovery", re.compile(r"(?i)\bfirst time\b"), 1.4),
    ("discovery", re.compile(r"(?i)\bnever (?:seen|noticed|knew)\b"), 1.4),
    ("discovery", re.compile(r"(?i)\bholy\b"), 1.2),
    ("discovery", re.compile(r"(?i)\boh my\b"), 1.0),
    ("discovery", re.compile(r"(?i)\bwhat the\b"), 1.0),
    ("discovery", re.compile(r"(?i)\b(?:insane|crazy|unreal|nuts|ridiculous)\b"), 1.1),
    ("discovery", re.compile(r"(?i)\brealis\w*\b"), 1.2),
    ("discovery", re.compile(r"(?i)\bi can (?:go|get|open|enter|walk|come|drive|fly)\b"), 1.3),
    ("discovery", re.compile(r"(?i)\bwait,? (?:what|i|you|it)\b"), 1.3),
    # ---- story / emotion ----------------------------------------------------
    ("story", re.compile(r"(?i)\bi can'?t believe\b"), 1.4),
    ("story", re.compile(r"(?i)\blet me tell\b"), 1.2),
    ("story", re.compile(r"(?i)\bthis is (?:why|how|what)\b"), 1.0),
    ("story", re.compile(r"(?i)\bso basical?ly\b"), 0.9),
    ("story", re.compile(r"(?i)\bthe (?:reason|worst|best|craziest)\b"), 1.0),
    ("story", re.compile(r"(?i)\bback (?:in|when|home)\b"), 0.8),
    ("story", re.compile(r"(?i)\bremember (?:when|that)\b"), 1.0),
    ("story", re.compile(r"(?i)\bmy (?:first|friend|mom|dad)\b"), 0.8),
    # ---- chaos: panic / repetition ------------------------------------------
    ("chaos", re.compile(r"(?i)\b(?:get out|run|help|oh no|we'?re dead|we'?re (?:gonna )?die|what (?:is|was) (?:that|happening))\b"), 0.7),
]

NOISE = re.compile(
    r"(?i)giveaway|\braid(?:ing|ed)?\b|\bbrb\b|be right back|ad break|sponsor|"
    r"subscribe|discord\.gg|https?://|promo code|follow me|code \w+"
)

# repetition counting: same content word many times in the window
REP_MIN = 4
CHAOS_PER_REP = 0.55
SHORT_WINDOW_WORDS = 25
SHORT_WINDOW_FACTOR = 0.4


def _sentences(words: list[dict]) -> list[dict]:
    """Group the word stream into sentences on .?! or >1.2s pauses."""
    out: list[dict] = []
    cur: list[dict] = []
    for w in words:
        cur.append(w)
        done = w["w"].rstrip('"\'').endswith((".", "!", "?")) or (
            len(cur) > 1 and w["s"] - cur[-2]["e"] > 1.2
        )
        if done:
            out.append(_finish(cur))
            cur = []
    if cur:
        out.append(_finish(cur))
    return out


def _finish(cur: list[dict]) -> dict:
    text = " ".join(w["w"] for w in cur).strip()
    return {"text": text, "s": cur[0]["s"], "e": cur[-1]["e"], "words": cur}


def score_window(
    words: list[dict],
    *,
    loudness_bonus: float = 0.0,
) -> dict | None:
    """Score one transcript window (list of word dicts with s/e/w).

    Returns the best verdict in the window or None when nothing clippable.
    """
    if not words:
        return None
    sents = _sentences(words)
    if not sents:
        return None

    noise_hits = sum(1 for s in sents if NOISE.search(s["text"]))
    if noise_hits * 2 >= len(sents):
        return None  # mostly ads/raids/subs-plugs

    cat_scores: dict[str, float] = {}
    cat_hits: dict[str, list[dict]] = {}
    for cat, pat, wgt in PATTERNS:
        for s in sents:
            for m in pat.finditer(s["text"]):
                cat_scores[cat] = cat_scores.get(cat, 0.0) + wgt
                # locate the phrase's first word so the moment anchors on it
                p_start = _phrase_word_index(s, m.start())
                cat_hits.setdefault(cat, []).append({
                    "sent": s, "phrase": m.group(0), "wgt": wgt,
                    "s": s["words"][p_start]["s"] if p_start is not None else s["s"],
                    "idx": p_start if p_start is not None else 0,
                })
    # repetition (chaos signature: a content word spammed)
    counts = Counter(
        w["w"].lower().strip(".,!?;:") for w in words
        if w["w"].lower().strip(".,!?;:") not in STOP and len(w["w"]) >= 3
    )
    top_word, top_n = (counts.most_common(1)[0] if counts else ("", 0))
    if top_n >= REP_MIN:
        cat_scores["chaos"] = cat_scores.get("chaos", 0.0) + top_n * CHAOS_PER_REP

    if not cat_scores:
        return None
    topic_type = max(cat_scores, key=cat_scores.get)
    score = sum(cat_scores.values()) + loudness_bonus

    total_words = sum(len(s["words"]) for s in sents)
    if total_words < SHORT_WINDOW_WORDS and not top_n >= REP_MIN:
        # thin context is untrustworthy — unless it's pure shouting, which
        # IS the signal (operator reference B: "get out" x10)
        score *= SHORT_WINDOW_FACTOR

    # anchor: the heaviest matched phrase of the winning category (tie -> earliest)
    best = max(cat_hits[topic_type], key=lambda h: (h["wgt"], -h["s"]))
    hook = _hook(topic_type, best, top_word, top_n)
    return {
        "score": round(score, 2),
        "topic_type": topic_type,
        "hook": hook,
        "quote": best["sent"]["text"][:160],
        "moment": best["s"],
    }


def _phrase_word_index(sent: dict, char_start: int) -> int | None:
    """Index of the word whose text contains char position `char_start`."""
    pos = 0
    for i, w in enumerate(sent["words"]):
        nxt = pos + len(w["w"]) + 1  # +1 for the joining space
        if char_start < nxt:
            return i
        pos = nxt
    return None


def _hook(topic_type: str, hit: dict, top_word: str, top_n: int) -> str:
    """Short burned-in banner (<= ~58 chars) starting at the matched phrase."""
    if topic_type == "chaos" and top_n >= REP_MIN:
        # prefer the spammed bigram ("get out") over the lone token ("out")
        seq = [w["w"].lower().strip(".,!?;:") for w in hit["sent"]["words"]]
        bigrams = Counter(zip(seq, seq[1:]))
        label = " ".join(bigrams.most_common(1)[0][0]) if bigrams else top_word
        return f"{label.upper()}! x{top_n} - GTA 6 CHAOS"[:58]
    words = hit["sent"]["words"][hit["idx"]:]
    text = " ".join(w["w"] for w in words[:11])
    text = re.sub(r"\s+", " ", text).strip().strip('"\'. ')
    text = re.sub(r"(?i)^to (?=auto|drive|do|get|use|enable|unlock)", "", text)
    if topic_type == "tip" and not re.match(r"(?i)^(tip|how|press|hold|use|did)", text):
        text = f"Tip: {text}"
    text = text[:58].rsplit(" ", 1)[0] if len(text) > 58 else text
    return text.rstrip(".,;:") or "GTA 6 moment"


def dedup_fingerprint(words: list[dict], n: int = 5) -> frozenset:
    """Top-n content words of a window; near-identical windows dedupe away."""
    counts = Counter(
        w["w"].lower().strip(".,!?;:") for w in words
        if w["w"].lower().strip(".,!?;:") not in STOP and len(w["w"]) >= 3
    )
    return frozenset(w for w, _ in counts.most_common(n))


def similar(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
