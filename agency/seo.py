"""SEO strategist: hooks, titles, captions and hashtags per platform.

Two brains:
  1. Template engine (always available, deterministic, playbook-derived)
  2. Optional LLM refinement via any OpenAI-compatible endpoint
     (seo.llm_api_base + LLM_API_KEY + LLM_MODEL in .env). Every LLM output
     passes the same sanitization as templates, and any failure silently
     falls back to templates.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re

import requests

from .config import ROOT, env, load_config
from .logutil import log

# Same banned-content list as the repo's check.py — nothing we generate may
# contain these, ever.
BANNED = [
    "leak", "leaked", "play early", "early access download", "free download",
    "crack", "piracy", "pirated", "mod menu", "money glitch", "afk bot",
    "full movie free", "free full movie", "watch free", "123movies",
    "putlocker", "fmovies", "download movie",
]

MAXLEN = {"youtube_title": 95, "tiktok": 2200, "instagram": 2200, "x": 280}


# ---------------------------------------------------------------- sanitize
def sanitize(text: str, *, credit: str = "", pillar: str = "clip") -> str:
    """Strip banned terms, trim whitespace. Does NOT add credit (callers do)."""
    low = text.lower()
    for word in BANNED:
        if word in low:
            # replace banned phrase with a safe placeholder
            pattern = re.compile(re.escape(word), re.IGNORECASE)
            text = pattern.sub("[gotcha]", text)
            low = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def credit_line(meta: dict) -> str:
    ch = meta.get("credit") or meta.get("source", {}).get("channel") or ""
    return f"🎥 {ch}" if ch else ""


def _subject_from(candidate: dict) -> str:
    """Short subject phrase from the candidate transcript or score reasons."""
    text = (candidate.get("text") or "").strip()
    if text:
        # first sentence-ish fragment, max ~7 words, strip filler
        frag = re.split(r"[.!?]", text)[0]
        stop = {"um", "uh", "like", "you", "know", "i", "we", "so", "and", "the", "yeah", "bro"}
        words = [w for w in frag.split() if w.lower().strip("',.\"") not in stop]
        frag = " ".join(words[:7]).strip()
        if len(frag) >= 8:
            return frag
    why = candidate.get("why") or []
    if why:
        return str(why[0]).strip().rstrip(".")[:60]
    return "this moment"


# ---------------------------------------------------------------- hooks
_HOOK_TEMPLATES = [
    "Wait for the {subject}...",
    "This {subject} hits different",
    "Nobody expected this {subject}",
    "POV: {subject}",
    "The {subject} scene everyone replayed",
    "Watch {subject} unfold",
    "This edit goes too hard",
    "Part 2 if you want more {subject}",
]


def generate_hook(candidate: dict, info: dict, *, pillar: str = "clip") -> str:
    """Deterministic-but-varied hook: same candidate keeps its hook, days vary."""
    channel = (info.get("channel") or info.get("uploader") or "This streamer")
    channel_short = channel.split()[0][:18]
    subject = _subject_from(candidate)
    options = [t.format(subject=subject, channel_short=channel_short)
               for t in _HOOK_TEMPLATES]
    seed = hashlib.sha1(f"{candidate.get('n')}|{datetime.date.today()}|{pillar}".encode()).hexdigest()
    hook = options[int(seed, 16) % len(options)]
    return sanitize(hook)


# ---------------------------------------------------------------- captions
# Strategist engine: hook first (emoji), the original poster's description as
# the body when there is one, credit + CTA, then a platform-tuned hashtag set
# topped with the source's own topical tags. Everything deterministic per
# package (seeded picks) so reposts never look copy-pasted.

_JUNK_TAGS = {
    "fyp", "fypシ", "foryou", "foryoupage", "4u", "4upage", "xyzbca", "viral",
    "trending", "tiktok", "viralvideo", "blowthisup", "growmyaccount",
    "recommend", "recommendme", "shorts", "reels", "reelsinstagram",
    "explorepage", "explore", "parati", "movie", "edit", "edits",
}

_TAG_COUNTS = {"youtube": 5, "instagram": 8, "facebook": 6, "tiktok": 5, "x": 0}

# Tags every post carries - the discovery tags that actually reach the right
# audience. The rest of the budget is a rotation pool (rotates per package so
# the feed never looks copy-pasted).
_CORE_TAGS = {
    "youtube": ["#shorts", "#movie", "#movieclips"],
    "instagram": ["#movie", "#movieclips", "#filmedit"],
    "facebook": ["#movie", "#movieclips", "#movieedits"],
    "tiktok": ["#movie", "#movieclips", "#filmedit"],
    "x": [],
}

_DEFAULT_POOLS = {
    "youtube": ["#movieedit", "#film", "#cinema", "#moviemoments", "#scenes",
                "#shortsfeed", "#filmclips"],
    "instagram": ["#movieedit", "#film", "#cinema", "#moviescenes", "#moviemoments",
                  "#edits", "#filmclips", "#scenes"],
    "facebook": ["#filmedit", "#film", "#cinema", "#moviemoments", "#scenes",
                 "#reels", "#filmclips"],
    "tiktok": ["#movieedit", "#filmtok", "#movietok", "#cinematok", "#scenes",
               "#filmclips", "#fyp"],
    "x": [],
}

_HOOK_EMOJI = ["🎬", "🔥", "🍿", "😭🔥", "🤯", "✨", "😤🔥", "💯"]

_HOOK_TEMPLATES = [
    "This scene lives rent-free in my head {e}",
    "Name a better scene — I'll wait {e}",
    "They don't make scenes like this anymore {e}",
    "POV: you've replayed this 10 times already {e}",
    "Cinema peaked right here {e}",
    "Wait for it {e}",
    "The scene everyone keeps replaying {e}",
    "This went way too hard {e}",
    "Tell me this isn't peak cinema {e}",
    "Still not over this scene {e}",
    "This scene deserves way more love {e}",
    "No notes. Just cinema {e}",
    "The way this hits every single time {e}",
    "Certified rewatch material {e}",
]


def _seed(meta: dict) -> str:
    return str(meta.get("id") or meta.get("hook") or "rgclipzah")


def _pick(seq: list, seed: str):
    if not seq:
        return ""
    return seq[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(seq)]


def _strip_hashtags(text: str) -> str:
    return re.sub(r"(?<![\w#])#[\w\d_]+", "", text or "")


def _clean_source_title(meta: dict) -> str:
    """The poster's own title without hashtag noise — '' when it was only hashtags."""
    raw = str(meta.get("source", {}).get("title", "") or "")
    t = re.sub(r"\s+", " ", _strip_hashtags(raw)).strip(" -–—|.,")
    if t.lower().rstrip("!.") in ("", "movie", "video", "clip", "shorts"):
        return ""
    return t


