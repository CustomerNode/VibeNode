# -*- coding: utf-8 -*-
"""
VibeNode Explainer Video Builder -- Premium Studio Edition
============================================================
Cinematic 1080p explainer. Voice-synced text animations (from SRT data),
Ken Burns screenshots, crossfade scene transitions, synthesized ambient
music + sound design, film grain overlay, animated gradient backgrounds.

Usage:  python docs/video/build_video.py
"""

import os, sys, math, wave, time, subprocess, tempfile, re, threading
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

_tls = threading.local()  # thread-local for per-scene output dir

# ── Paths ──────────────────────────────────────────────────────────────
BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(BASE))
AUDIO_DIR = os.path.join(BASE, "audio")
SS_DIR = os.path.join(REPO, "docs", "screenshots")
OUTPUT = os.path.join(BASE, "vibenode_explainer.mp4")
STATUS_FILE = os.path.join(BASE, ".render_status")

W, H = 1920, 1080
FPS = 30
CROSSFADE = 0.35  # seconds — snappy cuts

# ── Palette ────────────────────────────────────────────────────────────
BG      = (10, 10, 16)
BG2     = (18, 18, 28)
ACCENT  = (124, 124, 255)
ACCENT2 = (63, 185, 80)
ACCENT3 = (255, 180, 50)
RED     = (248, 81, 73)
WHITE   = (232, 232, 232)
GRAY    = (155, 155, 175)
DIM     = (80, 80, 100)
SURFACE = (35, 35, 50)

_status_lock = threading.Lock()

def status(msg):
    try:
        with _status_lock:
            with open(STATUS_FILE, "a") as f:
                f.write(msg + "\n")
    except Exception:
        pass

# ── SRT Parser ─────────────────────────────────────────────────────────
def parse_srt(path):
    """Parse SRT into [(start_sec, end_sec, text), ...]"""
    cues = []
    if not os.path.exists(path):
        return cues
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n\n+", content.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        timing = lines[1]
        m = re.match(r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)", timing)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0]*3600 + g[1]*60 + g[2] + g[3]/1000
        end = g[4]*3600 + g[5]*60 + g[6] + g[7]/1000
        text = " ".join(lines[2:])
        cues.append((start, end, text))
    return cues

# ── Fonts ──────────────────────────────────────────────────────────────
def _f(size, bold=False):
    for p in (["C:/Windows/Fonts/segoeuib.ttf"] if bold else []) + \
             ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def _fm(size):
    """Monospace font for terminal scenes."""
    for p in ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/cour.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

F = {
    "hero": _f(84, True), "mega": _f(110, True), "h1": _f(58, True), "h2": _f(42, True),
    "h3": _f(34, True), "body": _f(26), "bodyb": _f(26, True),
    "small": _f(20), "pill": _f(16, True), "tiny": _f(14),
    "cmd": _f(32), "sub": _f(22), "caption": _f(24, True),
    "mono": _fm(18), "mono_lg": _fm(24),
    "stat_num": _f(72, True), "stat_label": _f(28, True),
}

# ── Easing ─────────────────────────────────────────────────────────────
def eo(t): return 1 - (1 - min(max(t, 0), 1)) ** 3
def eio(t): t = min(max(t, 0), 1); return 3*t*t - 2*t*t*t
def eob(t):
    t = min(max(t, 0), 1); c = 1.7
    return 1 + (c + 1) * (t - 1)**3 + c * (t - 1)**2
def ei(t): t = min(max(t, 0), 1); return t * t * t

# ── Drawing Primitives ─────────────────────────────────────────────────
_grain_cache = {}
def film_grain(img, intensity=12, seed=None):
    """Add subtle film grain for cinematic texture."""
    key = (W, H, seed or 0)
    if key not in _grain_cache or seed is None:
        rng = np.random.RandomState(seed)
        noise = rng.randint(-intensity, intensity + 1, (H, W, 3), dtype=np.int16)
        _grain_cache[key] = noise
    arr = np.array(img, dtype=np.int16)
    arr = np.clip(arr + _grain_cache[key], 0, 255).astype(np.uint8)
    img.paste(Image.fromarray(arr))

def gradient_bg(img, shift=0.0):
    """Animated gradient background with color pulses and depth."""
    d = ImageDraw.Draw(img)
    for y in range(H):
        ry = y / H
        wave = 0.03 * math.sin(ry * 8 + shift * 1.5)
        pulse = 0.02 * math.sin(shift * 2.0) * math.sin(ry * 4)
        t = min(max(ry + wave + pulse, 0), 1)
        r = int(BG[0] + (BG2[0] - BG[0]) * t)
        g = int(BG[1] + (BG2[1] - BG[1]) * t)
        b = int(BG[2] + (BG2[2] - BG[2]) * t + 12 * math.sin(ry * 3 + shift))
        # Subtle accent color bleed at edges
        edge = max(0, 1 - abs(ry - 0.5) * 3)
        b = min(255, b + int(8 * edge * math.sin(shift * 0.8)))
        d.line([(0, y), (W, y)], fill=(max(0,min(r,255)), max(0,min(g,255)), max(0,min(b,255))))

def radial_glow(img, cx, cy, radius, color, intensity=0.3):
    """Soft radial glow overlay."""
    overlay = Image.new("RGBA", (W, H), (0,0,0,0))
    od = ImageDraw.Draw(overlay)
    for r in range(radius, 0, -3):
        a = int(intensity * 255 * (1 - r/radius) ** 2)
        if a > 0:
            od.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(*color, min(a, 50)))
    img_rgba = img.convert("RGBA")
    result = Image.alpha_composite(img_rgba, overlay)
    img.paste(result.convert("RGB"))

def tc(d, text, y, f, color=WHITE, shadow=True):
    """Text centered."""
    bb = d.textbbox((0,0), text, font=f)
    tw = bb[2] - bb[0]
    x = (W - tw) // 2
    if shadow:
        d.text((x+2, y+3), text, font=f, fill=(0,0,0))
    d.text((x, y), text, font=f, fill=color)
    return tw

def tl(d, text, x, y, f, color=WHITE):
    """Text left."""
    d.text((x+1, y+2), text, font=f, fill=(0,0,0))
    d.text((x, y), text, font=f, fill=color)

def glow_line(d, y, progress, color=ACCENT, mw=None):
    w = int((mw or W*0.5) * min(progress, 1))
    xs = (W - w) // 2
    for i in range(5, 0, -1):
        c = tuple(max(0, v//(i+1)) for v in color)
        d.line([(xs, y-i), (xs+w, y-i)], fill=c, width=1)
        d.line([(xs, y+i), (xs+w, y+i)], fill=c, width=1)
    d.line([(xs, y), (xs+w, y)], fill=color, width=2)

def place_ss(img, path, rect, opacity=1.0, border=None, glow_r=0):
    """Screenshot with shadow, rounded corners, optional border glow."""
    if not os.path.exists(path): return
    x, y, w, h = rect
    raw = Image.open(path).convert("RGBA")
    # Fit within (w, h) preserving aspect ratio
    rw, rh = raw.size
    scale = min(w / rw, h / rh)
    fw, fh = int(rw * scale), int(rh * scale)
    ss = raw.resize((fw, fh), Image.LANCZOS)
    # Center within the requested rect
    x += (w - fw) // 2
    y += (h - fh) // 2
    w, h = fw, fh
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0,0,w,h], radius=12, fill=255)
    ss.putalpha(mask)
    # Shadow
    sh = Image.new("RGBA", (w+40, h+40), (0,0,0,0))
    sh.paste(Image.new("RGBA", (w, h), (0,0,0,70)), (20, 20))
    sh = sh.filter(ImageFilter.GaussianBlur(15))
    comp = Image.new("RGBA", (W, H), (0,0,0,0))
    comp.paste(sh, (x-15, y-10), sh)
    # Border glow
    if border:
        brd = Image.new("RGBA", (w+12, h+12), (0,0,0,0))
        ImageDraw.Draw(brd).rounded_rectangle([0,0,w+11,h+11], radius=14,
            outline=(*border, 80), width=3)
        brd = brd.filter(ImageFilter.GaussianBlur(5))
        comp.paste(brd, (x-6, y-6), brd)
    comp.paste(ss, (x, y), ss)
    if opacity < 1:
        a = comp.split()[3].point(lambda p: int(p * opacity))
        comp.putalpha(a)
    result = Image.alpha_composite(img.convert("RGBA"), comp)
    img.paste(result.convert("RGB"))

