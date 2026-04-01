"""
VibeNode Boot Splash -- shows startup progress while the server initializes.

Launched as a subprocess by session_manager.py.  Communicates via a status
file that run.py writes to.  Exits automatically when boot completes.

Usage:  pythonw boot_splash.py <status_file_path>
"""
import sys
import os

try:
    import tkinter as tk
except ImportError:
    # tkinter not installed (some Linux distros) -- silently exit.
    # The app boots normally; the user just won't see the splash.
    sys.exit(0)

# ── Boot phases (must match the STEP:xxx ids sent by run.py) ────────────
STEPS = [
    ("cache",   "Clearing caches"),
    ("ports",   "Releasing ports"),
    ("deps",    "Checking dependencies"),
    ("daemon",  "Starting session daemon"),
    ("server",  "Initializing server"),
    ("browser", "Opening browser"),
]

_SYM_DONE   = "\u2713"   # checkmark
_SYM_ACTIVE = "\u25CF"   # filled circle
_ANIM_CYCLE = ["\u25CF", "\u25C9", "\u25CB", "\u25C9"]  # pulse: ● ◉ ○ ◉


class BootSplash:
    """Tkinter splash window that polls a status file for boot progress."""

    def __init__(self, status_file: str):
        self.status_file = status_file
        self.current_step: str | None = None
        self.completed: set[str] = set()
        self.done = False
        self._last_read_pos = 0
        self._anim_idx = 0

        # Platform fonts
        if sys.platform == "win32":
            self._font = "Segoe UI"
        elif sys.platform == "darwin":
            self._font = "Helvetica Neue"
        else:
            self._font = "sans-serif"

        # ── Window ──────────────────────────────────────────────────────
        self.root = tk.Tk()
        self.root.title("VibeNode")
        self.root.overrideredirect(True)          # no OS title bar
        self.root.attributes("-topmost", True)    # above other windows

        w, h = 400, 350
        sx = self.root.winfo_screenwidth()
        sy = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sx - w) // 2}+{(sy - h) // 2}")

        # ── Colour palette (Catppuccin Mocha-inspired) ──────────────────
        self._c = c = {
            "bg":      "#1e1e2e",
            "fg":      "#cdd6f4",
            "accent":  "#89b4fa",
            "green":   "#a6e3a1",
            "dim":     "#585b70",
            "red":     "#f38ba8",
            "border":  "#313244",
            "btn_bg":  "#45475a",
        }
        self.root.configure(bg=c["border"])

        # Inner frame (gives a 1-px border effect)
        inner = tk.Frame(self.root, bg=c["bg"], padx=30, pady=20)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # Make the window draggable from anywhere inside
        self._dx = self._dy = 0
        inner.bind("<Button-1>",  self._drag_start)
        inner.bind("<B1-Motion>", self._drag_move)

        # ── Close button (top-right) ────────────────────────────────────
        close_lbl = tk.Label(inner, text="\u00D7", font=(self._font, 14),
                             bg=c["bg"], fg=c["dim"], cursor="hand2")
        close_lbl.place(relx=1.0, rely=0.0, anchor="ne", x=-2, y=2)
        close_lbl.bind("<Button-1>", lambda _: self._destroy())

        # ── Title ───────────────────────────────────────────────────────
        tk.Label(inner, text="VibeNode", font=(self._font, 24, "bold"),
                 bg=c["bg"], fg=c["accent"]).pack(pady=(12, 3))

        # ── Subtitle ───────────────────────────────────────────────────
        self._subtitle = tk.Label(inner, text="Starting up\u2026",
                                  font=(self._font, 10),
                                  bg=c["bg"], fg=c["dim"])
        self._subtitle.pack(pady=(0, 22))

        # ── Step rows ──────────────────────────────────────────────────
        steps_frame = tk.Frame(inner, bg=c["bg"])
        steps_frame.pack(fill=tk.X, padx=10)

        self._widgets: dict[str, tuple[tk.Label, tk.Label]] = {}
        for step_id, label_text in STEPS:
            row = tk.Frame(steps_frame, bg=c["bg"])
            row.pack(fill=tk.X, pady=3)

            sym = tk.Label(row, text=" ", font=(self._font, 13),
                           bg=c["bg"], fg=c["dim"],
                           width=2, anchor="center")
            sym.pack(side=tk.LEFT)

            txt = tk.Label(row, text=label_text, font=(self._font, 11),
                           bg=c["bg"], fg=c["dim"], anchor="w")
            txt.pack(side=tk.LEFT, padx=(8, 0))

            self._widgets[step_id] = (sym, txt)

        # ── Progress bar ───────────────────────────────────────────────
        bar_bg = tk.Frame(inner, bg=c["border"], height=3)
        bar_bg.pack(fill=tk.X, pady=(25, 0))
        bar_bg.pack_propagate(False)

        self._bar = tk.Frame(bar_bg, bg=c["accent"], height=3)
        self._bar.place(x=0, y=0, relheight=1.0, relwidth=0.0)

        # ── Error area (initially hidden) ──────────────────────────────
        self._err_label = tk.Label(inner, text="", font=(self._font, 9),
                                   bg=c["bg"], fg=c["red"],
                                   wraplength=330, justify=tk.LEFT)
        self._dismiss_btn = tk.Button(
            inner, text="Dismiss", command=self._destroy,
            bg=c["btn_bg"], fg=c["fg"],
            activebackground=c["dim"], activeforeground=c["fg"],
            font=(self._font, 9), relief=tk.FLAT,
            padx=20, pady=4, cursor="hand2",
        )

        # ── Timers ─────────────────────────────────────────────────────
        self.root.after(150,     self._poll)
        self.root.after(400,     self._animate)
        self.root.after(90_000,  self._timeout)   # safety auto-close

    # ── Drag helpers ────────────────────────────────────────────────────
    def _drag_start(self, event):
        self._dx, self._dy = event.x, event.y

    def _drag_move(self, event):
        x = self.root.winfo_x() + event.x - self._dx
        y = self.root.winfo_y() + event.y - self._dy
        self.root.geometry(f"+{x}+{y}")

    # ── Status-file polling ─────────────────────────────────────────────
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
                        step_id = line.split(":", 2)[1]
                        self._activate(step_id)
        except Exception:
            pass

        if not self.done:
            self.root.after(150, self._poll)

    # ── Active-step animation (pulsing dot) ─────────────────────────────
    def _animate(self):
        if self.done:
            return
        if self.current_step and self.current_step in self._widgets:
            self._anim_idx = (self._anim_idx + 1) % len(_ANIM_CYCLE)
            sym, _ = self._widgets[self.current_step]
            sym.configure(text=_ANIM_CYCLE[self._anim_idx])
        self.root.after(400, self._animate)

    # ── Step state management ───────────────────────────────────────────
    def _activate(self, step_id: str):
        if self.current_step and self.current_step != step_id:
            self._complete(self.current_step)
        self.current_step = step_id
        if step_id in self._widgets:
            sym, txt = self._widgets[step_id]
            sym.configure(text=_SYM_ACTIVE, fg=self._c["accent"])
            txt.configure(fg=self._c["fg"])
        self._update_bar()

    def _complete(self, step_id: str):
        self.completed.add(step_id)
        if step_id in self._widgets:
            sym, txt = self._widgets[step_id]
            sym.configure(text=_SYM_DONE, fg=self._c["green"])
            txt.configure(fg=self._c["green"])
        self._update_bar()

    def _update_bar(self):
        n = len(self.completed)
        if self.current_step and self.current_step not in self.completed:
            n += 0.5                       # half credit for in-progress step
        frac = min(n / len(STEPS), 1.0)
        self._bar.place(x=0, y=0, relheight=1.0, relwidth=frac)

    # ── Finish states ───────────────────────────────────────────────────
    def _finish_ok(self):
        self.done = True
        for sid, _ in STEPS:
            self._complete(sid)
        self._subtitle.configure(text="Ready!", fg=self._c["green"])
        self._bar.place(x=0, y=0, relheight=1.0, relwidth=1.0)
        self.root.after(800, self._destroy)

    def _finish_error(self, msg: str):
        self.done = True
        self._subtitle.configure(text="Startup failed", fg=self._c["red"])
        self._err_label.configure(text=msg)
        self._err_label.pack(pady=(15, 0))
        self._dismiss_btn.pack(pady=(10, 0))
        self.root.attributes("-topmost", False)  # let user interact

    def _timeout(self):
        if not self.done:
            self._destroy()

    def _destroy(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    # ── Main loop ───────────────────────────────────────────────────────
    def run(self):
        self.root.mainloop()


# ────────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    try:
        splash = BootSplash(sys.argv[1])
        splash.run()
    except Exception:
        # If anything goes wrong (tkinter init, display not available, etc.)
        # just exit silently.  The app boots normally without the splash.
        pass


if __name__ == "__main__":
    main()