def _clean_source_desc(meta: dict, limit: int = 400) -> str:
    """The original poster's description, cleaned: no links, no hashtag-only
    lines (hashtags get re-curated), banned words out. '' when there was no
    real text (hashtag-only descriptions are the TikTok norm)."""
    d = str(meta.get("source", {}).get("description", "") or "")
    if not d:
        return ""
    d = re.sub(r"https?://\S+|www\.\S+", "", d)
    lines = []
    for ln in d.splitlines():
        ln = _strip_hashtags(ln).strip()
        # a stripped link leaves a dangling label behind ("link:", "source:")
        ln = re.sub(r"(?i)\b(?:link|source|src|watch|full\s+video|credits?)\s*:\s*$", "", ln)
        ln = re.sub(r"[\s,;:.\-–—]+$", "", ln).strip()
        if ln:
            lines.append(ln)
    return sanitize(" ".join(lines))[:limit].strip()


def _is_title_like(text: str) -> bool:
    """True when the poster's text reads like a title, not like a caption.

    TikTok sets the video's title to the whole caption, so a long run-on
    sentence has to be treated as body copy - it never becomes the hook."""
    words = text.split()
    if len(text) > 72 or len(words) > 12:
        return False
    if len(words) > 6 and text.endswith((".", "!", "?")):
        return False
    if len(words) > 8 and text.endswith(("...", "\u2026")):
        return False
    return True


