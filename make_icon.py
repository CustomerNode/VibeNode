"""Generate claudecodegui.ico — terminal window with Claude spark"""

from PIL import Image, ImageDraw, ImageFilter, ImageFont
import math, os

SIZES = [256, 128, 64, 48, 32, 16]
OUT = os.path.join(os.path.dirname(__file__), "claudecodegui.ico")


def draw_icon(size):
    s = size
    p = lambda v: max(1, int(v * s / 256))
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ── Background: rounded dark terminal window ──────────────────
    bg_r = p(28)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=bg_r, fill=(14, 12, 24))

    # Subtle inner glow gradient — purple edge
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(p(18), 0, -1):
        alpha = int(60 * (1 - i / p(18)))
        gd.rounded_rectangle([i, i, s - 1 - i, s - 1 - i],
                              radius=max(1, bg_r - i),
                              outline=(120, 80, 220, alpha), width=1)
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)

    # ── Title bar ─────────────────────────────────────────────────
    tb_h = p(40)
    d.rounded_rectangle([0, 0, s - 1, tb_h], radius=bg_r, fill=(28, 22, 50))
    d.rectangle([0, bg_r, s - 1, tb_h], fill=(28, 22, 50))  # fill bottom of corners

    # Traffic-light dots
    dot_y = tb_h // 2
    for i, col in enumerate([(220, 80, 80), (220, 180, 60), (80, 200, 100)]):
        cx = p(22) + i * p(20)
        r = p(7)
        d.ellipse([cx - r, dot_y - r, cx + r, dot_y + r], fill=col)

    # ── Terminal body ─────────────────────────────────────────────
    # Prompt line 1
    px = p(22)
    py = tb_h + p(20)
    lh = p(30)

    prompt_col = (124, 124, 255)   # #7c7cff — matches GUI accent
    text_col   = (200, 200, 220)
    dim_col    = (90, 85, 130)
    green_col  = (80, 220, 140)

    # Line 1: "> claude" with blinking-cursor rectangle
    bar = p(8)
    # ">" chevron
    cx = px
    cy = py + lh // 2
    chev = p(7)
    d.line([(cx, cy - chev), (cx + chev, cy), (cx, cy + chev)],
           fill=prompt_col, width=max(2, p(3)))
    # "claude" text as blocks (for small sizes, draw rectangles as proxy glyphs)
    bx = px + p(18)
    by = py + p(6)
    bh = p(16)
    word_widths = [p(14), p(10), p(10), p(10), p(10), p(8)]  # c-l-a-u-d-e
    gap = p(3)
    for w in word_widths:
        d.rounded_rectangle([bx, by, bx + w, by + bh], radius=p(2), fill=text_col)
        bx += w + gap

    # Cursor block after text
    d.rectangle([bx + p(2), by, bx + p(11), by + bh], fill=prompt_col)

    # Line 2: dim output line
    py2 = py + lh
    bx2 = px + p(4)
    for w in [p(40), p(28), p(20)]:
        d.rounded_rectangle([bx2, py2 + p(7), bx2 + w, py2 + p(16)],
                             radius=p(2), fill=dim_col)
        bx2 += w + p(6)

    # Line 3: green "✓ Done" indicator
    py3 = py2 + lh
    ck = p(12)
    # Checkmark
    d.line([(px, py3 + p(10)), (px + ck // 2, py3 + p(18)),
             (px + ck + p(4), py3 + p(4))], fill=green_col, width=max(2, p(3)))
    bx3 = px + p(22)
    for w in [p(20), p(30)]:
        d.rounded_rectangle([bx3, py3 + p(6), bx3 + w, py3 + p(16)],
                             radius=p(2), fill=green_col)
        bx3 += w + p(6)

    # ── Claude spark / AI badge — bottom right ────────────────────
    br = p(52)
    bcx = s - p(52)
    bcy = s - p(52)

    # Outer glow
    glow2 = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd2 = ImageDraw.Draw(glow2)
    for i in range(p(24), 0, -2):
        alpha = int(80 * (1 - i / p(24)))
        gd2.ellipse([bcx - br - i, bcy - br - i, bcx + br + i, bcy + br + i],
                    fill=(140, 80, 255, alpha))
    glow2 = glow2.filter(ImageFilter.GaussianBlur(p(8)))
    img = Image.alpha_composite(img, glow2)
    d = ImageDraw.Draw(img)

    # Circle
    d.ellipse([bcx - br, bcy - br, bcx + br, bcy + br], fill=(72, 32, 160))
    # Inner lighter circle
    ir = p(38)
    d.ellipse([bcx - ir, bcy - ir, bcx + ir, bcy + ir], fill=(100, 50, 200))

    # Spark / lightning bolt shape
    lc = (220, 180, 255)
    pts = [
        (bcx + p(6),  bcy - p(26)),
        (bcx - p(8),  bcy - p(2)),
        (bcx + p(4),  bcy - p(2)),
        (bcx - p(6),  bcy + p(26)),
        (bcx + p(10), bcy + p(4)),
        (bcx - p(2),  bcy + p(4)),
    ]
    d.polygon(pts, fill=lc)

    return img


def make_ico():
    frames = [draw_icon(sz) for sz in SIZES]
    frames[0].save(OUT, format="ICO",
                   sizes=[(f.width, f.height) for f in frames],
                   append_images=frames[1:])
    print(f"Saved: {OUT}")


if __name__ == "__main__":
    make_ico()
