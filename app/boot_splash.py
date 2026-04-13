"""
VibeNode Boot Splash -- Ultra-Premium Edition
================================================
Full canvas-rendered 60fps animated boot splash with floating particles,
glowing progress bar with shimmer sweep, breathing title glow, completion
ring bursts, animated gradient border, vertical timeline, percentage
counter with ETA, and smooth eased transitions on everything.

Launched as a subprocess by session_manager.py.  Communicates via a status
file that run.py writes to.  Exits automatically when boot completes.

Usage:  pythonw boot_splash.py <status_file_path>
"""
import sys
import os
import math
import time
import random

try:
    import tkinter as tk
except ImportError:
    sys.exit(0)

try:
    from PIL import Image, ImageTk
    _has_pil = True
except ImportError:
    _has_pil = False

# ── Configuration ─────────────────────────────────────────────────────
STEPS = [
    ("cache",   "Clearing caches"),
    ("ports",   "Releasing ports"),
    ("deps",    "Checking dependencies"),
    ("daemon",  "Starting session daemon"),
    ("server",  "Initializing server"),
    ("browser", "Opening browser"),
]

W, H = 480, 490
FRAME_MS = 16        # ~60 fps
NUM_PARTICLES = 35

# VibeNode app palette (matches static/style.css dark theme)
C = dict(
    base="#111111", mantle="#0d0d0d", crust="#0a0a0a",
    text="#e8e8e8", subtext="#cccccc", overlay="#888888",
    surface2="#555555", surface1="#333333", surface0="#2a2a2a",
    blue="#7c7cff", lavender="#aaaaff", sapphire="#9a9aff",
    green="#3fb950", teal="#88cc88", red="#f85149",
    mauve="#bc8cff", peach="#d29922", yellow="#d29922",
    sky="#58a6ff", flamingo="#ccccff", rosewater="#e8e8e8",
)

PARTICLE_PALETTES = [C["blue"], C["lavender"], C["mauve"], C["sapphire"], C["sky"]]


# ── Helpers ───────────────────────────────────────────────────────────

def lerp_color(c1, c2, t):
    """Linearly interpolate between two hex colors."""
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return f"#{int(r1+(r2-r1)*t):02x}{int(g1+(g2-g1)*t):02x}{int(b1+(b2-b1)*t):02x}"


def ease_out_cubic(t):
    return 1 - (1 - t) ** 3


# ── Particle ──────────────────────────────────────────────────────────

class Particle:
    __slots__ = ("x", "y", "size", "speed", "wobble_amp", "wobble_freq",
                 "phase", "base_color", "rendered_color")

    def __init__(self):
        self.reset(initial=True)

    def reset(self, initial=False):
        self.x = random.uniform(0, W)
        self.y = random.uniform(0, H) if initial else random.uniform(H, H + 60)
        self.size = random.uniform(1.5, 4.0)
        self.speed = random.uniform(0.2, 0.7)
        self.wobble_amp = random.uniform(0.3, 1.2)
        self.wobble_freq = random.uniform(0.008, 0.03)
        self.phase = random.uniform(0, math.tau)
        self.base_color = random.choice(PARTICLE_PALETTES)
        alpha = random.uniform(0.12, 0.35)
        self.rendered_color = lerp_color(C["base"], self.base_color, alpha)

    def update(self, frame):
        self.y -= self.speed
        self.x += math.sin(frame * self.wobble_freq + self.phase) * self.wobble_amp * 0.3
        if self.y < -10:
            self.reset()


# ── Boot Splash ───────────────────────────────────────────────────────

