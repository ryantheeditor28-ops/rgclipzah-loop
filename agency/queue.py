"""Outbox queue: upload-ready post packages with an approval gate.

A package is a folder:  outbox/<post_id>/
    video.mp4              finished 1080x1920 clip (already rule-checked)
    meta.json              full state (see new_package)
    captions/<platform>.json   per-platform SEO metadata

Package lifecycle:
    pending_approval  ->  approved  ->  per-platform: queued -> posted | failed
                      ->  rejected (terminal)

meta.json schema:
{
  "id": "20261119-1530-a1b2c3",
  "created": "2026-11-19T15:30:41+01:00",
  "status": "pending_approval",          # package gate
  "pillar": "clip",                      # playbook pillar
  "hook": "...",
  "source": {"url": "...", "channel": "...", "title": "..."},
  "credit": "@creator",
  "video": "video.mp4",
  "duration_sec": 23.4,
  "layout": "blur",
  "rule_check": "PASS",
  "test_name": "",
  "notes": "",
  "posts": {                             # per-platform state, filled by poster
      "youtube": {"status": "queued", "post_id": "", "url": "", "posted_at": "", "error": ""}
  }
}
"""
from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from pathlib import Path

from .config import ROOT, ensure_dirs

PACKAGE_STATUSES = ("pending_approval", "approved", "rejected")
PLATFORM_STATUSES = ("queued", "posted", "failed", "skipped", "dry_run")


def outbox_dir() -> Path:
    ensure_dirs()
    return ROOT / "outbox"


def _new_id() -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    salt = hashlib.sha1(datetime.datetime.now().isoformat().encode()).hexdigest()[:6]
    return f"{stamp}-{salt}"


def new_package(
    video_path: Path,
    *,
    hook: str,
    source_url: str = "",
    source_channel: str = "",
    source_title: str = "",
    credit: str = "",
    pillar: str = "clip",
    layout: str = "blur",
    duration_sec: float = 0.0,
    rule_check: str = "PASS",
    platforms: list[str] | None = None,
    test_name: str = "",
    notes: str = "",
) -> dict:
    """Create an outbox package from a finished clip. Returns its meta dict."""
    ensure_dirs()
    platforms = platforms or ["youtube", "tiktok", "instagram", "x"]
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    pid = _new_id()
    pkg_dir = outbox_dir() / pid
    (pkg_dir / "captions").mkdir(parents=True, exist_ok=True)
    shutil.copy2(video_path, pkg_dir / "video.mp4")

    if duration_sec <= 0:
        from .media_probe import probe_duration

        duration_sec = probe_duration(pkg_dir / "video.mp4")

    meta = {
        "id": pid,
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "pending_approval",
        "pillar": pillar,
        "hook": hook,
        "source": {"url": source_url, "channel": source_channel, "title": source_title},
        "credit": credit,
        "video": "video.mp4",
        "duration_sec": round(float(duration_sec), 2),
        "layout": layout,
        "rule_check": rule_check,
        "test_name": test_name,
        "notes": notes,
        "posts": {p: {"status": "queued", "post_id": "", "url": "", "posted_at": "", "error": ""} for p in platforms},
    }
    save_meta(meta)
    return meta


def pkg_dir(meta_or_id: dict | str) -> Path:
    pid = meta_or_id["id"] if isinstance(meta_or_id, dict) else str(meta_or_id)
    return outbox_dir() / pid


def meta_path(meta_or_id: dict | str) -> Path:
    return pkg_dir(meta_or_id) / "meta.json"


def load_meta(meta_or_id: dict | str) -> dict:
    p = meta_path(meta_or_id)
    return json.loads(p.read_text(encoding="utf-8"))


def save_meta(meta: dict) -> Path:
    p = meta_path(meta)
    p.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def set_package_status(meta_or_id: dict | str, status: str) -> dict:
    assert status in PACKAGE_STATUSES, status
    meta = load_meta(meta_or_id)
    meta["status"] = status
    save_meta(meta)
    return meta


def set_platform_status(meta_or_id: dict | str, platform: str, status: str, **fields) -> dict:
    assert status in PLATFORM_STATUSES, status
    meta = load_meta(meta_or_id)
    if platform not in meta["posts"]:
        meta["posts"][platform] = {"status": "queued", "post_id": "", "url": "", "posted_at": "", "error": ""}
    meta["posts"][platform].update({"status": status, **fields})
    save_meta(meta)
    return meta


def set_caption(meta_or_id: dict | str, platform: str, caption: dict) -> Path:
    p = pkg_dir(meta_or_id) / "captions" / f"{platform}.json"
    p.write_text(json.dumps(caption, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def get_caption(meta_or_id: dict | str, platform: str) -> dict:
    p = pkg_dir(meta_or_id) / "captions" / f"{platform}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def list_packages(status: str | None = None) -> list[dict]:
    out = []
    if not outbox_dir().exists():
        return out
    for d in sorted(outbox_dir().iterdir()):
        mp = d / "meta.json"
        if not mp.exists():
            continue
        meta = json.loads(mp.read_text(encoding="utf-8"))
        if status is None or meta.get("status") == status:
            out.append(meta)
    return out


def approve(pid: str) -> dict:
    meta = set_package_status(pid, "approved")
    return meta


def reject(pid: str, reason: str = "") -> dict:
    meta = set_package_status(pid, "rejected")
    if reason:
        meta["notes"] = (meta.get("notes", "") + f" | rejected: {reason}").strip(" |")
        save_meta(meta)
    return meta


def trash_list() -> list[dict]:
    """Everything sitting in the trash (rejected, awaiting recover/purge)."""
    return [m for m in list_packages(status=None) if m.get("status") == "rejected"]


def delete_forever(meta_or_id: dict | str) -> bool:
    """Permanently remove a REJECTED package - video, captions, thumbs, folder."""
    meta = load_meta(meta_or_id)
    if meta.get("status") != "rejected":
        raise ValueError("only rejected packages can be deleted forever")
    d = pkg_dir(meta)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return not d.exists()


def summary_line(meta: dict) -> str:
    posts = " ".join(f"{k}:{v['status']}" for k, v in meta.get("posts", {}).items())
    return (
        f"{meta['id']}  [{meta['status']}]  {meta['duration_sec']}s  "
        f"hook={meta['hook'][:48]!r}  {posts}"
    )
