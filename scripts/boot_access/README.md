# Boot Access — reach VibeNode from your phone after a reboot, before anyone logs in

## The problem this solves

Mobile Command's reviver guarantees your phone always sees a **Start VibeNode**
page while the computer is up. But there is one window it cannot cover on its
own: **the machine rebooted and nobody has logged in yet.**

- `tailscaled` runs as a *system* service, so your phone still reaches the
  machine — but nothing answers on port 5050, and you get a bare 502.
- The reviver's own autostart mechanisms (Startup folder on Windows, launchd
  agent on macOS, systemd *user* unit on Linux) all fire **at login**, not at
  boot.

If your machine ever reboots unattended (power blip, auto-update, crash), the
phone link is dead until someone physically logs in. The scripts in this folder
close that window.

## What "fixed" looks like

Power the machine off and on. **Without touching it**, your phone shows either
the familiar **Start VibeNode** page (tap → running app) or, on Linux with an
encrypted home, an **Unlock & start** page (type your login password → running
app). One-handed, from anywhere on your tailnet.

## Per-OS: what to run

| OS | Situation | Fix | Script |
|----|-----------|-----|--------|
| **Windows** | Nothing runs before logon | Boot-time Scheduled Task runs the reviver as you, pre-login | `setup_windows_boot.ps1` |
| **Linux**, home **not** encrypted | User units need linger to start at boot | Enable linger (+ re-register the reviver unit) | `setup_linux_boot.sh` |
| **Linux**, **ecryptfs**-encrypted home | Pre-login, your home (unit file *and* reviver.py) is ciphertext — linger can't help | Tiny system-level "unlock & start" page installed **outside** the home | `setup_linux_boot.sh` (auto-detects, uses `prelogin_unlock.py`) |
| **macOS** | LaunchAgents fire at login; FileVault blocks earlier anyway | No safe pre-login path; enable auto-login or accept the window | — (see notes) |

All scripts are idempotent — re-run them freely after moving the checkout.

---

## Windows (`setup_windows_boot.ps1`)

Run **once**, in PowerShell, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File setup_windows_boot.ps1
```

It registers a Scheduled Task that starts `reviver.py` (windowless, via
`pythonw`) **at system startup**, running as your account *whether or not you
are logged on*. Windows requires your account password once, at registration,
to grant that — the script prompts via a standard credential dialog and hands
it straight to the Task Scheduler; **nothing is written to disk**.

After a reboot the reviver is up pre-login, your phone gets the Start page,
and one tap boots VibeNode (which then runs headless until you log in — your
sessions are fully usable from the phone in the meantime).

Gotchas the script handles or warns about:

- **Execution time limit** — Task Scheduler kills tasks after 72 h by default.
  The script sets the limit to *none*.
- **Battery** — by default tasks don't start on battery. The script enables
  battery start (laptops rebooting on battery still recover).
- **Fast Startup** — a Windows "shut down" is really a hibernate, and
  *at-startup* triggers may **not** fire on the next power-on (they always fire
  on Restart). If you want cold-boot coverage on a machine that gets shut down,
  disable Fast Startup (Control Panel → Power Options → "Choose what the power
  buttons do") or run `powercfg /h off`. The script detects and warns.
- **BitLocker** — with the default TPM auto-unlock, boots proceed normally and
  this all works. If your BitLocker requires a PIN at power-on, the machine
  never reaches Windows and no software can help remotely.

Uninstall: `powershell -ExecutionPolicy Bypass -File setup_windows_boot.ps1 -Uninstall`

## Linux (`setup_linux_boot.sh`)

Run **once**, as your normal user, from this folder:

```bash
bash setup_linux_boot.sh
```

It detects your home-directory situation:

**Unencrypted home (most machines):** enables `loginctl enable-linger` so your
systemd user manager — and with it the reviver's user unit — starts at boot,
and (re)registers the reviver unit. Done; no root service, no sudo beyond
linger.

**ecryptfs-encrypted home:** linger is not enough — before login, everything
under your home (including the reviver and its unit file) is ciphertext. The
script offers to install `prelogin_unlock.py` as a small **system** service
under `/opt/vibenode-prelogin/` (one-time sudo). Pre-login it serves an
**"Unlock & start VibeNode"** page on port 5050: you type your login password
on the phone, it unlocks the home via the same stock ecryptfs helpers a
console login uses, pokes your user manager so the reviver loads, launches
VibeNode, and gets out of the way. The moment the home is mounted by any
means, it releases the port and goes dormant.

Security posture of the pre-login page:

- Binds `127.0.0.1` only — remotely reachable *solely* through your existing
  tailnet-only `tailscale serve` HTTPS mapping (WireGuard-encrypted, your
  devices only). Nothing new is exposed.
- The password is piped once to the ecryptfs helpers' stdin — never stored,
  never logged, never in `argv`.
- 5 failed attempts → 15-minute lockout.

Other encryption schemes: **fscrypt** and **systemd-homed** homes have the
same blindness but different unlock plumbing — not covered here (yet). Full-
disk **LUKS** machines prompt at the boot console before the OS is even up;
no userspace service can help with that remotely (look at `dracut-sshd` /
`clevis` if you need it).

Uninstall: `bash setup_linux_boot.sh --uninstall`

## macOS

launchd LaunchAgents (what the reviver installs) run at login. LaunchDaemons
run at boot, but with FileVault enabled (the default) the disk isn't unlocked
until a user authenticates at the pre-boot screen, so a daemon can't reach
your checkout any earlier. Practical options:

1. **Enable automatic login** (System Settings → Users & Groups) — requires
   FileVault off. The machine boots straight to your session and the reviver's
   LaunchAgent covers everything.
2. Accept the window: after an unattended reboot, VibeNode is reachable again
   at your next physical login.

## How this composes with the reviver

Nothing here replaces `reviver.py` — these scripts only get it (or an unlock
page) running in the pre-login window. Once you're logged in, the reviver's
own self-installed mechanisms take over exactly as before. See the big header
comment in `reviver.py` for that design.
