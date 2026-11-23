"""Agency configuration: config.json + .env credentials loader.

All paths resolve from the agency root (folder containing this package).
Credentials live in .env (git-ignored) and are read-only; nothing is ever
sent anywhere except the platform APIs themselves.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # clip-agency/

DEFAULT_CONFIG: dict = {
    "page": {
        "name": "",
        "handle": "",
        "niche": "tips and hidden spots",
        "timezone": "local",
        "bio_credit": "Fan page. Not affiliated with Rockstar Games.",
    },
    "platforms": ["youtube", "tiktok", "instagram", "x"],
    "autonomy": {
        # Approval gate: nothing uploads until approve.py flips the package.
        "approval_gate": True,
    },
    "schedule": {
        "daily_run": "08:00",
        "monitor_runs": ["12:00", "18:00", "21:30"],
        "vertical_posts_per_day": 2,
        "x_posts_per_day": 4,
    },
    "research": {
        "query": "gta 6",
        "trend_hours": 24,
        "trend_limit": 30,
        "subreddits": "GTA6,GTA,GrandTheftAutoV",
        "max_moments": 8,
        "max_source_minutes": 90,
    },
    "production": {
        "layout": "blur",
        "captions": "word",
        "min_sec": 7,
        "max_sec": 30,
        "loudnorm_lufs": -14,
        "allow_profanity": False,
    },
    "seo": {
        # Optional LLM copywriting. Leave null to use template engine only.
        "llm_api_base": None,
        "llm_model": None,
        "max_title_len": 90,
        "max_desc_len": 900,
        "hashtags": {
            "youtube": ["#GTA6", "#GTAVI", "#Shorts", "#gaming"],
            "tiktok": ["#gta6", "#gtavi", "#gaming", "#fyp"],
            "instagram": ["#gta6", "#gtavi", "#gaming", "#rockstargames"],
            "x": [],
        },
    },
    "paths": {
        "repo": "",  # gtavi-claudecode checkout; auto-detected if empty
        "outbox": "outbox",
        "media_edits": "media/edits",
        "jobs": "jobs",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg_path = ROOT / "config.json"
    cfg = DEFAULT_CONFIG
    if cfg_path.exists():
        cfg = _merge(DEFAULT_CONFIG, json.loads(cfg_path.read_text(encoding="utf-8")))
    # local timezone default
    if cfg["page"]["timezone"] in ("", "local"):
        import datetime

        local = datetime.datetime.now().astimezone().tzname() or "local"
        cfg["page"]["timezone"] = local
    return cfg


def save_config(cfg: dict) -> Path:
    cfg_path = ROOT / "config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg_path


def repo_dir() -> Path:
    """Location of the gtavi-claudecode checkout (its scripts/ folder)."""
    cfg = load_config()
    hint = cfg["paths"].get("repo") or os.environ.get("GTAVI_REPO_DIR")
    candidates = [Path(hint)] if hint else []
    candidates.append(ROOT.parent / "gtavi-claudecode")
    for c in candidates:
        if (c / "scripts" / "moments.py").exists():
            return c
    raise FileNotFoundError(
        "gtavi-claudecode repo not found. Set paths.repo in config.json "
        "or the GTAVI_REPO_DIR environment variable."
    )


def load_env(path: Path | None = None) -> dict:
    """Parse .env (KEY=VALUE lines, # comments). Values are NOT printed."""
    env_path = path or (ROOT / ".env")
    vals: dict = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            vals[key.strip()] = val.strip().strip('"').strip("'")
    return vals


def env(key: str, default: str | None = None) -> str | None:
    """Look up a credential: process env first, then .env file."""
    val = os.environ.get(key)
    if val:
        return val
    return load_env().get(key, default)


def ensure_dirs() -> None:
    for rel in ("outbox", "media/official", "media/raw", "media/edits", "jobs", "logs"):
        (ROOT / rel).mkdir(parents=True, exist_ok=True)
