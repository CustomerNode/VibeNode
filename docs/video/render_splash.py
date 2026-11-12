"""
VibeNode Video Render Splash — Tkinter Progress
==================================================
Modeled after src/web/boot_splash.py. Shows scene progress,
frame counter, ETA, and completion state.
"""
import sys, os, math, time, random

try:
    import tkinter as tk
except ImportError:
    sys.exit(0)

W, H = 540, 360
FMS = 16
SCENE_NAMES = ["Hook", "Problem", "Sessions", "Workflow", "Workforce", "Impact", "CTA"]

C = dict(
    base="#0d0d0d", surface="#1a1a2a", border="#333350",
    text="#e8e8e8", dim="#666680", muted="#888898",
    blue="#7c7cff", green="#3fb950", red="#f85149",
)


class RenderSplash:
    def __init__(self, sf):
        self.sf = sf
        self._fpos = 0
        self._tick = 0
        self._t0 = time.time()
        self._building = set()
        self._done_set = set()
        self._phase = "Starting..."
        self._done = False
        self._encoding = False
        self._lines = []

        self.root = tk.Tk()
        self.root.title("VibeNode — Rendering Video")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        sx = self.root.winfo_screenwidth()
        sy = self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (W, H, (sx-W)//2, (sy-H)//2))

        ff = "Segoe UI" if sys.platform == "win32" else "Helvetica"
        self._ff = ff

        # Dark background
        self.root.configure(bg=C["base"])
        f = tk.Frame(self.root, bg=C["base"])
        f.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        # Title
        tk.Label(f, text="Rendering Video", font=(ff, 16, "bold"),
                 fg=C["text"], bg=C["base"]).pack(anchor="w")

        # Phase label
        self._phase_lbl = tk.Label(f, text="Starting...", font=(ff, 10),
                                    fg=C["muted"], bg=C["base"], anchor="w")
        self._phase_lbl.pack(fill=tk.X, pady=(2, 8))

        # Scene indicators frame
        sf2 = tk.Frame(f, bg=C["base"])
        sf2.pack(fill=tk.X, pady=(0, 8))
        self._scene_lbls = []
        for i, name in enumerate(SCENE_NAMES):
            lbl = tk.Label(sf2, text=name, font=(ff, 9), fg=C["dim"],
                           bg=C["surface"], padx=8, pady=3)
            lbl.pack(side=tk.LEFT, padx=2)
            self._scene_lbls.append(lbl)

        # Progress bar
        bar_f = tk.Frame(f, bg=C["surface"], height=14)
        bar_f.pack(fill=tk.X, pady=(0, 4))
        bar_f.pack_propagate(False)
        self._bar = tk.Frame(bar_f, bg=C["blue"], height=14)
        self._bar.place(x=0, y=0, relheight=1, width=0)
        self._bar_f = bar_f

        # Percentage
        self._pct_lbl = tk.Label(f, text="0%", font=(ff, 14, "bold"),
                                  fg=C["text"], bg=C["base"])
        self._pct_lbl.pack(anchor="w", pady=(0, 4))

        # Elapsed / ETA
        self._time_lbl = tk.Label(f, text="Elapsed: 0s", font=(ff, 9),
                                   fg=C["dim"], bg=C["base"], anchor="w")
        self._time_lbl.pack(fill=tk.X)

        # Log area — scrollable text showing status lines
        log_f = tk.Frame(f, bg=C["surface"], bd=1, relief=tk.FLAT)
        log_f.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self._log = tk.Text(log_f, font=(ff, 9), fg=C["muted"], bg=C["surface"],
                            wrap=tk.WORD, state=tk.DISABLED, bd=0, padx=6, pady=4,
                            highlightthickness=0)
        self._log.pack(fill=tk.BOTH, expand=True)

        self.root.after(FMS, self._render)
        self.root.after(300, self._poll)
        self.root.after(1800000, lambda: self._quit() if not self._done else None)

    def _log_line(self, text):
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _poll(self):
        if self._done:
            return
        try:
            if os.path.exists(self.sf):
                with open(self.sf, "r") as f:
                    f.seek(self._fpos)
                    new = f.readlines()
                    self._fpos = f.tell()
                for line in new:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._handle(line)
                    except Exception:
                        pass
        except Exception:
            pass
        if not self._done:
            self.root.after(300, self._poll)

    def _handle(self, line):
        if line.startswith("PSTART:"):
            parts = line[7:].split(":", 1)
            idx = int(parts[0])
            name = parts[1] if len(parts) > 1 else ""
            self._building.add(idx)
            self._phase = "Building: " + name
            self._log_line("[build] %s started" % name)
        elif line.startswith("PDONE:"):
            parts = line[6:].split(":", 1)
            idx = int(parts[0])
            name = parts[1] if len(parts) > 1 else ""
            self._building.discard(idx)
            self._done_set.add(idx)
            self._phase = "%d/7 scenes done" % len(self._done_set)
            self._log_line("[done] %s (%d/7)" % (name, len(self._done_set)))
        elif line.startswith("SF:"):
            pass  # per-frame progress, don't spam log
        elif line == "AUDIO":
            self._phase = "Generating audio..."
            self._log_line("[audio] Generating music + SFX")
        elif line == "ENCODING":
            self._encoding = True
            self._encode_t0 = time.time()
            self._phase = "Encoding final video..."
            self._log_line("[encode] Writing H.264 video — this takes a few minutes")
            for i in range(7):
                self._done_set.add(i)
            self._building.clear()
        elif line == "DONE":
            self._done = True
            self._phase = "Complete!"
            self._log_line("[done] Video rendered successfully!")
            self._pct_lbl.configure(fg=C["green"])
        elif line.startswith("ERROR:"):
            self._phase = "Error: " + line[6:]
            self._log_line("[ERROR] " + line[6:])
            self.root.after(8000, self._quit)
        elif line.startswith("FRAME:") or line.startswith("SCENE:"):
            pass  # handled by progress calc

    def _render(self):
        try:
            # Progress
            nd = len(self._done_set)
            nb = len(self._building)
            if self._done:
                pct = 100
            elif self._encoding:
                # Animate 92→99 over encoding duration (~5 min)
                enc_elapsed = time.time() - getattr(self, '_encode_t0', self._t0)
                pct = min(92 + int(enc_elapsed / 40), 99)  # +1% every 40s
            elif nd == 7:
                pct = 90  # crossfading
            else:
                pct = int((nd + nb * 0.5) / 7 * 85)

            # Update bar
            bar_w = int(self._bar_f.winfo_width() * pct / 100)
            self._bar.place(width=max(bar_w, 0))
            color = C["green"] if pct >= 100 else C["blue"]
            self._bar.configure(bg=color)

            # Labels
            self._pct_lbl.configure(text="%d%%" % pct)
            if self._encoding and not self._done:
                enc_s = int(time.time() - getattr(self, '_encode_t0', self._t0))
                self._phase_lbl.configure(text="Encoding final video... (%ds)" % enc_s)
            else:
                self._phase_lbl.configure(text=self._phase)

            # Scene indicators
            for i, lbl in enumerate(self._scene_lbls):
                if i in self._done_set:
                    lbl.configure(fg=C["green"], bg="#1a3020")
                elif i in self._building:
                    lbl.configure(fg=C["blue"], bg="#1a1a3a")
                else:
                    lbl.configure(fg=C["dim"], bg=C["surface"])

            # Time
            elapsed = int(time.time() - self._t0)
            if pct > 5 and pct < 100:
                eta = int(elapsed / pct * (100 - pct))
                self._time_lbl.configure(text="Elapsed %ds  |  ETA ~%ds" % (elapsed, eta))
            elif pct >= 100:
                self._time_lbl.configure(text="Completed in %ds" % elapsed)
            else:
                self._time_lbl.configure(text="Elapsed %ds" % elapsed)

            # Done state
            if self._done and not hasattr(self, "_done_ui"):
                self._done_ui = True
                btn = tk.Button(self.root, text="Close", font=(self._ff, 10, "bold"),
                                fg=C["green"], bg=C["surface"], bd=0, padx=16, pady=4,
                                command=self._quit)
                btn.pack(pady=(0, 8))
        except Exception:
            pass

        self.root.after(FMS, self._render)

    def _quit(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        sys.exit(0)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: pythonw render_splash.py <status_file>")
        sys.exit(1)
    RenderSplash(sys.argv[1]).run()