class BootSplash:

    def __init__(self, status_file: str):
        self.status_file = status_file
        self.current_step = None
        self.completed: set = set()
        self.done = False
        self._last_read_pos = 0
        self._frame = 0
        self._start_time = time.time()

        # Animation state
        self._target_progress = 0.0
        self._current_progress = 0.0
        self._shimmer_x = -0.4
        self._fade_alpha = 0.0
        self._glow_phase = 0.0
        self._completion_flash: dict = {}   # step_id -> frame when completed
        self._finish_time = None
        self._finish_frame = 0

        # Total estimated boot time (seconds) for linear countdown
        self._eta_total = 8

        # Platform font
        if sys.platform == "win32":
            self._ff = "Segoe UI"
        elif sys.platform == "darwin":
            self._ff = "SF Pro Display"
        else:
            self._ff = "sans-serif"

        # Title font: prefer Space Grotesk (matches web UI header)
        self._tf = "Space Grotesk"

        # ── Window ────────────────────────────────────────────────────
        self.root = tk.Tk()

        # Linux font detection — must happen after Tk root is created
        # because tkinter.font.families() requires a running Tk instance.
        if sys.platform not in ("win32", "darwin"):
            try:
                import tkinter.font as _tkfont
                _available = set(_tkfont.families())
                for _candidate in ("Ubuntu", "Noto Sans", "DejaVu Sans", "Liberation Sans"):
                    if _candidate in _available:
                        self._ff = _candidate
                        break
            except Exception:
                pass
        self.root.title("VibeNode")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)          # start invisible

        sx = self.root.winfo_screenwidth()
        sy = self.root.winfo_screenheight()
        self.root.geometry(f"{W}x{H}+{(sx - W) // 2}+{(sy - H) // 2}")

        # ── Canvas ────────────────────────────────────────────────────
        self.cv = tk.Canvas(self.root, width=W, height=H,
                            bg=C["base"], highlightthickness=0)
        self.cv.pack(fill=tk.BOTH, expand=True)

        self._dx = self._dy = 0
        self.cv.bind("<Button-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_drag)

        # ── Build scene (order = z-order, first = bottom) ─────────────
        self._draw_background()
        self._create_particles()
        self._create_border_sweep()
        self._create_ui()
        self._create_progress_bar()
        self._create_steps()

        # ── Start loops ───────────────────────────────────────────────
        self.root.after(FRAME_MS, self._render)
        self.root.after(150, self._poll)
        self.root.after(90_000, self._timeout)

    # ── Background (static gradient, drawn once) ──────────────────────
    def _draw_background(self):
        for y in range(H):
            t = y / H
            curve = 0.5 - 0.5 * math.cos(t * math.pi)
            color = lerp_color(C["crust"], C["base"], curve * 0.8 + 0.1)
            self.cv.create_line(0, y, W, y, fill=color)
        # Static border frame
        for edge in [(0, 0, W, 0), (0, H - 1, W, H - 1),
                     (0, 0, 0, H), (W - 1, 0, W - 1, H)]:
            self.cv.create_line(*edge, fill=C["surface0"])

    # ── Particles (pre-created ovals, moved each frame) ───────────────
    def _create_particles(self):
        self.particles = [Particle() for _ in range(NUM_PARTICLES)]
        self._p_ids = []
        for p in self.particles:
            oid = self.cv.create_oval(
                p.x - p.size, p.y - p.size,
                p.x + p.size, p.y + p.size,
                fill=p.rendered_color, outline="")
            self._p_ids.append(oid)

    # ── Border sweep (pre-created rects for animated glow) ────────────
    def _create_border_sweep(self):
        # 3 layered glow rects per edge (top + bottom = 6)
        self._sweep = []
        for _ in range(6):
            self._sweep.append(
                self.cv.create_rectangle(0, 0, 0, 0, fill="", outline=""))

    # ── UI elements ───────────────────────────────────────────────────
    def _create_ui(self):
        cx = W // 2

        # Close button
        self._close = self.cv.create_text(
            W - 22, 18, text="\u2715", font=(self._ff, 12),
            fill=C["overlay"], tags="close")
        self.cv.tag_bind("close", "<Button-1>", lambda _: self._destroy())
        self.cv.tag_bind("close", "<Enter>",
                         lambda _: self.cv.itemconfigure(self._close,
                                                         fill=C["text"]))
        self.cv.tag_bind("close", "<Leave>",
                         lambda _: self.cv.itemconfigure(self._close,
                                                         fill=C["overlay"]))

        # Logo
        self._logo_img = None
        _app_dir = os.path.dirname(os.path.abspath(__file__))
        _proj_dir = os.path.dirname(_app_dir)
        logo_path = os.path.join(_proj_dir, "static", "images", "logo-dark.png")
        if os.path.exists(logo_path):
            try:
                if _has_pil:
                    img = Image.open(logo_path).convert("RGBA")
                    scale = 36 / img.height
                    img = img.resize((int(img.width * scale), 36),
                                     Image.LANCZOS)
                    self._logo_img = ImageTk.PhotoImage(img)
                else:
                    raw = tk.PhotoImage(file=logo_path)
                    factor = max(1, raw.height() // 36)
                    self._logo_img = raw.subsample(factor, factor)
            except Exception:
                pass

        brand_y = 50

        if self._logo_img:
            # Measure title text width to center logo + title as a group
            logo_w = self._logo_img.width()
            gap = 14
            _probe = self.cv.create_text(0, -100, text="VibeNode",
                                         font=(self._tf, 32, "bold"))
            tb = self.cv.bbox(_probe)
            title_w = tb[2] - tb[0] if tb else 190
            self.cv.delete(_probe)

            total = logo_w + gap + title_w
            group_left = (W - total) // 2

            # Anchor logo center-left; nudge up 2px to visually align
            # with text baseline (logo has bottom-right handshake circle)
            self.cv.create_image(group_left, brand_y + 8,
                                 image=self._logo_img, anchor="w")
            title_x = group_left + logo_w + gap + title_w // 2
        else:
            title_x = cx

        # Title glow layers (3 under-layers for bloom)
        self._tglow = []
        for _ in range(3):
            self._tglow.append(self.cv.create_text(
                cx if not self._logo_img else title_x,
                brand_y, text="VibeNode",
                font=(self._tf, 32, "bold"), fill=C["blue"]))
        self._title = self.cv.create_text(
            cx if not self._logo_img else title_x,
            brand_y, text="VibeNode",
            font=(self._tf, 32, "bold"), fill=C["text"])

        # Subtitle
        self._sub = self.cv.create_text(
            cx, brand_y + 34, text="Starting up\u2026",
            font=(self._ff, 10), fill=C["overlay"])

        # CTA — tagline + underlined link on separate lines
        self.cv.create_text(
            cx, H - 120,
            text="Building something complex? Need to sell it?",
            font=(self._ff, 9), fill=C["subtext"])
        self._cta = self.cv.create_text(
            cx, H - 102,
            text="customernode.com",
            font=(self._ff, 10, "bold underline"), fill=C["lavender"],
            tags="cta")
        self.cv.tag_bind("cta", "<Button-1>",
                         lambda _: __import__("webbrowser").open(
                             "https://customernode.com"))
        self.cv.tag_bind("cta", "<Enter>",
                         lambda _: (self.cv.itemconfigure(self._cta,
                                                          fill=C["text"]),
                                    self.cv.configure(cursor="hand2")))
        self.cv.tag_bind("cta", "<Leave>",
                         lambda _: (self.cv.itemconfigure(self._cta,
                                                          fill=C["lavender"]),
                                    self.cv.configure(cursor="")))

        # Single-line footer
        self.cv.create_text(
            cx, H - 18,
            text="CustomerNode\u2122  \u2022  Claude Code  \u2022  \u00a9 2026 CustomerNode LLC",
            font=(self._ff, 8), fill=C["surface1"])

        # Error text + dismiss button (hidden until error)
        self._err = self.cv.create_text(
            cx, H - 70, text="", font=(self._ff, 9),
            fill=C["red"], width=360)
        self._dismiss_bg = self.cv.create_rectangle(
            cx - 50, H - 42, cx + 50, H - 18,
            fill="", outline="", tags="dismiss")
        self._dismiss_txt = self.cv.create_text(
            cx, H - 30, text="", font=(self._ff, 9),
            fill="", tags="dismiss")
        self.cv.tag_bind("dismiss", "<Button-1>", lambda _: self._destroy())

    # ── Progress bar ──────────────────────────────────────────────────
    def _create_progress_bar(self):
        cx = W // 2
        self._bx, self._by = 50, 114
        self._bw, self._bh = W - 100, 6

        # Track background
        self.cv.create_rectangle(
            self._bx, self._by,
            self._bx + self._bw, self._by + self._bh,
            fill=C["surface0"], outline="")

        # Glow (behind fill, slightly taller)
        self._bglow = self.cv.create_rectangle(
            self._bx, self._by - 2, self._bx, self._by + self._bh + 2,
            fill="", outline="")

        # Fill
        self._bfill = self.cv.create_rectangle(
            self._bx, self._by, self._bx, self._by + self._bh,
            fill=C["blue"], outline="")

        # Shimmer highlight
        self._bshim = self.cv.create_rectangle(
            0, self._by, 0, self._by + self._bh,
            fill="", outline="")

        # Percentage (big, bold, center)
        self._pct = self.cv.create_text(
            cx, 138, text="0%",
            font=(self._ff, 18, "bold"), fill=C["blue"])

        # Elapsed (left) / Remaining (right) — ultra-subtle, flanking bar
        self._time_elapsed = self.cv.create_text(
            self._bx, self._by + self._bh + 10, text="",
            font=(self._ff, 7), fill=C["surface1"], anchor="w")
        self._time_remaining = self.cv.create_text(
            self._bx + self._bw, self._by + self._bh + 10, text="",
            font=(self._ff, 7), fill=C["surface1"], anchor="e")

    # ── Steps — 3D cylinder carousel (shows ~3 at a time) ────────────
    def _create_steps(self):
        self._step_cy = 260          # vertical center of carousel
        self._step_radius = 55       # cylinder radius in pixels
        self._step_angle = math.pi / 3   # 60° per step slot
        self._scroll_current = 0.0   # smoothly animated scroll position
        self._scroll_target = 0.0

        self._si = {}
        for i, (sid, label) in enumerate(STEPS):
            sym = self.cv.create_text(
                0, -100, text="\u25CB",
                font=(self._ff, 12), fill=C["surface2"], anchor="center")
            txt = self.cv.create_text(
                0, -100, text=label,
                font=(self._ff, 11), fill=C["surface2"], anchor="w")
            ring = self.cv.create_oval(0, 0, 0, 0,
                                       outline="", fill="", width=2)
            self._si[sid] = dict(sym=sym, txt=txt, ring=ring, idx=i)

    # ── Drag ──────────────────────────────────────────────────────────
    def _on_press(self, e):
        self._dx, self._dy = e.x, e.y

    def _on_drag(self, e):
        x = self.root.winfo_x() + e.x - self._dx
        y = self.root.winfo_y() + e.y - self._dy
        self.root.geometry(f"+{x}+{y}")

    # ==================================================================
    #  RENDER LOOP  (~60 fps)
    # ==================================================================
    def _render(self):
        # Auto-close after completion
        if self.done and self._finish_time:
            if time.time() - self._finish_time > 1.2:
                self._destroy()
                return

        try:
            self._frame += 1

            # Fade in
            if self._fade_alpha < 1.0:
                self._fade_alpha = min(1.0, self._fade_alpha + 0.035)
                self.root.attributes("-alpha",
                                     ease_out_cubic(self._fade_alpha))

            # Update time-based progress target
            self._recalc_target()

            # Smooth eased progress interpolation
            diff = self._target_progress - self._current_progress
            self._current_progress += diff * 0.06
            if abs(diff) < 0.002:
                self._current_progress = self._target_progress

            self._r_particles()
            self._r_progress()
            self._r_title()
            self._r_steps()
            self._r_time()
            self._r_border()
        except Exception:
            pass

        self.root.after(FRAME_MS, self._render)

    # ── Particle motion ───────────────────────────────────────────────
    def _r_particles(self):
        for i, p in enumerate(self.particles):
            p.update(self._frame)
            self.cv.coords(self._p_ids[i],
                           p.x - p.size, p.y - p.size,
                           p.x + p.size, p.y + p.size)
            # Occasional twinkle
            if self._frame % 60 == i % 60:
                p.rendered_color = lerp_color(
                    C["base"], p.base_color, random.uniform(0.1, 0.4))
                self.cv.itemconfigure(self._p_ids[i], fill=p.rendered_color)

    # ── Progress bar, shimmer, percentage ─────────────────────────────
    def _r_progress(self):
        prog = self._current_progress
        fill_w = prog * self._bw
        bx, by, bh = self._bx, self._by, self._bh

        # Resize fill + glow
        self.cv.coords(self._bfill, bx, by, bx + fill_w, by + bh)
        self.cv.coords(self._bglow, bx, by - 2, bx + fill_w, by + bh + 2)

        # Color shifts blue -> sapphire -> teal as progress grows
        if prog < 0.5:
            base = lerp_color(C["blue"], C["sapphire"], prog * 2)
        else:
            base = lerp_color(C["sapphire"], C["teal"], (prog - 0.5) * 2)

        # Gentle brightness pulse
        pulse = 0.5 + 0.5 * math.sin(self._frame * 0.06)
        bar_col = lerp_color(base, C["lavender"], pulse * 0.12)

        # On completion, flash white -> green
        if self.done:
            age = self._frame - self._finish_frame
            if age < 20:
                bar_col = lerp_color("#ffffff", C["green"],
                                     ease_out_cubic(age / 20))
            else:
                bar_col = C["green"]

        self.cv.itemconfigure(self._bfill, fill=bar_col)
        self.cv.itemconfigure(self._bglow,
                              fill=lerp_color(C["base"], bar_col, 0.12))

        # Shimmer sweep
        if prog > 0.01 and not self.done:
            self._shimmer_x += 0.018
            if self._shimmer_x > 1.4:
                self._shimmer_x = -0.4
            sc = bx + self._shimmer_x * fill_w
            sw = 35
            sx1, sx2 = max(bx, sc - sw), min(bx + fill_w, sc + sw)
            if sx2 > sx1:
                self.cv.coords(self._bshim, sx1, by, sx2, by + bh)
                self.cv.itemconfigure(
                    self._bshim,
                    fill=lerp_color(bar_col, "#ffffff", 0.3))
            else:
                self.cv.itemconfigure(self._bshim, fill="")
        else:
            self.cv.itemconfigure(self._bshim, fill="")

        # Percentage text
        pct = int(prog * 100)
        pct_col = bar_col
        if self.done:
            age = self._frame - self._finish_frame
            if age < 15:
                pct_col = lerp_color("#ffffff", C["green"],
                                     ease_out_cubic(age / 15))
            else:
                pct_col = C["green"]
        self.cv.itemconfigure(self._pct, text=f"{pct}%", fill=pct_col)

    # ── Title glow breathing ──────────────────────────────────────────
    def _r_title(self):
        self._glow_phase += 0.025
        intensity = 0.12 + 0.08 * math.sin(self._glow_phase)

        if self.done:
            glow = lerp_color(C["base"], C["green"], intensity)
            self.cv.itemconfigure(self._title, fill=C["green"])
        else:
            glow = lerp_color(C["base"], C["blue"], intensity)

        for g in self._tglow:
            self.cv.itemconfigure(g, fill=glow)

    # ── 3D cylinder carousel renderer ─────────────────────────────────
    def _r_steps(self):
        # Smooth scroll interpolation
        diff = self._scroll_target - self._scroll_current
        self._scroll_current += diff * 0.08
        if abs(diff) < 0.005:
            self._scroll_current = self._scroll_target

        cy = self._step_cy
        R = self._step_radius
        sa = self._step_angle
        cx = W // 2
        pulse_syms = ["\u25CF", "\u25C9", "\u25CB", "\u25C9"]

        for sid, s in self._si.items():
            idx = s["idx"]
            angle = (idx - self._scroll_current) * sa

            # facing = how much this slot faces the viewer (1=front, 0=edge)
            facing = math.cos(angle)

            if facing <= 0.05:
                # Behind the cylinder — hide offscreen
                self.cv.coords(s["sym"], -100, -100)
                self.cv.coords(s["txt"], -100, -100)
                self.cv.itemconfigure(s["ring"], outline="")
                continue

            # 3D position on cylinder surface
            y = cy + math.sin(angle) * R

            sym_x = cx - 70
            txt_x = cx - 45

            self.cv.coords(s["sym"], sym_x, y)
            self.cv.coords(s["txt"], txt_x, y)

            # Font sizes scale with facing (bigger = closer)
            sf = int(10 + facing * 4)    # sym: 10–14
            tf = int(9 + facing * 3)     # txt: 9–12

            # ── Determine state + base color ──────────────────────
            if sid in self.completed:
                base_col = C["green"]
                sym_text = "\u2713"
                sym_font = (self._ff, sf, "bold")

                # Completion flash + ring burst
                if sid in self._completion_flash:
                    age = self._frame - self._completion_flash[sid]
                    if age < 0:
                        base_col = C["surface2"]
                        sym_text = "\u25CB"
                        sym_font = (self._ff, sf)
                    elif age < 20:
                        t = ease_out_cubic(age / 20)
                        base_col = lerp_color("#ffffff", C["green"], t)
                        ring_r = 8 + age * 0.8
                        self.cv.coords(s["ring"],
                                       sym_x - ring_r, y - ring_r,
                                       sym_x + ring_r, y + ring_r)
                        self.cv.itemconfigure(
                            s["ring"],
                            outline=lerp_color(C["base"], C["green"],
                                               (1 - t) * 0.6),
                            width=2)
                    else:
                        self.cv.itemconfigure(s["ring"], outline="")

            elif sid == self.current_step:
                ci = (self._frame // 10) % len(pulse_syms)
                gp = 0.5 + 0.5 * math.sin(self._frame * 0.08)
                base_col = lerp_color(C["blue"], C["lavender"], gp * 0.5)
                sym_text = pulse_syms[ci]
                sym_font = (self._ff, sf)

            else:
                base_col = C["surface2"]
                sym_text = "\u25CB"
                sym_font = (self._ff, sf)
                self.cv.itemconfigure(s["ring"], outline="")

            # Apply facing as depth fade
            col = lerp_color(C["base"], base_col, facing * 0.85 + 0.15)

            self.cv.itemconfigure(s["sym"], text=sym_text, fill=col,
                                  font=sym_font)
            self.cv.itemconfigure(s["txt"], fill=col, font=(self._ff, tf))

    # ── Elapsed / ETA timer ───────────────────────────────────────────
    def _r_time(self):
        elapsed = time.time() - self._start_time

        if self.done:
            self.cv.itemconfigure(
                self._time_elapsed,
                text=f"{elapsed:.1f}s", fill=C["surface2"])
            self.cv.itemconfigure(self._time_remaining, text="")
            return

        es = int(elapsed)
        self.cv.itemconfigure(self._time_elapsed, text=f"{es}s")
        remaining = max(0, self._eta_total - elapsed)
        if remaining > 0:
            rem = max(1, int(remaining))
            self.cv.itemconfigure(self._time_remaining, text=f"~{rem}s left")
        else:
            self.cv.itemconfigure(self._time_remaining, text="")

    # ── Animated gradient border sweep ────────────────────────────────
    def _r_border(self):
        pos = ((self._frame * 1.5) % (W + 200)) - 100

        widths = [60, 30, 10]
        alphas = [0.1, 0.25, 0.5]
        top_cols = [C["blue"], C["blue"], C["lavender"]]
        bot_cols = [C["mauve"], C["mauve"], C["lavender"]]

        for i in range(3):
            # Top edge (left to right)
            sx, ex = pos - widths[i], pos + widths[i]
            self.cv.coords(self._sweep[i], sx, 0, ex, 2)
            self.cv.itemconfigure(
                self._sweep[i], outline="",
                fill=lerp_color(C["crust"], top_cols[i], alphas[i]))

            # Bottom edge (right to left)
            bp = W - pos
            sx, ex = bp - widths[i], bp + widths[i]
            self.cv.coords(self._sweep[3 + i], sx, H - 2, ex, H)
            self.cv.itemconfigure(
                self._sweep[3 + i], outline="",
                fill=lerp_color(C["crust"], bot_cols[i], alphas[i]))

    # ==================================================================
    #  STATUS FILE POLLING
    # ==================================================================
    def _poll(self):
        try:
            if os.path.exists(self.status_file):
                with open(self.status_file, "r", encoding="utf-8") as fh:
                    fh.seek(self._last_read_pos)
                    lines = fh.readlines()
                    self._last_read_pos = fh.tell()
                for raw in lines:
                    line = raw.strip()
                    if not line:
                        continue
                    if line == "DONE":
                        self._finish_ok()
                        return
                    if line.startswith("ERROR:"):
                        self._finish_error(line[6:].strip())
                        return
                    if line.startswith("STEP:"):
                        self._activate(line.split(":", 2)[1])
        except Exception:
            pass
        if not self.done:
            self.root.after(150, self._poll)

    def _activate(self, step_id):
        if self.current_step and self.current_step != step_id:
            self._mark_done(self.current_step)
        self.current_step = step_id
        # Record when this step started for intra-step decay
        if step_id in self._si:
            self._si[step_id]["_started"] = time.time()
            self._scroll_target = self._si[step_id]["idx"]
        self._recalc_target()

    def _mark_done(self, step_id):
        if step_id not in self.completed:
            self.completed.add(step_id)
            self._completion_flash[step_id] = self._frame
        self._recalc_target()

    def _recalc_target(self):
        """Step-capped continuous progress.

        A slow global time-based curve keeps the bar always moving, but it
        is capped so it can never get more than one step ahead of reality.
        This means: floor (completed steps) + current step's slice is the
        ceiling.  The time curve fills up to that ceiling naturally, so
        there's constant motion without overshooting what's actually done."""
        if self.done:
            return
        n = len(STEPS)
        slice_size = 0.95 / n

        # Ceiling: completed steps + the active step's full slice
        done_count = len(self.completed)
        if self.current_step and self.current_step not in self.completed:
            ceiling = (done_count + 1) * slice_size
        else:
            ceiling = done_count * slice_size

        # Global time curve: always moving, asymptotes toward 95%
        elapsed = time.time() - self._start_time
        T = self._eta_total
        if elapsed <= T:
            time_p = 0.95 * (elapsed / T)
        else:
            time_p = 0.95 + 0.04 * (1 - math.exp(-(elapsed - T) / 10))

        # Take the lesser of the time curve and the step ceiling
        self._target_progress = min(time_p, ceiling, 0.99)

    # ── Finish states ─────────────────────────────────────────────────
    def _finish_ok(self):
        self.done = True
        self._finish_time = time.time()
        self._finish_frame = self._frame
        self.current_step = None
        self._target_progress = 1.0
        # Scroll to last step and staggered completion cascade
        self._scroll_target = len(STEPS) - 1
        for i, (sid, _) in enumerate(STEPS):
            if sid not in self.completed:
                self.completed.add(sid)
                self._completion_flash[sid] = self._frame + i * 4
        self.cv.itemconfigure(self._sub, text="Ready!", fill=C["green"])

    def _finish_error(self, msg):
        self.done = True
        self._finish_time = None                     # don't auto-close
        self.cv.itemconfigure(self._sub, text="Startup failed",
                              fill=C["red"])
        self.cv.itemconfigure(self._err, text=msg)
        self.cv.itemconfigure(self._dismiss_bg,
                              fill=C["surface1"], outline=C["surface2"])
        self.cv.itemconfigure(self._dismiss_txt,
                              text="Dismiss", fill=C["text"])
        self.root.attributes("-topmost", False)

    def _timeout(self):
        if not self.done:
            self._destroy()

    def _destroy(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


# ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    try:
        splash = BootSplash(sys.argv[1])
        splash.run()
    except Exception:
        pass


if __name__ == "__main__":
    main()