def _orig_hashtags(meta: dict, limit: int = 3, exclude: frozenset = frozenset()) -> list[str]:
    """The source's own topical hashtags (junk tags dropped, ours not repeated)."""
    src = meta.get("source", {})
    blob = f"{src.get('title', '')} {src.get('description', '')}"
    tags: list[str] = []
    for m in re.finditer(r"#([\w\d_]+)", blob):
        t = m.group(1).lower().strip("_")
        if not t or t in _JUNK_TAGS or f"#{t}" in exclude or f"#{t}" in tags:
            continue
        tags.append(f"#{t}")
        if len(tags) >= limit:
            break
    return tags


def _hook_for(meta: dict) -> str:
    """The poster's proven title is the best hook when it reads like a title;
    otherwise a seeded strategist hook so '#movie' never ships again and a
    run-on caption never becomes the title."""
    title = _clean_source_title(meta)
    emoji = _pick(_HOOK_EMOJI, _seed(meta) + "|e")
    if title and _is_title_like(title):
        t = title if len(title) <= 70 else title[:70].rsplit(" ", 1)[0]
        return sanitize(f"{t} {emoji}")
    return sanitize(_pick(_HOOK_TEMPLATES, _seed(meta) + "|h").format(e=emoji))


def _cta(cfg: dict) -> str:
    handle = (cfg.get("page", {}) or {}).get("handle") or "@rgcipzah"
    return f"Follow {handle} for daily movie clips 🔥"


def _stem(tag: str) -> str:
    """Tag identity without plural/format noise: #filmedit == #filmedits."""
    return tag.lstrip("#").lower().strip("_").rstrip("s")


def _dedupe_stems(tags: list[str]) -> list[str]:
    """Drop near-duplicate tags (#movieedit vs #movieedits, #film vs #films)."""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        stem = _stem(t)
        if not stem or stem in seen:
            continue
        seen.add(stem)
        out.append(t)
    return out


def _hashtag_list(platform: str, cfg: dict, meta: dict) -> list[str]:
    """Core discovery tags + a seeded rotation of the pool + the source's own
    topical tags. Near-duplicates are dropped."""
    n = _TAG_COUNTS.get(platform, 5)
    if n <= 0:
        return []
    pool = list(dict.fromkeys(cfg["seo"]["hashtags"].get(platform) or []))
    if not pool:
        pool = list(_DEFAULT_POOLS.get(platform) or [])
    if not pool:
        return []
    core = [t for t in _CORE_TAGS.get(platform, []) if t in pool] or list(_CORE_TAGS.get(platform, []))
    rest = [t for t in pool if t not in core]
    seed = _seed(meta)
    if rest:  # deterministic rotation so every post gets a different mix
        off = int(hashlib.sha1((seed + "|tags").encode()).hexdigest(), 16) % len(rest)
        rest = rest[off:] + rest[:off]
    ours = _dedupe_stems(core[:n] + rest[: max(0, n - len(core))])[:n]
    topical = _orig_hashtags(meta, limit=3, exclude=frozenset(t.lower() for t in ours))
    return _dedupe_stems(topical + ours)


