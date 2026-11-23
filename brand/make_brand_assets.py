"""RGClipzah brand asset builder.

Inputs : the two generated logo images in Downloads (shield + wordmark).
Outputs: avatar.png, wordmark-bug.png, banner-yt.png + original copies.
"""
from __future__ import annotations

import os
import shutil

from PIL import Image, ImageDraw, ImageFilter

SRC_SHIELD = r"C:\Users\ryang\Downloads\Generated Image November 24, 2026 - 3_24AM.png"
SRC_TEXT = r"C:\Users\ryang\Downloads\Generated Image November 24, 2026 - 3_24AM (1).png"
OUT = os.path.dirname(os.path.abspath(__file__))

shutil.copy(SRC_SHIELD, os.path.join(OUT, "logo-full.png"))
shutil.copy(SRC_TEXT, os.path.join(OUT, "logo-text.png"))

shield = Image.open(SRC_SHIELD).convert("RGBA")
text = Image.open(SRC_TEXT).convert("RGBA")

# ---------------------------------------------------------------- avatar ----
# Tight square crop around the shield + wordmark, then 800x800 for FB/IG/YT.
avatar = shield.crop((140, 130, 884, 874)).resize((800, 800), Image.LANCZOS)
avatar.save(os.path.join(OUT, "avatar.png"))

# ---------------------------------------------------------- wordmark bug ----
# Crop the wordmark band, round it into a soft-edged dark pill badge.
# This is the brand bug that goes on RGClipzah's own edited clips.
band = text.crop((110, 390, 915, 700))  # 805 x 310
mask = Image.new("L", band.size, 0)
d = ImageDraw.Draw(mask)
d.rounded_rectangle((4, 4, band.width - 5, band.height - 5), radius=58, fill=255)
mask = mask.filter(ImageFilter.GaussianBlur(3))
bug = band.copy()
bug.putalpha(mask)
bug_w = 720
bug_h = round(bug.height * bug_w / bug.width)
bug = bug.resize((bug_w, bug_h), Image.LANCZOS)
bug.save(os.path.join(OUT, "wordmark-bug.png"))

# ------------------------------------------------------------ yt banner -----
# 2560x1440 with all content inside the 1546x423 safe area.
W, H = 2560, 1440
top = text.getpixel((512, 40))
bot = text.getpixel((512, 984))
grad = Image.new("RGBA", (W, H))
gp = grad.load()
for y in range(H):
    t = y / (H - 1)
    r = round(top[0] + (bot[0] - top[0]) * t)
    g = round(top[1] + (bot[1] - top[1]) * t)
    b = round(top[2] + (bot[2] - top[2]) * t)
    for x in range(0, W, 8):
        for xx in range(x, min(x + 8, W)):
            gp[xx, y] = (r, g, b, 255)

# RG emblem only (no baked-in wordmark), feathered blend, left of the pill.
emb = shield.crop((315, 285, 705, 595))  # 390 x 310
em = Image.new("L", emb.size, 0)
de = ImageDraw.Draw(em)
de.ellipse((10, 10, emb.width - 10, emb.height - 10), fill=255)
em = em.filter(ImageFilter.GaussianBlur(14))
emb.putalpha(em)
emb = emb.resize((320, 254), Image.LANCZOS)

pill = bug.resize((880, round(bug.height * 880 / bug.width)), Image.LANCZOS)

lock_w = 320 + 60 + pill.width
x0 = (W - lock_w) // 2
y_shield = (H - emb.height) // 2
y_pill = (H - pill.height) // 2
grad.alpha_composite(emb, (x0, y_shield))
grad.alpha_composite(pill, (x0 + 320 + 60, y_pill))
grad.convert("RGB").save(os.path.join(OUT, "banner-yt.png"), quality=92)

print("assets written to", OUT)
for f in ("logo-full.png", "logo-text.png", "avatar.png", "wordmark-bug.png", "banner-yt.png"):
    p = os.path.join(OUT, f)
    print(f"  {f:20s} {os.path.getsize(p):>9,} bytes  {Image.open(p).size}")
