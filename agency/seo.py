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
def captions_for(meta: dict, cfg: dict | None = None) -> dict[str, dict]:
    """Per-platform SEO metadata for a package. Template engine."""
    cfg = cfg or load_config()
    platforms = list(meta.get("posts", {}).keys()) or cfg["platforms"]
    hook = meta.get("hook") or "You have to see this"
    credit = credit_line(meta)
    source_title = meta.get("source", {}).get("title", "")
    tags = cfg["seo"]["hashtags"]
    bio = cfg["page"].get("bio_credit", "")

    value_line = ""
    if source_title:
        value_line = f"From: {source_title[:100]}"

    out: dict[str, dict] = {}
    for p in platforms:
        if p == "youtube":
            title = sanitize(hook)[: MAXLEN["youtube_title"]]
            desc_parts = [hook, "", credit, "", bio, " ".join(tags.get("youtube", []))]
            out[p] = {
                "title": title,
                "description": "\n".join(x for x in desc_parts if x is not None)[: MAXLEN["tiktok"]],
                "tags": [t.lstrip("#") for t in tags.get("youtube", [])][:12],
            }
        elif p == "tiktok":
            hh = " ".join(tags.get("tiktok", []))
            out[p] = {"caption": sanitize(f"{hook} {hh}")[: MAXLEN["tiktok"]]}
        elif p == "facebook":
            hh = " ".join(tags.get("facebook", [])[:6])
            body = "\n".join(x for x in [hook, value_line, credit] if x)
            out[p] = {"caption": f"{body}\n\n{hh}"[: MAXLEN["tiktok"]]}
        elif p == "instagram":
            hh = " ".join(tags.get("instagram", [])[:5])  # max 5 hashtags
            body = "\n".join(x for x in [hook, value_line, credit] if x)
            out[p] = {"caption": f"{body}\n\n{hh}"[: MAXLEN["instagram"]]}
        elif p == "x":
            text = "\n".join(x for x in [hook, credit] if x)
            out[p] = {"text": text[: MAXLEN["x"]]}
    return out


# ---------------------------------------------------------------- LLM refine
_LLM_PROMPT = """You are a short-form video SEO strategist for a movie clip & edit fan page (RGClipzah).
Improve this post's metadata. Rules:
- Keep it factual to the source material; never invent movie facts or spoilers in the title.
- No clickbait lies, no words like: {banned}.
- YouTube title <= 95 chars. Facebook caption <= 2200 chars. Instagram caption <= 2200 chars with at most 5 hashtags. X text <= 280 chars, no hashtags.
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