def _paras(*parts: str) -> str:
    """Join non-empty caption blocks with a blank line between them."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def captions_for(meta: dict, cfg: dict | None = None) -> dict[str, dict]:
    """Per-platform SEO metadata for a package. Strategist template engine."""
    cfg = cfg or load_config()
    platforms = list(meta.get("posts", {}).keys()) or cfg["platforms"]
    hook = _hook_for(meta)
    credit = credit_line(meta)
    orig = _clean_source_desc(meta)
    bio = cfg["page"].get("bio_credit", "")
    cta = _cta(cfg)

    # TikTok puts the caption in BOTH title and description - never print the
    # same sentence twice in one post.
    title_text = _clean_source_title(meta)
    if orig and title_text and _is_title_like(title_text) \
            and orig.lower().startswith(title_text.lower()[:40]):
        orig = ""

    out: dict[str, dict] = {}
    for p in platforms:
        tags = _hashtag_list(p, cfg, meta)
        hh = " ".join(tags)
        if p == "youtube":
            desc = _paras(hook, orig, credit, cta, bio)
            if hh:
                desc += "\n\n" + hh
            out[p] = {
                "title": hook[: MAXLEN["youtube_title"]],
                "description": desc[:900],
                "tags": [t.lstrip("#") for t in tags][:12],
            }
        elif p == "tiktok":
            out[p] = {"caption": sanitize(" ".join(x for x in [hook, orig, hh] if x))[: MAXLEN["tiktok"]]}
        elif p in ("facebook", "instagram"):
            body = _paras(hook, orig, credit, cta)
            if hh:
                body += "\n\n" + hh
            out[p] = {"caption": body[: MAXLEN.get(p, 2200)]}
        elif p == "x":
            out[p] = {"text": _paras(hook, credit)[: MAXLEN["x"]]}
    return out


# ---------------------------------------------------------------- LLM refine
_LLM_PROMPT = """You are a short-form video SEO strategist for a movie clip & edit fan page (RGCIPZAH).
Improve this post's metadata. Rules:
- Emojis are encouraged: 2-6 tasteful emojis per caption. Hook on the FIRST line, hashtags LAST.
- Keep it factual to the source material; never invent movie facts or spoilers in the title.
- No clickbait lies, no words like: {banned}.
- YouTube title <= 95 chars. Facebook caption <= 2200 chars. Instagram caption <= 2200 chars with at most 8 hashtags. X text <= 280 chars, no hashtags.
- Always keep creator credit exactly: "{credit}"
- Vertical clip audience: movie fans 13-34. Hook must earn the first second.
Return ONLY minified JSON: {{"youtube":{{"title":"","description":""}},"tiktok":{{"caption":""}},"instagram":{{"caption":""}},"x":{{"text":""}}}}

Package context:
{context}
"""


def _llm_refine(meta: dict, captions: dict, cfg: dict) -> dict | None:
    base = cfg["seo"].get("llm_api_base") or env("LLM_API_BASE")
    key = env("LLM_API_KEY")
    model = cfg["seo"].get("llm_model") or env("LLM_MODEL")
    if not (base and key and model):
        return None
    context = json.dumps({
        "hook": meta.get("hook"),
        "source_title": meta.get("source", {}).get("title"),
        "source_channel": meta.get("source", {}).get("channel"),
        "duration_sec": meta.get("duration_sec"),
        "pillar": meta.get("pillar"),
        "current_captions": captions,
    }, ensure_ascii=False)
    try:
        resp = requests.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": _LLM_PROMPT.format(
                    banned=", ".join(BANNED), credit=credit_line(meta) or "(none)", context=context)}],
                "temperature": 0.8, "max_tokens": 800,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return None
        refined = json.loads(m.group(0))
    except Exception as exc:
        log(f"seo: LLM refine failed ({exc!r}); using templates", level="error")
        return None

    # validate + sanitize LLM output; fall back per-platform to template value
    safe = {}
    for p, cap in captions.items():
        got = refined.get(p) if isinstance(refined, dict) else None
        if not isinstance(got, dict):
            safe[p] = cap
            continue
        merged = {**cap}
        for k, v in got.items():
            if isinstance(v, str) and v.strip():
                merged[k] = sanitize(v)
        # enforce lengths
        if p == "youtube" and merged.get("title"):
            merged["title"] = merged["title"][:MAXLEN["youtube_title"]]
        if p == "x" and merged.get("text"):
            merged["text"] = merged["text"][:MAXLEN["x"]]
        if merged.get("credit") is False or "credit" not in merged:
            pass
        safe[p] = merged
    return safe


def fill_captions(meta: dict, *, use_llm: bool = True, cfg: dict | None = None) -> dict[str, dict]:
    """Full caption generation: templates, optionally refined by LLM."""
    cfg = cfg or load_config()
    captions = captions_for(meta, cfg)
    if use_llm:
        refined = _llm_refine(meta, captions, cfg)
        if refined:
            captions = refined
    # final guard: credit must survive in clip captions
    credit = credit_line(meta)
    if credit:
        for p, cap in captions.items():
            joined = " ".join(str(v) for v in cap.values())
            if p in ("tiktok", "instagram", "facebook") and credit.split()[-1] not in joined:
                cap["caption"] = f"{cap.get('caption', '')}\n{credit}"[:MAXLEN.get(p, 2200)]
    return captions