def ken_burns(img, path, t, dur, s0=1.08, s1=1.0, fade=0.6, darken=0):
    """Full-bleed screenshot with slow zoom and optional darkening overlay."""
    if not os.path.exists(path): return
    p = min(t / max(dur, 0.01), 1)
    s = s0 + (s1 - s0) * eio(p)
    ss = Image.open(path).convert("RGB")
    sw, sh = int(W * s), int(H * s)
    ss = ss.resize((sw, sh), Image.LANCZOS)
    cx, cy = sw//2, sh//2
    ss = ss.crop((cx - W//2, cy - H//2, cx + W//2, cy + H//2))
    op = min(t / fade, 1) if fade > 0 else 1.0
    if op < 1:
        bg = img.copy()
        img.paste(Image.blend(bg, ss, op))
    else:
        img.paste(ss)
    if darken > 0:
        ov = Image.new("RGBA", (W, H), (BG[0], BG[1], BG[2], int(darken)))
        img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"))

def animated_caption(d, text, y, t_in, t, f=None, color=WHITE, x=None):
    """Text that slides up and fades in when voice hits it."""
    f = f or F["caption"]
    elapsed = t - t_in
    if elapsed < 0: return
    p = eob(min(elapsed / 0.25, 1))
    y_off = int(18 * (1 - p))
    # Slight alpha simulation via color lerp
    c = tuple(int(BG2[i] + (color[i] - BG2[i]) * p) for i in range(3))
    if x is not None:
        tl(d, text, x, y + y_off, f, c)
    else:
        tc(d, text, y + y_off, f, c)


# ══════════════════════════════════════════════════════════════════════
# MUSIC + SFX
# ══════════════════════════════════════════════════════════════════════

def generate_music(dur, path):
    """124 BPM electronic: side-chained pad, arpeggiated lead, build-ups."""
    sr = 44100; n = int(dur * sr)
    bpm = 124; beat = 60.0 / bpm; bar = beat * 4; step16 = beat / 4
    sig = np.zeros(n, dtype=np.float64)

    def _s(sec): return max(0, min(int(sec * sr), n))
    def _add(buf, start_sec, sound):
        s = _s(start_sec); e = min(s + len(sound), n)
        if s >= e: return
        buf[s:e] += sound[:e - s]
    def _lp(x, k=24):
        return np.convolve(x, np.ones(k) / k, mode='same')

    def _sidechain_env(length_sec):
        ns = int(length_sec * sr); env = np.ones(ns)
        for i in range(int(length_sec / beat) + 1):
            pos = int(i * beat * sr); dl = int(beat * 0.75 * sr)
            if pos >= ns: break
            end = min(pos + dl, ns); sl = end - pos
            env[pos:end] = np.minimum(env[pos:end], 0.15 + 0.85 * (0.5 - 0.5 * np.cos(np.pi * np.arange(sl) / sl)))
        return env

    def _kick():
        d=0.22; ns=int(d*sr); ts=np.arange(ns)/sr
        freq=55+120*np.exp(-ts*35); phase=2*np.pi*np.cumsum(freq)/sr
        sub=np.sin(phase)*np.exp(-ts*8)
        click=0.35*np.sin(2*np.pi*4000*ts)*np.exp(-ts*120)
        body=0.2*np.sin(2*np.pi*110*ts)*np.exp(-ts*18)
        return np.tanh((sub+click+body)*1.6)*np.exp(-ts*5)*0.65

    def _hat(opn=False):
        d=0.045 if not opn else 0.14; ns=int(d*sr); ts=np.arange(ns)/sr
        decay=18 if not opn else 7
        h=np.random.randn(ns)*np.exp(-ts*decay)
        h+=0.4*np.sin(2*np.pi*7500*ts)*np.exp(-ts*decay)
        h+=0.25*np.sin(2*np.pi*9800*ts)*np.exp(-ts*(decay+5))
        return h*0.18

    def _pad_chunk(freq, length_sec):
        ns=int(length_sec*sr)
        if ns<=0: return np.zeros(1)
        ts=np.arange(ns)/sr; s=np.zeros(ns)
        for det in [0.993,0.997,1.0,1.003,1.007]:
            s+=(2*np.pi*freq*det*ts%(2*np.pi))/np.pi-1.0
        s/=5.0; s=_lp(s,80); s=_lp(s,60)
        env=np.ones(ns)
        att=min(int(0.08*sr),ns); rel=min(int(0.12*sr),ns)
        if att>1: env[:att]=np.linspace(0,1,att)
        if rel>1: env[-rel:]=np.linspace(1,0,rel)
        return s*env*0.18

    def _arp_note(freq, length_sec):
        ns=int(length_sec*sr)
        if ns<=0: return np.zeros(1)
        ts=np.arange(ns)/sr
        s=np.sin(2*np.pi*freq*ts)+0.35*np.sin(2*np.pi*freq*2*ts)+0.15*np.sin(2*np.pi*freq*3*ts)
        env=np.exp(-ts*14)*(1.0-np.exp(-ts*200))
        return s*env*0.07

    def _sub(freq, length_sec):
        ns=int(length_sec*sr)
        if ns<=0: return np.zeros(1)
        ts=np.arange(ns)/sr; s=np.sin(2*np.pi*freq*ts)
        env=np.ones(ns); att=min(int(0.005*sr),ns); rel=min(int(0.04*sr),ns)
        if att>1: env[:att]=np.linspace(0,1,att)
        if rel>1: env[-rel:]=np.linspace(1,0,rel)
        return s*env*0.22

    def _crash():
        d=1.2; ns=int(d*sr); ts=np.arange(ns)/sr
        c=np.random.randn(ns)*np.exp(-ts*2.5)+0.3*np.sin(2*np.pi*6000*ts)*np.exp(-ts*4)
        return c*0.12

    def _riser(length_sec):
        ns=int(length_sec*sr); ts=np.arange(ns)/sr; prog=ts/length_sec
        noise=np.random.randn(ns)*prog*0.3
        freq=200+2800*prog**2; phase=2*np.pi*np.cumsum(freq)/sr
        sweep=np.sin(phase)*prog*0.15
        return (noise+sweep)*np.linspace(0,1,ns)

    chord_roots=[55.0,43.65,65.41,49.0]
    chord_pads=[220.0,174.61,261.63,196.0]
    arp_seqs=[
        [440,523.25,659.25,880,659.25,523.25,880,1046.5],
        [349.23,440,523.25,698.46,523.25,440,698.46,880],
        [523.25,659.25,783.99,1046.5,783.99,659.25,1046.5,1318.5],
        [392,493.88,587.33,783.99,587.33,493.88,783.99,987.77],
    ]
    hat_vel=[1.0,0.2,0.45,0.25,0.85,0.2,0.45,0.3,0.95,0.2,0.45,0.25,0.85,0.2,0.5,0.35]
    swing=0.02

    k=_kick(); hh=_hat(False); hho=_hat(True); crash=_crash()
    num_bars=int(dur/bar)+2; section_len=8; build_bars=2

    for b in range(num_bars):
        bs=b*bar
        if bs>=dur: break
        ci=b%4; bis=b%section_len
        is_build=bis>=(section_len-build_bars); is_drop=bis==0 and b>0
        intro=min(b/4.0,1.0)

        if not is_build:
            for i in range(4):
                vel=1.0 if i%2==0 else 0.85
                _add(sig,bs+i*beat,k*vel*intro)
        else:
            prog=(bis-(section_len-build_bars))/build_bars
            steps=int(8+16*prog)
            for i in range(steps):
                _add(sig,bs+i*bar/steps,k*(0.4+0.4*i/steps))

        if not is_build:
            for i in range(16):
                sw=swing if i%2==1 else 0
                h=hho if i in(10,14) else hh
                _add(sig,bs+i*step16+sw,h*hat_vel[i]*intro)

        pad=_pad_chunk(chord_pads[ci],bar)
        sc=_sidechain_env(bar); sl=min(len(pad),len(sc))
        pad[:sl]*=sc[:sl]
        _add(sig,bs,pad*(intro if not is_build else 0.3))

        if not is_build:
            _add(sig,bs,_sub(chord_roots[ci],beat*1.8)*intro)
            _add(sig,bs+beat*2,_sub(chord_roots[ci],beat*1.8)*intro)

        if intro>0.5 and not is_build:
            arp=arp_seqs[ci]; nl=step16*0.85
            for i in range(8):
                sw=swing*0.5 if i%2==1 else 0
                vel=0.7+0.3*((i%4)==0)
                _add(sig,bs+i*beat/2+sw,_arp_note(arp[i%len(arp)],nl)*vel)

        if bis==(section_len-build_bars) and bs>1:
            _add(sig,bs,_riser(build_bars*bar))
        if is_drop:
            _add(sig,bs,crash)

    sig=np.tanh(sig*1.1); sig=_lp(sig,12)
    fi=int(2*sr); fo=int(3*sr)
    if fi>0 and fi<=n: sig[:fi]*=np.linspace(0,1,fi)
    if fo>0 and fo<=n: sig[-fo:]*=np.linspace(1,0,fo)
    pk=np.max(np.abs(sig))
    if pk>0: sig=sig/pk*0.25
    out=np.clip(sig*32767,-32768,32767).astype(np.int16)
    with wave.open(path,'w') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(out.tobytes())


def generate_transition_whoosh(path, dur=0.5):
    """Rising sweep with reverb tail for scene transitions."""
    sr=44100; tail=0.4; n=int((dur+tail)*sr); ts=np.arange(n)/sr
    sn=int(dur*sr); st=np.arange(sn)/sr; prog=st/dur
    freq=300+4200*prog**1.5; phase=2*np.pi*np.cumsum(freq)/sr
    sweep=np.sin(phase)*0.25*np.sin(np.pi*prog)**0.7
    noise=np.random.randn(sn)*0.15*np.sin(np.pi*prog)**0.7
    sig=np.zeros(n); sig[:sn]=sweep+noise
    tn=n-sn+int(0.05*sr); ts2=max(0,sn-int(0.05*sr))
    if tn>0:
        rev=np.random.randn(tn)*np.exp(-np.arange(tn)/sr*6)*0.12
        rev=np.convolve(rev,np.ones(48)/48,mode='same')
        end=min(ts2+tn,n); sig[ts2:end]+=rev[:end-ts2]
    pk=np.max(np.abs(sig))
    if pk>0: sig=sig/pk*0.45
    out=np.clip(sig*32767,-32768,32767).astype(np.int16)
    with wave.open(path,'w') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(out.tobytes())


def generate_whoosh(path, dur=0.4):
    generate_transition_whoosh(path, dur)


def generate_ui_click(path):
    """Subtle UI click sound."""
    sr=44100; d=0.06; n=int(d*sr); ts=np.arange(n)/sr
    click=0.6*np.sin(2*np.pi*3800*ts)*np.exp(-ts*300)
    body=0.4*np.sin(2*np.pi*1200*ts)*np.exp(-ts*80)
    thump=0.2*np.sin(2*np.pi*400*ts)*np.exp(-ts*120)
    sig=click+body+thump
    tn=int(0.04*sr); tail=np.random.randn(tn)*np.exp(-np.arange(tn)/sr*100)*0.03
    full=np.zeros(n+tn); full[:n]=sig; full[n:n+tn]+=tail
    pk=np.max(np.abs(full))
    if pk>0: full=full/pk*0.5
    out=np.clip(full*32767,-32768,32767).astype(np.int16)
    with wave.open(path,'w') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(out.tobytes())


def build_frames(fn, dur):
    total = int(dur * FPS)
    out_dir = getattr(_tls, "frame_dir", None)
    scene_idx = getattr(_tls, "scene_idx", -1)
    results = []
    for i in range(total):
        t = i / FPS
        img = Image.new("RGB", (W, H), BG)
        fn(img, t, i / max(total - 1, 1))
        film_grain(img, intensity=8, seed=i % 60)
        if out_dir:
            p = os.path.join(out_dir, "%06d.jpg" % i)
            img.save(p, "JPEG", quality=95)
            results.append(p)
        else:
            results.append(np.array(img))
        if scene_idx >= 0:
            status("SF:%d:%d/%d" % (scene_idx, i + 1, total))
    return results

# ── SCENE 1: HOOK ─────────────────────────────────────────────────────
# SRT: 0.05-0.95 "VibeNode." | 0.95-2.86 "Your command center..." | 2.86-9.1 "Manage sessions..."

def scene_hook(dur):
    """Clean, confident title card — Apple keynote style."""
    pillars = ["Sessions", "Workflow", "Workforce"]
    pillar_colors = [ACCENT, ACCENT2, ACCENT3]
    def draw(img, t, p):
        gradient_bg(img, shift=t * 0.3)
        # Single static centered radial glow behind title
        radial_glow(img, W // 2, 320, 500, ACCENT, 0.12)
        d = ImageDraw.Draw(img)
        # VibeNode title — clean white text with single subtle shadow
        if t >= 0.0:
            ap = eob(min(t / 0.5, 1))
            y = 240 + int(100 * (1 - ap))
            text = "VibeNode"
            bb = d.textbbox((0, 0), text, font=F["hero"])
            tw = bb[2] - bb[0]
            tx = (W - tw) // 2
            # Single subtle shadow — no multi-layer glow
            d.text((tx + 2, y + 3), text, font=F["hero"], fill=(0, 0, 0))
            d.text((tx, y), text, font=F["hero"], fill=WHITE)
            # Solid accent blue dot — no pulsing
            d.text((tx + tw + 4, y), ".", font=F["hero"], fill=ACCENT)
        # Subtitle — larger, using h2
        if t >= 0.95:
            animated_caption(d, "Your command center for Claude Code", 370, 0.95, t, F["h2"], GRAY)
        # Thin 1px horizontal line at y=440, ACCENT color, draws from center outward
        if t >= 1.5:
            line_p = eo(min((t - 1.5) / 1.0, 1))
            line_w = int(W * line_p)
            xs = (W - line_w) // 2
            if line_w > 0:
                d.line([(xs, 440), (xs + line_w, 440)], fill=ACCENT, width=1)
        # Three pillar words — appear simultaneously in a horizontal row
        if t >= 3.0:
            ap = eob(min((t - 3.0) / 0.45, 1))
            col_w = 400
            for i, (label, color) in enumerate(zip(pillars, pillar_colors)):
                cx = W // 2 + (i - 1) * col_w
                y = 490 + int(30 * (1 - ap))
                bb = d.textbbox((0, 0), label, font=F["h2"])
                lw = bb[2] - bb[0]
                lx = cx - lw // 2
                tc_c = tuple(int(DIM[j] + (color[j] - DIM[j]) * ap) for j in range(3))
                d.text((lx + 2, y + 3), label, font=F["h2"], fill=(0, 0, 0))
                d.text((lx, y), label, font=F["h2"], fill=tc_c)
        if t >= 7.0:
            bar_p = eo(min((t - 7.0) / 2.0, 1))
            glow_line(d, H - 60, bar_p, ACCENT, mw=int(W * 0.7))
    return build_frames(draw, dur)


def scene_problem(dur):
    """Animated terminal chaos with problem cards."""
    terminals = [
        ("$ claude --session api-refactor", "Rewrote auth module..."),
        ("$ claude --session fix-tests", "Running pytest... 3 failed"),
        ("$ claude --session ui-overhaul", "? Allow write to src/app.tsx"),
        ("$ claude --session db-migration", "ALTER TABLE users ADD col..."),
        ("$ claude --session deploy-fix", "Error: port 8080 in use"),
        ("$ claude --session perf-audit", "Profiling bundle size..."),
        ("$ claude --session docs-update", "Generating API reference..."),
        ("$ claude --session security-scan", "? Allow exec: npm audit"),
    ]
    term_start = [1.59 + i * 0.4 for i in range(8)]
    problems = [
        (1.59, "8+ sessions, zero visibility", "Terminal windows everywhere. Permission prompts piling up."),
        (8.09, "Speed without direction", "Your roadmap isn't moving. Sessions drift. Work gets duplicated."),
        (18.08, "Knowledge buried in dot-files", "Skills and agents scattered across directories nobody can find."),
    ]
    def draw(img, t, p):
        gradient_bg(img, shift=t * 0.4)
        radial_glow(img, W // 2, H // 2, 700, RED, 0.10)
        d = ImageDraw.Draw(img)
        if t >= 0.05:
            ap = eob(min((t - 0.05) / 0.35, 1))
            tc(d, "The Problem", 35 + int(30 * (1 - ap)), F["h1"], RED)
        if t >= 0.5:
            glow_line(d, 110, (t - 0.5) / 0.5, RED, mw=350)
        # Terminal grid
        cols, rows = 4, 2
        mx, my = 60, 135
        gap = 16
        tw = (W - 2 * mx - (cols - 1) * gap) // cols
        th = 120
        chaos_start = 15.0
        for idx, (cmd, output) in enumerate(terminals):
            ts = term_start[idx]
            if t < ts:
                continue
            col = idx % cols
            row = idx // cols
            el = t - ts
            ap = eob(min(el / 0.35, 1))
            bx = mx + col * (tw + gap)
            by = my + row * (th + gap + 20)
            chaos_p = min((t - chaos_start) / 4.0, 1.0) if t >= chaos_start else 0.0
            jx = int(chaos_p * 8 * math.sin(t * 5 + idx * 2.3))
            jy = int(chaos_p * 6 * math.cos(t * 4.7 + idx * 1.7))
            cx_t = W // 2 - tw // 2
            cy_t = H // 2 - th // 2
            op = chaos_p * 0.25
            bx = int(bx * (1 - op) + cx_t * op) + jx
            by = int(by * (1 - op) + cy_t * op) + jy
            dy = by + int(40 * (1 - ap))
            bc = ACCENT if chaos_p < 0.3 else tuple(int(ACCENT[j] * (1 - chaos_p) + RED[j] * chaos_p) for j in range(3))
            bw = 1 + int(chaos_p * 2)
            d.rounded_rectangle([bx + 4, dy + 4, bx + tw + 4, dy + th + 4], radius=8, fill=(5, 5, 8))
            d.rounded_rectangle([bx, dy, bx + tw, dy + th], radius=8, fill=(16, 16, 24))
            d.rounded_rectangle([bx, dy, bx + tw, dy + th], radius=8, outline=bc, width=bw)
            d.rounded_rectangle([bx, dy, bx + tw, dy + 22], radius=8, fill=(25, 25, 38))
            d.line([(bx, dy + 22), (bx + tw, dy + 22)], fill=(40, 40, 55), width=1)
            for di, dc in enumerate([(RED[0] // 2, 40, 40), (180, 140, 30), (40, 130, 50)]):
                d.ellipse([bx + 8 + di * 14, dy + 6, bx + 18 + di * 14, dy + 16], fill=dc)
            tp = min(el / 1.5, 1.0)
            cc = int(len(cmd) * tp)
            cursor = "_" if tp < 1.0 and int(t * 4) % 2 == 0 else ""
            d.text((bx + 8, dy + 30), cmd[:cc] + cursor, font=F["mono"], fill=ACCENT2)
            if tp >= 1.0 and el > 1.5:
                op2 = min((el - 1.5) / 0.8, 1.0)
                oc = int(len(output) * op2)
                out_c = RED if "Error" in output else (ACCENT3 if "?" in output else GRAY)
                d.text((bx + 8, dy + 55), output[:oc], font=F["mono"], fill=out_c)
            if int(t * 2.5 + idx) % 2 == 0:
                d.rectangle([bx + 8, dy + th - 20, bx + 16, dy + th - 10], fill=ACCENT if chaos_p < 0.5 else RED)
        # ── Problem 1 (1.59s): Large overlay text on the terminal visual ──
        if t >= 1.59:
            ap = eob(min((t - 1.59) / 0.5, 1))
            # Dark overlay strip at bottom for text readability
            oy = H - 180
            overlay = Image.new("RGBA", (W, 180), (BG[0], BG[1], BG[2], int(200 * ap)))
            img_rgba = img.convert("RGBA")
            img_rgba.paste(overlay, (0, oy), overlay)
            img.paste(img_rgba.convert("RGB"))
            d = ImageDraw.Draw(img)
            tc(d, "8+ sessions, zero visibility", oy + 30, F["h1"], tuple(int(RED[j] * ap) for j in range(3)))
            tc(d, "Terminal windows everywhere. Permission prompts piling up.", oy + 110, F["body"], tuple(int(GRAY[j] * ap) for j in range(3)))

        # ── Problem 2 (8.09s): FULL SCREEN progress bars — darken everything to suppress terminal grid ──
        if t >= 8.09:
            # Full-screen dark overlay to suppress terminal grid ghosts
            overlay_p2 = Image.new("RGBA", (W, H), (BG[0], BG[1], BG[2], 180))
            img.paste(Image.alpha_composite(img.convert("RGBA"), overlay_p2).convert("RGB"))
            d = ImageDraw.Draw(img)
            # Redraw header so it stays visible
            tc(d, "The Problem", 35, F["h1"], RED)
            if t >= 0.5:
                glow_line(d, 110, 1.0, RED, mw=350)
            ap = eob(min((t - 8.09) / 0.5, 1))
            # FULL SCREEN centered progress bars — 1400px wide, dominating the frame
            bar_area_x = (W - 1400) // 2
            bar_area_y = 180
            bar_w = 1400
            labels = ["Auth refactor", "Stripe hooks", "DB migration", "Test suite"]
            pcts = [0.72, 0.45, 0.15, 0.33]
            bar_gap = 110  # more vertical space for thick bars + large labels
            for bi in range(4):
                by2 = bar_area_y + bi * bar_gap
                bx2 = bar_area_x + int(100 * (1 - ap))
                # Large label — F["h3"]
                d.text((bx2, by2), labels[bi], font=F["h3"], fill=tuple(int(WHITE[j] * ap) for j in range(3)))
                # Thick 12px track
                d.rounded_rectangle([bx2, by2 + 42, bx2 + bar_w, by2 + 54], radius=6, fill=(30, 30, 42))
                # Fill - animates to stuck position then pulses red
                fill_p = min((t - 8.09 - bi * 0.3) / 1.5, 1.0) if t >= 8.09 + bi * 0.3 else 0
                fill_w = int(bar_w * pcts[bi] * eo(fill_p))
                fill_c = ACCENT2 if fill_p < 0.8 else tuple(int(ACCENT2[j] * 0.5 + RED[j] * 0.5) for j in range(3))
                if fill_w > 0:
                    d.rounded_rectangle([bx2, by2 + 42, bx2 + fill_w, by2 + 54], radius=6, fill=fill_c)
                # Percentage number to the right
                pct_text = "%d%%" % int(pcts[bi] * 100)
                d.text((bx2 + bar_w + 20, by2 + 38), pct_text, font=F["h3"], fill=tuple(int(WHITE[j] * ap) for j in range(3)))
                # Stuck indicator
                if fill_p >= 1.0:
                    stuck_ap = min((t - 8.09 - bi * 0.3 - 1.5) / 0.3, 1)
                    if stuck_ap > 0:
                        sx = bx2 + fill_w + 16
                        sc = tuple(int(RED[j] * stuck_ap) for j in range(3))
                        d.text((sx, by2 + 38), "STUCK", font=F["bodyb"], fill=sc)
            # Large overlay text at bottom
            oy2 = H - 180
            overlay2 = Image.new("RGBA", (W, 180), (BG[0], BG[1], BG[2], int(200 * ap)))
            img_rgba2 = img.convert("RGBA")
            img_rgba2.paste(overlay2, (0, oy2), overlay2)
            img.paste(img_rgba2.convert("RGB"))
            d = ImageDraw.Draw(img)
            tc(d, "Speed without direction", oy2 + 30, F["h1"], tuple(int(RED[j] * ap) for j in range(3)))
            tc(d, "Your roadmap isn't moving. Sessions drift. Work gets duplicated.", oy2 + 110, F["body"], tuple(int(GRAY[j] * ap) for j in range(3)))

        # ── Problem 3 (18.08s): FULL SCREEN dot-file terminal — darken everything to suppress progress bars ──
        if t >= 18.08:
            # Full-screen dark overlay to suppress progress bar ghosts
            overlay_p3 = Image.new("RGBA", (W, H), (BG[0], BG[1], BG[2], 180))
            img.paste(Image.alpha_composite(img.convert("RGBA"), overlay_p3).convert("RGB"))
            d = ImageDraw.Draw(img)
            # Redraw header so it stays visible
            tc(d, "The Problem", 35, F["h1"], tuple(int(RED[j] * 0.3) for j in range(3)))
            ap = eob(min((t - 18.08) / 0.5, 1))
            # LARGE centered terminal — 1200x500px, dominating the frame
            tw2, th2 = 1200, 500
            tx = (W - tw2) // 2 + int(50 * (1 - ap))
            ty = 100
            d.rounded_rectangle([tx + 6, ty + 6, tx + tw2 + 6, ty + th2 + 6], radius=10, fill=(5, 5, 8))
            d.rounded_rectangle([tx, ty, tx + tw2, ty + th2], radius=10, fill=(12, 12, 20))
            d.rounded_rectangle([tx, ty, tx + tw2, ty + th2], radius=10, outline=RED, width=2)
            # Title bar
            d.rounded_rectangle([tx, ty, tx + tw2, ty + 28], radius=10, fill=(25, 25, 38))
            for di, dc in enumerate([(RED[0] // 2, 40, 40), (180, 140, 30), (40, 130, 50)]):
                d.ellipse([tx + 10 + di * 18, ty + 7, tx + 22 + di * 18, ty + 19], fill=dc)
            d.text((tx + tw2 // 2 - 50, ty + 6), "~/.claude/", font=F["mono"], fill=DIM)
            # File listing - types out with F["mono_lg"] font
            files = [
                ("$ ls -la ~/.claude/", ACCENT2),
                ("drwxr-x---  .claude/", DIM),
                ("  -rw-------  CLAUDE.md", DIM),
                ("  -rw-------  agent_backend.md", DIM),
                ("  -rw-------  agent_frontend.md", DIM),
                ("  -rw-------  agent_devops.md", DIM),
                ("  -rw-------  agent_security.md", DIM),
                ("  -rw-------  skill_refactor.md", DIM),
                ("  -rw-------  skill_testing.md", DIM),
                ("  -rw-------  skill_review.md", DIM),
                ("  -rw-------  gstack_pipeline.json", DIM),
                ("  -rw-------  gstack_security.json", DIM),
                ("  -rw-------  settings.json", DIM),
                ("  -rw-------  ... 47 more files", RED),
            ]
            line_h = 32
            for fi, (ftxt, fclr) in enumerate(files):
                fy = ty + 38 + fi * line_h
                if fy + 24 > ty + th2:
                    break
                file_t = 18.08 + 0.12 * fi
                if t >= file_t:
                    fap = min((t - file_t) / 0.2, 1)
                    fc = tuple(int(fclr[j] * fap * ap) for j in range(3))
                    type_p = min((t - file_t) / 0.4, 1)
                    chars = int(len(ftxt) * type_p)
                    d.text((tx + 16, fy), ftxt[:chars], font=F["mono_lg"], fill=fc)
            # Blinking cursor
            if int(t * 2.5) % 2 == 0:
                last_file_y = ty + 38 + min(len(files), (th2 - 38) // line_h) * line_h
                d.rectangle([tx + 16, last_file_y, tx + 28, last_file_y + 18], fill=RED)
            # Large overlay text at bottom
            oy3 = H - 180
            overlay3 = Image.new("RGBA", (W, 180), (BG[0], BG[1], BG[2], int(200 * ap)))
            img_rgba3 = img.convert("RGBA")
            img_rgba3.paste(overlay3, (0, oy3), overlay3)
            img.paste(img_rgba3.convert("RGB"))
            d = ImageDraw.Draw(img)
            tc(d, "Knowledge buried in dot-files", oy3 + 30, F["h1"], tuple(int(RED[j] * ap) for j in range(3)))
            tc(d, "Skills and agents scattered across directories nobody can find.", oy3 + 110, F["body"], tuple(int(GRAY[j] * ap) for j in range(3)))
    return build_frames(draw, dur)

def scene_sessions(dur):
    """REAL APP: Route-intercept /api/sessions with fake data, navigate sessions view,
    click a real session card to preview it. No injected HTML -- only API interception."""
    from playwright.sync_api import sync_playwright
    import time as _time
    import json as _json
    import io

    total = int(dur * FPS)
    frames = []

    # 10 fake sessions -- the real app renders these via its real UI
    fake_sessions_json = [
        {"id":"fake-s1","display_title":"Refactor auth middleware","custom_title":"Refactor auth middleware","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:45 PM","last_activity_ts":1743782700,"sort_ts":1743782700,
         "size":"12 KB","file_bytes":12288,"message_count":42,"preview":"Rewrote JWT validation layer with RS256 support"},
        {"id":"fake-s2","display_title":"Stripe webhook handler","custom_title":"Stripe webhook handler","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:42 PM","last_activity_ts":1743782520,"sort_ts":1743782520,
         "size":"9 KB","file_bytes":9216,"message_count":38,"preview":"Implemented payment_intent.succeeded handler with idempotency"},
        {"id":"fake-s3","display_title":"Fix queue race condition","custom_title":"Fix queue race condition","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:38 PM","last_activity_ts":1743782280,"sort_ts":1743782280,
         "size":"5 KB","file_bytes":5120,"message_count":15,"preview":"Added mutex lock around dequeue operation"},
        {"id":"fake-s4","display_title":"DB schema migration v3","custom_title":"DB schema migration v3","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:30 PM","last_activity_ts":1743781800,"sort_ts":1743781800,
         "size":"8 KB","file_bytes":8192,"message_count":27,"preview":"? Allow ALTER TABLE users ADD COLUMN subscription_tier"},
        {"id":"fake-s5","display_title":"Integration test suite","custom_title":"Integration test suite","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:25 PM","last_activity_ts":1743781500,"sort_ts":1743781500,
         "size":"15 KB","file_bytes":15360,"message_count":51,"preview":"Running pytest -- 47 passed, 4 pending"},
        {"id":"fake-s6","display_title":"API docs - Payments","custom_title":"API docs - Payments","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:15 PM","last_activity_ts":1743780900,"sort_ts":1743780900,
         "size":"4 KB","file_bytes":4096,"message_count":12,"preview":"Generated OpenAPI spec for /v1/payments endpoints"},
        {"id":"fake-s7","display_title":"Refund webhook handler","custom_title":"Refund webhook handler","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  1:50 PM","last_activity_ts":1743779400,"sort_ts":1743779400,
         "size":"3 KB","file_bytes":3072,"message_count":8,"preview":"Handling charge.refunded events"},
        {"id":"fake-s8","display_title":"CI/CD deploy pipeline","custom_title":"CI/CD deploy pipeline","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  1:30 PM","last_activity_ts":1743778200,"sort_ts":1743778200,
         "size":"6 KB","file_bytes":6144,"message_count":20,"preview":"Configured GitHub Actions with staging deploy"},
        {"id":"fake-s9","display_title":"PCI compliance check","custom_title":"PCI compliance check","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  12:00 PM","last_activity_ts":1743772800,"sort_ts":1743772800,
         "size":"2 KB","file_bytes":2048,"message_count":6,"preview":"Auditing data-at-rest encryption settings"},
        {"id":"fake-s10","display_title":"Invoice PDF generator","custom_title":"Invoice PDF generator","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  11:30 AM","last_activity_ts":1743771000,"sort_ts":1743771000,
         "size":"1 KB","file_bytes":1024,"message_count":4,"preview":"Templating invoice layout with Puppeteer"},
    ]

    def _handle_sessions_route(route):
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(fake_sessions_json))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H})
        page = ctx.new_page()

        # Route interception BEFORE page.goto()
        page.route("**/socket.io/**", lambda r: r.abort())
        page.route("**/api/sessions", _handle_sessions_route)
        page.route("**/api/sessions?*", _handle_sessions_route)
        # Intercept individual session detail to prevent "session not found"
        def _handle_session_detail(route):
            url = route.request.url
            sid = url.split("/api/sessions/")[-1].split("?")[0].split("/")[0] if "/api/sessions/" in url else ""
            match = next((s for s in fake_sessions_json if s["id"] == sid), None)
            if match:
                detail = dict(match)
                detail["messages"] = [
                    {"role":"user","text":"Implement the payment webhook handler with idempotency keys","ts":"2026-04-04T14:30:00Z"},
                    {"role":"assistant","text":"I will implement the Stripe webhook handler. Let me start by reading the existing code.","ts":"2026-04-04T14:30:05Z"},
                ]
                detail["cost"] = {"total_cost": 0.42, "input_tokens": 28500, "output_tokens": 12300}
                route.fulfill(status=200, content_type="application/json", body=_json.dumps(detail))
            else:
                route.fulfill(status=200, content_type="application/json", body=_json.dumps(fake_sessions_json[0]))
        page.route("**/api/sessions/fake-*", _handle_session_detail)

        page.goto("http://localhost:5050", wait_until="networkidle")
        _time.sleep(2)

        # Dark theme + branding
        page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
        page.evaluate('() => { var pl=document.getElementById("project-label");if(pl)pl.textContent="VibeNode"; }')

        
        # Hide admin/sync/publish buttons from topnav
        page.evaluate('''() => {
            document.querySelectorAll('[onclick*=publish], [onclick*=sync], [onclick*=pullUpdate], [onclick*=pushUpdate]').forEach(el => el.style.display='none');
            document.querySelectorAll('.toolbar-btn').forEach(btn => {
                if(btn.textContent.match(/publish|sync|pull|push|update/i)) btn.style.display='none';
            });
        }''')

        # Switch to sessions grid view -- real app renders the 10 fake sessions
        page.evaluate("if(typeof setViewMode==='function')setViewMode('sessions')")
        page.evaluate("if(typeof setSessionDisplayMode==='function')setSessionDisplayMode('grid')")
        _time.sleep(1)

        # Fix search placeholder to show "Search 10 sessions..."
        page.evaluate('() => { var s=document.getElementById("search"); if(s) s.placeholder="Search 10 sessions..."; }')

        clicked_card = False
        card_click_time = 0.0

        for i in range(total):
            t = i / FPS

            # 0-4s: Browse the session grid, mouse moves across cards
            if t < 4.0 and not clicked_card:
                mx = 300 + int(t * 120)
                my = 250 + int(60 * math.sin(t * 0.6))
                page.mouse.move(mx, my)

            # 4s: Click a session card to preview it -- real app shows its chat/preview UI
            elif not clicked_card and t >= 4.0:
                clicked_card = True
                card_click_time = t
                try:
                    card = page.query_selector('.wf-card')
                    if card:
                        card.click()
                except Exception:
                    pass
                _time.sleep(0.5)

            # 4+: After clicking, the real app shows the session preview/chat panel
            # Move mouse naturally around the main content area
            elif clicked_card:
                elapsed = t - card_click_time
                mx = 900 + int(100 * math.sin(elapsed * 0.3))
                my = 400 + int(80 * math.sin(elapsed * 0.25))
                page.mouse.move(mx, my)

            frame_bytes = page.screenshot(type="jpeg", quality=92)
            frames.append(np.array(Image.open(io.BytesIO(frame_bytes))))

        browser.close()
    return frames


def scene_workflow(dur):
    """REAL APP: Route-intercept kanban API with fake tasks (including subtasks),
    navigate to kanban view, click real cards for drill-down. No injected HTML."""
    from playwright.sync_api import sync_playwright
    import time as _time
    import json as _json
    import io

    total = int(dur * FPS)
    frames = []

    # Real column IDs from the database
    COL_IDS = {
        "not_started": "d6d8a0b0-861a-419c-b5b1-70d69eabb179",
        "working":     "e518a672-0203-472e-92ed-aacafb08b487",
        "validating":  "a67e3266-b213-4428-9ffc-9af92f3a19a1",
        "remediating": "c96165cf-e8d1-4498-9eaf-b4648b313e92",
        "complete":    "beac1603-a80c-4f93-8e32-49c167114dfb",
    }

    PROJECT_ID = str(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))).replace("\\", "-").replace("/", "-").replace(":", "-")
    TS = "2026-04-04T14:00:00+00:00"

    def _task(tid, title, status, desc="", parent_id=None, depth=0,
              children_count=0, children_complete=0,
              session_count=0, active_sessions=0):
        return {
            "id": tid, "title": title, "status": status, "description": desc,
            "parent_id": parent_id, "depth": depth, "position": -1000,
            "project_id": PROJECT_ID, "children_count": children_count,
            "children_complete": children_complete, "session_count": session_count,
            "active_sessions": active_sessions, "owner": None,
            "verification_url": None, "created_at": TS, "updated_at": TS,
        }

    # Top-level tasks (depth=0)
    fake_tasks = [
        _task("t-ns-1", "Refund flow",          "not_started", children_count=3, children_complete=0),
        _task("t-ns-2", "Invoice generation",    "not_started", children_count=2, children_complete=0),
        _task("t-ns-3", "Subscription tiers",    "not_started", children_count=4, children_complete=0),
        _task("t-wk-1", "Stripe integration",    "working", "End-to-end Stripe payment integration",
              children_count=5, children_complete=2, session_count=1, active_sessions=1),
        _task("t-wk-2", "Webhook handlers",      "working", children_count=3, children_complete=1,
              session_count=1, active_sessions=1),
        _task("t-wk-3", "Payment validation",    "working", children_count=4, children_complete=1,
              session_count=1, active_sessions=1),
        _task("t-va-1", "Payment intent flow",   "validating", children_count=5, children_complete=4),
        _task("t-re-1", "Currency conversion",    "remediating", "Fix JPY/KRW rounding precision",
              children_count=3, children_complete=2, session_count=1, active_sessions=1),
        _task("t-co-1", "Customer model",        "complete", children_count=3, children_complete=3),
        _task("t-co-2", "Pricing table UI",      "complete", children_count=4, children_complete=4),
        _task("t-co-3", "Checkout flow",         "complete", children_count=5, children_complete=5),
        _task("t-co-4", "Subscription mgmt",     "complete", children_count=3, children_complete=3),
    ]

    # Subtasks for "Stripe integration" (t-wk-1)
    subtasks_wk1 = [
        _task("t-wk-1-sub1", "Create Stripe client wrapper",    "complete", parent_id="t-wk-1", depth=1),
        _task("t-wk-1-sub2", "Implement checkout session API",  "complete", parent_id="t-wk-1", depth=1),
        _task("t-wk-1-sub3", "Add payment method storage",      "working", parent_id="t-wk-1", depth=1,
              session_count=1, active_sessions=1),
        _task("t-wk-1-sub4", "Build subscription billing flow",  "not_started", parent_id="t-wk-1", depth=1),
        _task("t-wk-1-sub5", "Write integration tests",          "not_started", parent_id="t-wk-1", depth=1),
    ]

    # Subtasks for "Webhook handlers" (t-wk-2)
    subtasks_wk2 = [
        _task("t-wk-2-sub1", "payment_intent.succeeded handler", "complete", parent_id="t-wk-2", depth=1),
        _task("t-wk-2-sub2", "charge.refunded handler",         "working", parent_id="t-wk-2", depth=1,
              session_count=1, active_sessions=1),
        _task("t-wk-2-sub3", "Signature verification middleware", "not_started", parent_id="t-wk-2", depth=1),
    ]

    all_subtasks = subtasks_wk1 + subtasks_wk2
    all_tasks = fake_tasks + all_subtasks
    all_tasks_by_id = {t["id"]: t for t in all_tasks}

    fake_columns = [
        {"id": COL_IDS["not_started"],  "project_id": PROJECT_ID, "name": "Not Started",
         "status_key": "not_started",  "position": 0, "color": "#8b949e",
         "sort_mode": "position", "sort_direction": "asc", "is_terminal": False, "is_regression": False,
         "task_count": 3, "total_count": 3},
        {"id": COL_IDS["working"],      "project_id": PROJECT_ID, "name": "Working",
         "status_key": "working",      "position": 1, "color": "#58a6ff",
         "sort_mode": "position", "sort_direction": "asc", "is_terminal": False, "is_regression": False,
         "task_count": 3, "total_count": 3},
        {"id": COL_IDS["validating"],   "project_id": PROJECT_ID, "name": "Validating",
         "status_key": "validating",   "position": 2, "color": "#d29922",
         "sort_mode": "position", "sort_direction": "asc", "is_terminal": False, "is_regression": False,
         "task_count": 1, "total_count": 1},
        {"id": COL_IDS["remediating"],  "project_id": PROJECT_ID, "name": "Remediating",
         "status_key": "remediating",  "position": 3, "color": "#f85149",
         "sort_mode": "position", "sort_direction": "asc", "is_terminal": False, "is_regression": False,
         "task_count": 1, "total_count": 1},
        {"id": COL_IDS["complete"],     "project_id": PROJECT_ID, "name": "Complete",
         "status_key": "complete",     "position": 4, "color": "#3fb950",
         "sort_mode": "position", "sort_direction": "asc", "is_terminal": True, "is_regression": False,
         "task_count": 4, "total_count": 4},
    ]

    fake_board = {
        "columns": fake_columns,
        "tasks": fake_tasks,  # Board view shows only depth=0 tasks
        "tags": [],
        "active_tag_filter": [],
        "_timing": {"total": 0},
    }

    def _handle_kanban_board(route):
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(fake_board))

    def _handle_kanban_task(route):
        """Intercept /api/kanban/tasks/<id> and sub-routes -- return matching fake data."""
        url = route.request.url
        parts = url.split("/api/kanban/tasks/")
        if len(parts) > 1:
            remainder = parts[1].split("?")[0]  # e.g. "t-wk-1/ancestors" or "t-wk-1"
            segments = remainder.split("/")
            task_id = segments[0]
            task = all_tasks_by_id.get(task_id)

            # Handle /ancestors sub-route
            if len(segments) > 1 and segments[1] == "ancestors":
                if task:
                    ancestors = []
                    pid = task.get("parent_id")
                    while pid:
                        parent = all_tasks_by_id.get(pid)
                        if parent:
                            ancestors.append(parent)
                            pid = parent.get("parent_id")
                        else:
                            break
                    route.fulfill(status=200, content_type="application/json",
                                  body=_json.dumps({"ancestors": ancestors, "task": task}))
                else:
                    route.fulfill(status=404, content_type="application/json",
                                  body=_json.dumps({"error": "not found"}))
                return

            # Handle single task detail -- include children
            if task:
                result = dict(task)
                children = [t for t in all_tasks if t.get("parent_id") == task_id]
                result["children"] = children
                result["sessions"] = []
                result["issues"] = []
                result["tags"] = []
                route.fulfill(status=200, content_type="application/json",
                              body=_json.dumps(result))
                return

        route.fulfill(status=404, content_type="application/json",
                      body=_json.dumps({"error": "not found"}))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H})
        page = ctx.new_page()

        # Intercept ALL kanban API requests BEFORE page.goto()
        page.route("**/socket.io/**", lambda r: r.abort())
        page.route("**/api/kanban/board*", _handle_kanban_board)
        page.route("**/api/kanban/tasks/**", _handle_kanban_task)
        page.route("**/api/kanban/tasks/*", _handle_kanban_task)

        page.goto("http://localhost:5050", wait_until="networkidle")
        _time.sleep(2)

        # Dark theme + branding
        page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
        page.evaluate('() => { var pl=document.getElementById("project-label");if(pl)pl.textContent="VibeNode"; }')

        
        # Hide admin/sync/publish buttons from topnav
        page.evaluate('''() => {
            document.querySelectorAll('[onclick*=publish], [onclick*=sync], [onclick*=pullUpdate], [onclick*=pushUpdate]').forEach(el => el.style.display='none');
            document.querySelectorAll('.toolbar-btn').forEach(btn => {
                if(btn.textContent.match(/publish|sync|pull|push|update/i)) btn.style.display='none';
            });
        }''')

        # Navigate straight to kanban -- real app fetches from intercepted API
        page.evaluate("if(typeof setViewMode==='function')setViewMode('kanban')")
        _time.sleep(1.5)

        # VOICE-SYNCED TIMING:
        # 0-6.5s: "Hierarchical task board..." -> show the board
        # 6.5-16s: "Break epics, drill subtasks" -> DRILL DOWN
        # 16-23.5s: "AI planner" -> show AI planner
        # 23.5-end: "Vibe engineering" -> board view

        phase = "board"
        phase_t = 0.0

        for i in range(total):
            t = i / FPS

            # Phase 1: 0-6.5s - Show the board
            if t < 6.5 and phase == "board":
                progress = t / 6.5
                mx = 200 + int(progress * 1500)
                my = 300 + int(30 * math.sin(t * 0.5))
                page.mouse.move(mx, my)

            # Phase 2: 6.5s - Click Stripe integration to drill down
            elif phase == "board" and t >= 6.5:
                phase = "drilldown"
                phase_t = t
                try:
                    cards = page.query_selector_all('.kanban-card')
                    clicked = False
                    for card in cards:
                        text = card.inner_text()
                        if "Stripe" in text:
                            card.click()
                            clicked = True
                            break
                    if not clicked and len(cards) > 3:
                        cards[3].click()
                except Exception:
                    pass
                _time.sleep(1.0)

            # 6.5-10s: Explore drill-down (subtasks visible)
            elif phase == "drilldown" and t < 10.0:
                elapsed = t - phase_t
                mx = 600 + int(150 * math.sin(elapsed * 0.3))
                my = 300 + int(80 * math.sin(elapsed * 0.25))
                page.mouse.move(mx, my)

            # 10s: Click subtask to drill deeper
            elif phase == "drilldown" and t >= 10.0:
                phase = "subtask"
                phase_t = t
                try:
                    sub = page.query_selector('.kanban-drill-subtask-title, .kanban-drill-subtask-row')
                    if sub:
                        sub.click()
                        _time.sleep(0.8)
                except Exception:
                    pass

            # 10-14s: View subtask detail
            elif phase == "subtask" and t < 14.0:
                elapsed = t - phase_t
                mx = 700 + int(100 * math.sin(elapsed * 0.3))
                my = 350 + int(60 * math.sin(elapsed * 0.2))
                page.mouse.move(mx, my)

            # 14s: Back to board
            elif phase == "subtask" and t >= 14.0:
                phase = "back_to_board"
                phase_t = t
                try:
                    crumb = page.query_selector('.kanban-drill-crumb-board')
                    if crumb:
                        crumb.click()
                    else:
                        page.evaluate("if(typeof navigateToBoard==='function')navigateToBoard()")
                except Exception:
                    pass
                _time.sleep(1.0)

            # 14-16s: Board back
            elif phase == "back_to_board" and t < 16.0:
                mx = 500 + int(60 * math.sin((t - phase_t) * 0.4))
                my = 300
                page.mouse.move(mx, my)

            # 16s: Click Plan with AI
            elif phase == "back_to_board" and t >= 16.0:
                phase = "planner"
                phase_t = t
                try:
                    plan_btn = page.query_selector('button:has-text("Plan"), button:has-text("AI"), [onclick*=plan], .plan-btn')
                    if plan_btn:
                        plan_btn.click()
                        _time.sleep(1.0)
                    else:
                        btns = page.query_selector_all('button, .toolbar-btn')
                        for btn in btns:
                            txt = btn.inner_text().lower()
                            if "plan" in txt or "ai" in txt:
                                btn.click()
                                _time.sleep(1.0)
                                break
                except Exception:
                    pass

            # 16-23.5s: AI planner UI
            elif phase == "planner" and t < 23.5:
                elapsed = t - phase_t
                mx = 700 + int(100 * math.sin(elapsed * 0.2))
                my = 400 + int(60 * math.sin(elapsed * 0.15))
                page.mouse.move(mx, my)

            # 23.5s: Back to board for finale
            elif phase == "planner" and t >= 23.5:
                phase = "final"
                phase_t = t
                try:
                    page.evaluate("if(typeof navigateToBoard==='function')navigateToBoard()")
                except Exception:
                    pass
                _time.sleep(0.5)

            elif phase == "final":
                elapsed = t - phase_t
                mx = 900 + int(200 * math.sin(elapsed * 0.2))
                my = 350
                page.mouse.move(mx, my)

            frame_bytes = page.screenshot(type="jpeg", quality=92)
            frames.append(np.array(Image.open(io.BytesIO(frame_bytes))))

        browser.close()
    return frames


def scene_workforce(dur):
    """REAL APP: Route-intercept workforce and sessions APIs with fake data,
    navigate to workplace view, click real folder cards. No injected HTML."""
    from playwright.sync_api import sync_playwright
    import time as _time
    import json as _json
    import io

    total = int(dur * FPS)
    frames = []

    # Fake sessions for the workforce view (it shows recent sessions)
    fake_sessions_json = [
        {"id":"wf-s1","display_title":"API endpoint design","custom_title":"API endpoint design","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:40 PM","last_activity_ts":1743782400,"sort_ts":1743782400,
         "size":"8 KB","file_bytes":8192,"message_count":22,"preview":"Designing REST endpoints for /v1/invoices"},
        {"id":"wf-s2","display_title":"Component architecture","custom_title":"Component architecture","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:35 PM","last_activity_ts":1743782100,"sort_ts":1743782100,
         "size":"6 KB","file_bytes":6144,"message_count":18,"preview":"Refactoring dashboard into composable widgets"},
        {"id":"wf-s3","display_title":"CI/CD pipeline setup","custom_title":"CI/CD pipeline setup","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  2:20 PM","last_activity_ts":1743781200,"sort_ts":1743781200,
         "size":"4 KB","file_bytes":4096,"message_count":14,"preview":"Configuring GitHub Actions with staging deploy"},
        {"id":"wf-s4","display_title":"Security audit scan","custom_title":"Security audit scan","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  1:50 PM","last_activity_ts":1743779400,"sort_ts":1743779400,
         "size":"3 KB","file_bytes":3072,"message_count":9,"preview":"Running npm audit and checking CVE database"},
        {"id":"wf-s5","display_title":"DB migration scripts","custom_title":"DB migration scripts","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  1:30 PM","last_activity_ts":1743778200,"sort_ts":1743778200,
         "size":"5 KB","file_bytes":5120,"message_count":16,"preview":"Writing migration for subscription_tier column"},
        {"id":"wf-s6","display_title":"Unit test coverage","custom_title":"Unit test coverage","user_named":True,
         "date":"2026-04-04","last_activity":"2026-04-04  1:00 PM","last_activity_ts":1743776400,"sort_ts":1743776400,
         "size":"7 KB","file_bytes":7168,"message_count":25,"preview":"Adding tests for payment processing module"},
    ]

    # Fake workforce assets -- departments with agents/skills
    fake_workforce_assets = {
        "ok": True,
        "assets": [
            {"id":"agent-api-arch","name":"API Architect","department":"Backend","tags":["rest","graphql"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You are an API architecture specialist..."},
            {"id":"agent-db-spec","name":"DB Specialist","department":"Backend","tags":["sql","migrations"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You specialize in database schema design..."},
            {"id":"agent-auth-eng","name":"Auth Engineer","department":"Backend","tags":["oauth","jwt"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You handle authentication and authorization..."},
            {"id":"agent-cache-opt","name":"Cache Optimizer","department":"Backend","tags":["redis","caching"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You optimize caching strategies..."},
            {"id":"agent-ui-eng","name":"UI Engineer","department":"Frontend","tags":["react","components"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You build frontend component architectures..."},
            {"id":"agent-ux-rev","name":"UX Reviewer","department":"Frontend","tags":["accessibility","usability"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You review UI for accessibility and usability..."},
            {"id":"agent-perf","name":"Performance","department":"Frontend","tags":["bundle","lighthouse"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You optimize bundle size and load times..."},
            {"id":"agent-cicd","name":"CI/CD Pipeline","department":"DevOps","tags":["github-actions","deploy"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You manage build, test, and deploy automation..."},
            {"id":"agent-infra","name":"Infra Manager","department":"DevOps","tags":["cloud","scaling"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You manage cloud resources and scaling..."},
            {"id":"agent-vuln","name":"Vuln Scanner","department":"Security","tags":["cve","scanning"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You scan for dependency and code vulnerabilities..."},
            {"id":"agent-compliance","name":"Compliance","department":"Security","tags":["pci","soc2","gdpr"],
             "active":True,"version":"1.0","source":None,"allowed_tools":None,
             "systemPrompt":"You audit PCI, SOC2, and GDPR compliance..."},
        ],
        "map": None,
        "source": "disk",
    }

    def _handle_sessions_route(route):
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(fake_sessions_json))

    def _handle_workforce_assets(route):
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(fake_workforce_assets))

    def _handle_workforce_discover(route):
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"ok": True, "discovered": []}))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": W, "height": H})
        page = ctx.new_page()

        # Route interception BEFORE page.goto()
        page.route("**/socket.io/**", lambda r: r.abort())
        page.route("**/api/sessions", _handle_sessions_route)
        page.route("**/api/sessions?*", _handle_sessions_route)
        page.route("**/api/workforce/assets*", _handle_workforce_assets)
        page.route("**/api/workforce/discover*", _handle_workforce_discover)

        page.goto("http://localhost:5050", wait_until="networkidle")
        _time.sleep(2)

        # Dark theme + branding
        page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
        page.evaluate('() => { var pl=document.getElementById("project-label");if(pl)pl.textContent="VibeNode"; }')

        # Navigate to workplace view -- real app renders the real workforce UI
        page.evaluate("if(typeof setViewMode==='function')setViewMode('workplace')")
        _time.sleep(1.5)

        clicked_dept = False
        dept_click_time = 0.0

        for i in range(total):
            t = i / FPS

            # 0-5s: Browse the workforce command center (stats, department folders, recent sessions)
            if t < 5.0 and not clicked_dept:
                mx = 400 + int(t * 100)
                my = 300 + int(60 * math.sin(t * 0.5))
                page.mouse.move(mx, my)

            # 5s: Click a department folder card -- real app navigates into the folder
            elif not clicked_dept and t >= 5.0:
                clicked_dept = True
                dept_click_time = t
                try:
                    folder = page.query_selector('.ws-folder-card:not(.ws-add-folder-card)')
                    if folder:
                        folder.click()
                except Exception:
                    pass
                _time.sleep(1.0)

            # 5+: Browse inside the department (real app shows sessions in that folder)
            elif clicked_dept:
                elapsed = t - dept_click_time
                mx = 500 + int(150 * math.sin(elapsed * 0.3))
                my = 350 + int(80 * math.cos(elapsed * 0.25))
                page.mouse.move(mx, my)

            frame_bytes = page.screenshot(type="jpeg", quality=92)
            frames.append(np.array(Image.open(io.BytesIO(frame_bytes))))

        browser.close()
    return frames


def scene_impact(dur):
    """Dramatic strikethrough + radial burst + stat counters."""
    stats = [(7.38, "You plan", ACCENT, "Strategy"), (8.29, "You validate", ACCENT2, "Quality"), (9.50, "Claude executes", ACCENT3, "Speed")]
    def draw(img, t, p):
        gradient_bg(img, shift=t * 0.5)
        glow_b = 0.10 + 0.05 * math.sin(t * 1.0)
        radial_glow(img, W // 2, H // 2 - 50, 750, ACCENT, glow_b)
        d = ImageDraw.Draw(img)
        # "vibe coding" strikethrough
        if t >= 0.05:
            ap = eo(min(t / 0.3, 1))
            text = "vibe coding"
            bb = d.textbbox((0, 0), text, font=F["hero"])
            tw2 = bb[2] - bb[0]
            th2 = bb[3] - bb[1]
            tx = (W - tw2) // 2
            ty = 250 + int(50 * (1 - ap))
            dim_c = tuple(int(DIM[j] * ap) for j in range(3))
            d.text((tx + 2, ty + 3), text, font=F["hero"], fill=(0, 0, 0))
            d.text((tx, ty), text, font=F["hero"], fill=dim_c)
            if t >= 0.6:
                sp = eo(min((t - 0.6) / 0.15, 1))
                sw = int(tw2 * sp)
                sy = ty + th2 // 2 + 8
                for layer in range(8, 0, -2):
                    ga = int(40 * sp * (1 - layer / 9))
                    gc = (min(255, RED[0] // 2 + ga), RED[1] // 4, RED[2] // 4)
                    d.line([(tx - 10, sy - layer), (tx - 10 + sw + 20, sy - layer)], fill=gc, width=1)
                    d.line([(tx - 10, sy + layer), (tx - 10 + sw + 20, sy + layer)], fill=gc, width=1)
                d.line([(tx - 10, sy), (tx - 10 + sw + 20, sy)], fill=RED, width=6)
                if sp < 1.0:
                    tip = tx + sw
                    for si in range(6):
                        sx = tip + int(20 * math.cos(si * 1.05 + t * 8))
                        ssz = 2 + si % 3
                        d.ellipse([sx - ssz, sy + int(15 * math.sin(si * 1.05 + t * 8)) - ssz, sx + ssz, sy + int(15 * math.sin(si * 1.05 + t * 8)) + ssz], fill=RED)
        # "vibe engineering" slam
        if t >= 1.69:
            sel = t - 1.69
            ap = eob(min(sel / 0.4, 1))
            if sel < 1.5:
                bp = eo(min(sel / 0.8, 1))
                br = int(600 * bp)
                bi = 0.2 * (1 - bp)
                if bi > 0.01:
                    radial_glow(img, W // 2, 470, br, ACCENT, bi)
                    d = ImageDraw.Draw(img)
            y = 410 + int(80 * (1 - ap))
            text = "vibe engineering"
            bb = d.textbbox((0, 0), text, font=F["mega"])
            tw2 = bb[2] - bb[0]
            tx = (W - tw2) // 2
            for layer in range(6, 0, -1):
                ga = int(30 * ap * (1 - layer / 7))
                gc = (ACCENT[0] // 4 + ga, ACCENT[1] // 4 + ga, min(255, ACCENT[2] // 3 + ga))
                for ox in [layer * 3, -layer * 3]:
                    d.text((tx + ox, y), text, font=F["mega"], fill=gc)
                d.text((tx, y + layer * 3), text, font=F["mega"], fill=gc)
            d.text((tx + 3, y + 4), text, font=F["mega"], fill=(0, 0, 0))
            d.text((tx, y), text, font=F["mega"], fill=ACCENT)
            if sel >= 0.6:
                glp = eo(min((sel - 0.6) / 0.6, 1))
                pulse = 1.0 + 0.1 * math.sin(t * 2.5) if glp >= 1.0 else 1.0
                glow_line(d, y + 120, glp, ACCENT, mw=int(800 * pulse))
        if t >= 3.36:
            animated_caption(d, "Structured planning and validation", 580, 3.36, t, F["body"], GRAY)
        if t >= 5.0:
            animated_caption(d, "with a human in the loop", 620, 5.0, t, F["body"], GRAY)
        # Stat counters
        if t >= 7.0:
            col_w = W // 3
            for i, (vt, label, color, sublabel) in enumerate(stats):
                if t < vt:
                    continue
                el = t - vt
                ap = eob(min(el / 0.4, 1))
                cx = col_w // 2 + i * col_w
                by = 700
                yo = int(50 * (1 - ap))
                cr = int(45 * ap)
                if cr > 0:
                    pulse = 0.5 + 0.3 * math.sin(t * 2 + i * 1.2)
                    cc = tuple(int(color[j] * 0.15 * pulse) for j in range(3))
                    d.ellipse([cx - cr, by - 30 + yo - cr, cx + cr, by - 30 + yo + cr], fill=cc)
                lc = tuple(int(BG2[j] + (color[j] - BG2[j]) * ap) for j in range(3))
                bb = d.textbbox((0, 0), label, font=F["stat_label"])
                lw2 = bb[2] - bb[0]
                d.text((cx - lw2 // 2 + 2, by + yo + 3), label, font=F["stat_label"], fill=(0, 0, 0))
                d.text((cx - lw2 // 2, by + yo), label, font=F["stat_label"], fill=lc)
                sbb = d.textbbox((0, 0), sublabel, font=F["small"])
                sw2 = sbb[2] - sbb[0]
                d.text((cx - sw2 // 2, by + yo + 38), sublabel, font=F["small"], fill=tuple(int(DIM[j] * ap) for j in range(3)))
                dr = int(5 * min(el / 0.2, 1))
                if dr > 0:
                    d.ellipse([cx - dr, by + yo - 20 - dr, cx + dr, by + yo - 20 + dr], fill=color)
        if t >= 10.5:
            animated_caption(d, "Scoped to tasks, not left to wander.", 850, 10.5, t, F["bodyb"], GRAY)
    return build_frames(draw, dur)


def scene_cta(dur):
    """Terminal box, GitHub glow, badges, closing logo."""
    cmd_text = "Get me set up with https://github.com/CustomerNode/VibeNode"
    badges = [(7.56, "Open Source", ACCENT), (8.8, "Free Forever", ACCENT2), (10.0, "MIT License", ACCENT3)]
    def draw(img, t, p):
        gradient_bg(img, shift=t * 0.25)
        glow_i = 0.12 + 0.06 * math.sin(t * 0.9)
        radial_glow(img, W // 2, 380, 600, ACCENT, glow_i)
        d = ImageDraw.Draw(img)
        for i in range(15):
            sx = (i * 173 + 30) % W
            px = sx + int(20 * math.sin(t * 0.25 + i * 0.9))
            py = int((H + 30 - (t * (10 + (i % 4) * 6) + i * 50) % (H + 60)))
            sz = 1 + i % 2
            pa = 0.2 + 0.2 * math.sin(t * 1.2 + i)
            d.ellipse([px - sz, py - sz, px + sz, py + sz], fill=tuple(int(ACCENT[j] * pa * 0.3) for j in range(3)))
        if t >= 0.05:
            ap = eob(min(t / 0.4, 1))
            tc(d, "Get Started", 120 + int(50 * (1 - ap)), F["h1"], WHITE)
        if t >= 0.6:
            glow_line(d, 200, (t - 0.6) / 0.6, ACCENT, mw=400)
        # Terminal box — natural language to Claude Code (references README)
        if t >= 1.85:
            tap = eo(min((t - 1.85) / 0.5, 1))
            bw2, bh2 = 1200, 160
            bx2 = (W - bw2) // 2
            by2 = 250
            dh = int(bh2 * tap)
            dy2 = by2 + (bh2 - dh) // 2
            for g in range(10, 0, -2):
                ga = int(20 * tap * (1 - g / 11))
                gc = (ACCENT[0] // 6 + ga, ACCENT[1] // 6 + ga, min(255, ACCENT[2] // 5 + ga))
                d.rounded_rectangle([bx2 - g, dy2 - g, bx2 + bw2 + g, dy2 + dh + g], radius=16 + g, fill=gc)
            d.rounded_rectangle([bx2, dy2, bx2 + bw2, dy2 + dh], radius=14, fill=(12, 12, 20))
            d.rounded_rectangle([bx2, dy2, bx2 + bw2, dy2 + dh], radius=14, outline=ACCENT, width=2)
            d.rounded_rectangle([bx2, dy2, bx2 + bw2, dy2 + 32], radius=14, fill=(22, 22, 35))
            d.line([(bx2, dy2 + 32), (bx2 + bw2, dy2 + 32)], fill=(40, 40, 55), width=1)
            for di, dc in enumerate([(220, 60, 60), (220, 180, 40), (60, 190, 70)]):
                d.ellipse([bx2 + 14 + di * 20, dy2 + 9, bx2 + 26 + di * 20, dy2 + 21], fill=dc)
            d.text((bx2 + bw2 // 2 - 55, dy2 + 8), "Claude Code", font=F["tiny"], fill=DIM)
            if t >= 2.2 and dh > 40:
                te = t - 2.2
                tp2 = min(te / 2.0, 1.0)
                chars = int(len(cmd_text) * tp2)
                cursor = "_" if tp2 < 1.0 and int(t * 3) % 2 == 0 else ""
                prompt = "> "
                vis = prompt + cmd_text[:chars]
                d.text((bx2 + 20, dy2 + 55), vis + cursor, font=F["mono_lg"], fill=ACCENT2)
                if tp2 >= 1.0 and te > 2.5:
                    op2 = eo(min((te - 2.5) / 0.4, 1))
                    oc = tuple(int(ACCENT[j] * op2) for j in range(3))
                    d.text((bx2 + 20, dy2 + 90), "Cloning repo, installing deps, launching...", font=F["mono"], fill=oc)
                    d.text((bx2 + 20, dy2 + 115), "Server running on http://localhost:5050", font=F["mono"], fill=tuple(int(ACCENT2[j] * op2) for j in range(3)))
                    d.text((bx2 + bw2 - 50, dy2 + 115), "OK", font=F["mono"], fill=tuple(int(ACCENT2[j] * op2) for j in range(3)))
        if t >= 5.58:
            ap = eob(min((t - 5.58) / 0.4, 1))
            uy = 460 + int(25 * (1 - ap))
            url = "github.com/CustomerNode/VibeNode"
            bb = d.textbbox((0, 0), url, font=F["h3"])
            utw = bb[2] - bb[0]
            gp = min((t - 5.58) / 1.0, 1)
            if gp > 0:
                radial_glow(img, W // 2, uy + 20, int(300 * gp), ACCENT, 0.08 * gp)
                d = ImageDraw.Draw(img)
            uc = tuple(int(BG2[j] + (ACCENT[j] - BG2[j]) * ap) for j in range(3))
            tc(d, url, uy, F["h3"], uc)
            if ap > 0.5:
                ulp = eo((ap - 0.5) * 2)
                ulw = int(utw * ulp)
                d.line([((W - ulw) // 2, uy + 42), ((W + ulw) // 2, uy + 42)], fill=ACCENT, width=2)
        if t >= 7.56:
            bw3, bh3 = 220, 48
            ttw = len(badges) * bw3 + (len(badges) - 1) * 30
            sx = (W - ttw) // 2
            sy = 540
            for i, (bt, label, color) in enumerate(badges):
                if t < bt:
                    continue
                el = t - bt
                ap = eob(min(el / 0.35, 1))
                bx3 = sx + i * (bw3 + 30)
                by3 = sy + int(20 * (1 - ap))
                bg = tuple(color[j] // 6 for j in range(3))
                d.rounded_rectangle([bx3, by3, bx3 + bw3, by3 + bh3], radius=24, fill=bg)
                d.rounded_rectangle([bx3, by3, bx3 + bw3, by3 + bh3], radius=24, outline=tuple(int(color[j] * ap) for j in range(3)), width=2)
                bb = d.textbbox((0, 0), label, font=F["bodyb"])
                lw3 = bb[2] - bb[0]
                lh = bb[3] - bb[1]
                d.text((bx3 + (bw3 - lw3) // 2, by3 + (bh3 - lh) // 2 - 2), label, font=F["bodyb"], fill=tuple(int(BG2[j] + (WHITE[j] - BG2[j]) * ap) for j in range(3)))
        # "Built by" line — use F["h3"] not F["small"]
        if t >= 10.45:
            animated_caption(d, "Built by CustomerNode and Claude Code", 620, 10.45, t, F["h3"], GRAY)
        # Closing "VibeNode" logo — use F["mega"] with stronger glow
        if t >= dur - 4.0:
            lel = t - (dur - 4.0)
            lp = eo(min(lel / 1.5, 1))
            ly = 700 + int(30 * (1 - lp))
            # Calculate fade-to-black for last 2 seconds (60 frames)
            fade_start = dur - 2.0
            fade_progress = max(0, min((t - fade_start) / 2.0, 1)) if t >= fade_start else 0
            # Logo glows brighter as it fades — afterimage effect
            glow_boost = 1.0 + fade_progress * 1.5
            if lp > 0.2:
                # Stronger glow — larger radius and higher intensity, boosted during fade
                radial_glow(img, W // 2, ly + 40, int(450 * lp), ACCENT, min(0.35, 0.15 * lp * glow_boost))
                radial_glow(img, W // 2, ly + 40, int(250 * lp), ACCENT, min(0.25, 0.10 * lp * glow_boost))
                d = ImageDraw.Draw(img)
            # Extra text glow layers — intensify during fade
            text = "VibeNode"
            bb = d.textbbox((0, 0), text, font=F["mega"])
            tw2 = bb[2] - bb[0]
            tx = (W - tw2) // 2
            glow_layers = int(6 + 4 * fade_progress)  # more glow layers during fade
            for layer in range(glow_layers, 0, -1):
                ga = int((20 + 30 * fade_progress) * lp * (1 - layer / (glow_layers + 1)))
                gc = (min(255, ACCENT[0] // 4 + ga), min(255, ACCENT[1] // 4 + ga), min(255, ACCENT[2] // 3 + ga))
                for ox in [layer * 3, -layer * 3]:
                    d.text((tx + ox, ly), text, font=F["mega"], fill=gc)
            logo_brightness = min(1.0, lp * glow_boost)
            lc = tuple(min(255, int(BG2[j] + (ACCENT[j] - BG2[j]) * logo_brightness)) for j in range(3))
            d.text((tx + 3, ly + 4), text, font=F["mega"], fill=(0, 0, 0))
            d.text((tx, ly), text, font=F["mega"], fill=lc)
        if t >= 8.0:
            bp = eo(min((t - 8.0) / 2.0, 1))
            pulse = 0.7 + 0.3 * math.sin(t * 1.5)
            glow_line(d, H - 50, bp * pulse, ACCENT, mw=int(W * 0.85))
        # Final 2-second fade to black (last 60 frames)
        fade_start = dur - 2.0
        if t >= fade_start:
            fade_p = min((t - fade_start) / 2.0, 1)
            # Darken entire frame progressively
            dark_alpha = int(255 * fade_p)
            if dark_alpha > 0:
                overlay = Image.new("RGBA", (W, H), (0, 0, 0, dark_alpha))
                result = Image.alpha_composite(img.convert("RGBA"), overlay)
                img.paste(result.convert("RGB"))
                d = ImageDraw.Draw(img)
    return build_frames(draw, dur)

def crossfade_frames(frames_a, frames_b, n_frames):
    """Blend last n frames of A with first n frames of B."""
    out = []
    for i in range(n_frames):
        alpha = i / max(n_frames - 1, 1)
        a = frames_a[-(n_frames - i)]
        b = frames_b[i]
        blended = ((1 - alpha) * a.astype(np.float32) + alpha * b.astype(np.float32)).astype(np.uint8)
        out.append(blended)
    return out


def build():
    from moviepy import (ImageSequenceClip, AudioFileClip, CompositeAudioClip,
                         concatenate_audioclips)

    if os.path.exists(STATUS_FILE):
        os.remove(STATUS_FILE)

    # Launch splash
    splash = None
    try:
        splash_py = os.path.join(BASE, "render_splash.py")
        if os.path.exists(splash_py):
            pw = sys.executable.replace("python.exe", "pythonw.exe")
            if not os.path.exists(pw): pw = sys.executable
            splash = subprocess.Popen([pw, splash_py, STATUS_FILE],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass

    print("=" * 60)
    print("  VibeNode Explainer -- Premium Studio Edition")
    print("=" * 60)

    # Audio durations
    af = ["01_hook.mp3", "02_problem.mp3", "03_sessions.mp3",
          "04_workflow.mp3", "05_workforce.mp3", "06_impact.mp3", "07_cta.mp3"]
    aclips = []
    durs = []
    for f in af:
        c = AudioFileClip(os.path.join(AUDIO_DIR, f))
        aclips.append(c)
        d = c.duration + 0.6  # tight padding
        durs.append(d)
        print(f"  {f}: {c.duration:.1f}s -> {d:.1f}s")
    total = sum(durs)
    print(f"\n  Total: {total:.1f}s ({total/60:.1f}m)")

    # Generate music + SFX
    print("\n  Generating music...")
    status("AUDIO")
    music_path = os.path.join(AUDIO_DIR, "ambient_bg.wav")
    generate_music(total + 3, music_path)
    whoosh_path = os.path.join(AUDIO_DIR, "whoosh.wav")
    generate_whoosh(whoosh_path)
    print("  Done.")


    # ── Build scenes in parallel, frames to disk ────────────────────
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import glob as _glob
    import shutil as _sh

    builders = [
        ("Hook", scene_hook), ("Problem", scene_problem),
        ("Sessions", scene_sessions), ("Workflow", scene_workflow),
        ("Workforce", scene_workforce), ("Impact", scene_impact),
        ("CTA", scene_cta),
    ]
    cf_n = int(CROSSFADE * FPS)
    scene_fcounts = [int(d * FPS) for d in durs]
    total_out = sum(scene_fcounts) - (len(builders) - 1) * cf_n

    tmpdir = tempfile.mkdtemp(prefix="vn_render_")
    print("\n  Temp dir: %s" % tmpdir)
    print("  Target: ~%d frames (%.1fs)" % (total_out, total_out / FPS))

    # Create per-scene subdirs
    scene_dirs = []
    for i in range(len(builders)):
        sd = os.path.join(tmpdir, "s%d" % i)
        os.makedirs(sd)
        scene_dirs.append(sd)

    def _build_scene(args):
        idx, name, builder_fn, dur, out_dir = args
        _tls.frame_dir = out_dir
        _tls.scene_idx = idx
        status("PSTART:%d:%s" % (idx, name))
        paths = builder_fn(dur)  # writes JPEGs to out_dir via build_frames
        status("PDONE:%d:%s" % (idx, name))
        return idx, paths

    n_workers = 7  # all scenes in parallel
    print("\n  Building %d scenes in parallel (%d workers)..." % (len(builders), n_workers))

    all_scene_paths = [None] * len(builders)
    tasks = [(i, name, fn, d, scene_dirs[i])
             for i, ((name, fn), d) in enumerate(zip(builders, durs))]

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {pool.submit(_build_scene, t): t for t in tasks}
        done_count = 0
        for fut in as_completed(futures):
            idx, paths = fut.result()
            all_scene_paths[idx] = paths
            done_count += 1
            name = builders[idx][0]
            print("  [%d/7] %s done (%d frames)" % (done_count, name, len(paths)))

    # ── Crossfade + assemble final frame list ─────────────────────────
    print("\n  Applying crossfades...")
    status("SCENE:7/7:Crossfading")
    cf_dir = os.path.join(tmpdir, "crossfades")
    os.makedirs(cf_dir)

    def _load_frame(item):
        """Load a frame whether it's a file path string or numpy array."""
        if isinstance(item, np.ndarray):
            return item
        return np.array(Image.open(item))

    def _save_frame(arr, path):
        """Save numpy array and return the path."""
        Image.fromarray(arr).save(path, "JPEG", quality=95)
        return path

    def _ensure_path(item, idx_ref):
        """If item is numpy array, save to disk and return path. If string path, return as-is."""
        if isinstance(item, np.ndarray):
            p = os.path.join(cf_dir, "np_%06d.jpg" % idx_ref[0])
            idx_ref[0] += 1
            Image.fromarray(item).save(p, "JPEG", quality=95)
            return p
        return item

    np_idx = [0]
    final_paths = []
    for i in range(len(builders)):
        paths = all_scene_paths[i]
        # Convert any numpy arrays to disk files
        paths = [_ensure_path(p, np_idx) for p in paths]
        all_scene_paths[i] = paths

        if i == 0:
            final_paths.extend(paths[:-cf_n])
        else:
            prev_paths = all_scene_paths[i - 1]
            for k in range(cf_n):
                alpha = k / max(cf_n - 1, 1)
                a = np.array(Image.open(prev_paths[-(cf_n - k)]))
                b = np.array(Image.open(paths[k]))
                blended = ((1 - alpha) * a.astype(np.float32) +
                           alpha * b.astype(np.float32)).astype(np.uint8)
                bp = os.path.join(cf_dir, "cf_%d_%03d.jpg" % (i, k))
                Image.fromarray(blended).save(bp, "JPEG", quality=95)
                final_paths.append(bp)

            if i < len(builders) - 1:
                final_paths.extend(paths[cf_n:-cf_n])
            else:
                final_paths.extend(paths[cf_n:])

    print("  Final sequence: %d frames (%.1fs)" % (len(final_paths), len(final_paths) / FPS))

    # ── Video from frame paths ────────────────────────────────────────
    video = ImageSequenceClip(final_paths, fps=FPS)

    # ── Audio mix ─────────────────────────────────────────────────────
    print("  Building audio mix...")
    status("AUDIO")
    from moviepy import AudioClip
    voice_parts = []
    for i, ac in enumerate(aclips):
        pad_dur = durs[i]
        if ac.duration < pad_dur:
            silence = AudioClip(lambda t: [0], duration=pad_dur - ac.duration, fps=44100)
            silence = silence.with_fps(44100)
            padded = concatenate_audioclips([ac, silence])
        else:
            padded = ac.subclipped(0, pad_dur)
        voice_parts.append(padded)
    voice = concatenate_audioclips(voice_parts)
    if voice.duration > video.duration:
        voice = voice.subclipped(0, video.duration)

    music = AudioFileClip(music_path)
    if music.duration > video.duration:
        music = music.subclipped(0, video.duration)

    final_audio = CompositeAudioClip([voice, music])
    video = video.with_audio(final_audio)

    # ── Render ────────────────────────────────────────────────────────
    print("\n  Encoding to %s..." % OUTPUT)
    status("ENCODING")

    video.write_videofile(
        OUTPUT, fps=FPS, codec="libx264", audio_codec="aac",
        bitrate="8000k", preset="medium", threads=4, logger="bar",
    )

    status("DONE")
    sz = os.path.getsize(OUTPUT) / 1024 / 1024
    print("\n  Done! %.1f MB | %.1fs | %dx%d" % (sz, video.duration, W, H))

    # Cleanup temp dir
    try:
        _sh.rmtree(tmpdir)
        print("  Cleaned up temp dir")
    except Exception:
        pass

    # Don't kill splash
    def _cleanup():
        time.sleep(10)
        if os.path.exists(STATUS_FILE):
            os.remove(STATUS_FILE)
    threading.Thread(target=_cleanup, daemon=True).start()


if __name__ == "__main__":
    build()
