"""clip-agency: automated vertical-clip agency on top of gtavi-claudecode."""

# Windows consoles often default to a legacy code page (cp1252); captions and
# credits contain emoji (🎥). Force UTF-8 stdio wherever the package is used.
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
del _stream

