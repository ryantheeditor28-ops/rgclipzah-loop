"""Poster base class: the contract every platform adapter implements.

Adapters NEVER read config.json's secrets; credentials come from env(key)
(process env or .env). Every adapter must work in three modes:
    is_configured() -> False          : posting refuses with a clear message
    dry_run(package)                  : validate + describe exactly what would upload
    upload(package)                   : real upload (only after package approval)

Return contract for upload():
    {"ok": True,  "post_id": "...", "url": "..."}            on success
    {"ok": False, "error": "human readable", "retry": bool}  on failure

Return contract for metrics(post):
    {"views": int|None, "likes": int|None, "comments": int|None,
     "shares": int|None, "saves": int|None, "followers": int|None}
    Use None when the platform does not expose the field.
"""
from __future__ import annotations

from pathlib import Path

from ..config import env, load_config
from ..media_probe import probe_duration, probe_streams

# codecs ffprobe reports for still images
IMAGE_CODECS = {"mjpeg", "jpeg", "jpg", "png", "webp", "gif", "bmp"}


def probe_is_image(path) -> bool:
    """True when the media file is a still image (single frame, no audio)."""
    try:
        streams = probe_streams(str(path))
    except Exception:  # noqa: BLE001
        return False
    v = [s for s in streams.get("streams", []) if s.get("codec_type") == "video"]
    a = [s for s in streams.get("streams", []) if s.get("codec_type") == "audio"]
    if not v or a:
        return False
    codec = (v[0].get("codec_name") or "").lower()
    if codec in IMAGE_CODECS:
        return True
    try:
        return probe_duration(Path(path)) < 1.0  # codec-agnostic fallback
    except Exception:  # noqa: BLE001
        return False


class PosterError(Exception):
    pass


class PosterBase:
    name: str = "base"
    required_env: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.cfg = load_config()

    # ---- configuration -------------------------------------------------
    def missing_config(self) -> list[str]:
        return [k for k in self.required_env if not env(k)]

    def is_configured(self) -> bool:
        return not self.missing_config()

    # ---- validation ----------------------------------------------------
    def validate(self, package: dict) -> list[str]:
        """Problems that block posting; empty list = ready."""
        problems: list[str] = []
        pkg = Path(__file__).resolve().parent.parent.parent / "outbox" / package["id"]
        video = pkg / package.get("video", "video.mp4")
        if not video.exists():
            problems.append(f"video missing: {video}")
        else:
            streams = probe_streams(video)
            vstreams = [s for s in streams.get("streams", []) if s.get("codec_type") == "video"]
            if not vstreams:
                problems.append("no video stream")
            elif probe_is_image(video):
                # image posts (reddit ingest): no audio, any aspect — the
                # platform APIs arbitrate aspect at post time
                pass
            else:
                if not any(s.get("codec_type") == "audio" for s in streams.get("streams", [])):
                    problems.append("no audio stream")
                w = vstreams[0].get("width") if vstreams else 0
                h = vstreams[0].get("height") if vstreams else 0
                # vertical 9:16 is the primary contract (Shorts/Reels accept any
                # resolution at that aspect); 1:1 square is also allowed (operator
                # posts square edits directly)
                ratio = (w / h) if (w and h) else 0.0
                if not (w and h and (abs(ratio - 9 / 16) < 0.02 or abs(ratio - 1.0) < 0.02)):
                    problems.append(f"not 9:16 or 1:1 (got {w}x{h})")
        if package.get("rule_check") != "PASS":
            problems.append(f"rule_check={package.get('rule_check')!r} (must be PASS)")
        if package.get("status") != "approved" and self.cfg["autonomy"]["approval_gate"]:
            problems.append(f"package status={package.get('status')!r} (needs approval)")
        return problems

    def package_is_image(self, package: dict) -> bool:
        try:
            return probe_is_image(self.video_path(package))
        except Exception:  # noqa: BLE001
            return False

    def dry_run(self, package: dict) -> dict:
        missing = self.missing_config()
        problems = self.validate(package)
        caption = package.get("_captions", {}).get(self.name, {})
        return {
            "platform": self.name,
            "configured": not missing,
            "missing_env": missing,
            "problems": problems,
            "would_upload": not missing and not problems,
            "caption_preview": {k: (str(v)[:120] if isinstance(v, str) else v) for k, v in caption.items()},
        }

    # ---- operations (implemented per platform) --------------------------
    def upload(self, package: dict) -> dict:
        raise NotImplementedError(f"{self.name}.upload()")

    def metrics(self, post: dict) -> dict:
        """post = the platform entry from meta['posts'][self.name]."""
        raise NotImplementedError(f"{self.name}.metrics()")

    # ---- helpers --------------------------------------------------------
    def video_path(self, package: dict) -> Path:
        from ..queue import pkg_dir

        return pkg_dir(package) / package.get("video", "video.mp4")

    def caption(self, package: dict) -> dict:
        return package.get("_captions", {}).get(self.name, {})

    @staticmethod
    def _duration(package: dict) -> float:
        from ..queue import pkg_dir

        return probe_duration(pkg_dir(package) / package.get("video", "video.mp4"))
