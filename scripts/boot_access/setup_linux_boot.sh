#!/usr/bin/env bash
#
# setup_linux_boot.sh — make VibeNode's phone Start-page survive reboots on
# Linux, even before anyone logs in. See README.md in this folder.
#
# Detects your home-directory situation and does the right thing:
#   * unencrypted home  -> enable linger + (re)register the reviver user unit.
#                          The reviver then starts at boot; nothing else needed.
#   * ecryptfs home     -> the above PLUS install prelogin_unlock.py as a small
#                          SYSTEM service under /opt/vibenode-prelogin (one-time
#                          sudo) that serves an "Unlock & start" page pre-login.
#
# Run as your normal user (NOT root):   bash setup_linux_boot.sh
# Remove everything it installed:       bash setup_linux_boot.sh --uninstall
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
VN_USER="$(id -un)"
VN_UID="$(id -u)"
VN_HOME="$HOME"
INSTALL_DIR="/opt/vibenode-prelogin"
UNIT_PATH="/etc/systemd/system/vibenode-prelogin.service"
SALT="$(printf '%s' "$REPO_ROOT" | md5sum | cut -c1-8)"
REVIVER_UNIT="vibenode-reviver-${SALT}.service"

if [[ "$VN_UID" == "0" ]]; then
  echo "Run this as your normal user, not root (it uses sudo only where needed)." >&2
  exit 1
fi

home_is_ecryptfs() {
  grep -qsE "^[^ ]+ ${VN_HOME} ecryptfs " /proc/mounts && return 0
  [[ -d "/home/.ecryptfs/${VN_USER}" ]] && return 0
  return 1
}

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "Removing pre-login service (sudo)…"
  sudo systemctl disable --now vibenode-prelogin.service 2>/dev/null || true
  sudo rm -f "$UNIT_PATH"
  sudo rm -rf "$INSTALL_DIR"
  sudo systemctl daemon-reload
  echo "Done. (Linger and the reviver user unit were left alone — remove those"
  echo "with 'loginctl disable-linger' / reviver.py --unregister if you want.)"
  exit 0
fi

if [[ ! -f "$REPO_ROOT/reviver.py" ]]; then
  echo "reviver.py not found at $REPO_ROOT — run from inside the VibeNode checkout." >&2
  exit 1
fi

echo "VibeNode checkout : $REPO_ROOT"
echo "User              : $VN_USER (uid $VN_UID)"

# --- Step 1: linger — lets your systemd user manager (and its units) start at
# boot instead of at first login. Harmless if already enabled.
echo
echo "[1/3] Enabling linger (user services may start at boot)…"
loginctl enable-linger "$VN_USER" || sudo loginctl enable-linger "$VN_USER"
echo "      linger: $(loginctl show-user "$VN_USER" 2>/dev/null | grep -i '^Linger=' || echo 'unknown')"

# --- Step 2: make sure the reviver's user unit exists & is enabled (reviver.py
# self-installs it on every run, but do it explicitly so this script is
# sufficient on a fresh clone).
echo
echo "[2/3] Registering the reviver systemd user unit…"
python3 - <<PY
import sys; sys.path.insert(0, "$REPO_ROOT")
import reviver
reviver._register_linux()
print("      unit:", reviver._systemd_unit_name())
PY

# --- Step 3: ecryptfs pre-login coverage (only when needed).
echo
if ! home_is_ecryptfs; then
  echo "[3/3] Home is NOT ecryptfs-encrypted — linger + reviver unit already"
  echo "      cover the pre-login window. You're done."
  echo
  echo "Test it: reboot without logging in, then open VibeNode on your phone."
  exit 0
fi

cat <<EOF
[3/3] Your home is ecryptfs-ENCRYPTED. Pre-login it is ciphertext, so the
      reviver (which lives inside it) cannot run — linger alone won't help.

      Fix: install prelogin_unlock.py as a small SYSTEM service (outside your
      home, under $INSTALL_DIR). Before login it serves an
      "Unlock & start VibeNode" page on the VibeNode port: you type your login
      password on your phone, it unlocks the home with the same stock ecryptfs
      helpers a console login uses, then boots VibeNode. Loopback-only; only
      reachable remotely via your existing tailnet-only 'tailscale serve'.

      This needs one-time sudo (writes /opt + /etc/systemd/system).
EOF
read -r -p "      Install it? [Y/n] " ans
case "${ans:-Y}" in [Yy]*|"") ;; *) echo "Skipped."; exit 0;; esac

for tool in ecryptfs-insert-wrapped-passphrase-into-keyring; do
  command -v "$tool" >/dev/null || {
    echo "Missing '$tool' — install the ecryptfs-utils package first." >&2; exit 1; }
done

sudo mkdir -p "$INSTALL_DIR"
sudo install -o root -g root -m 0755 "$HERE/prelogin_unlock.py" "$INSTALL_DIR/prelogin_unlock.py"

sudo tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=VibeNode pre-login Start page (boot CTA + ecryptfs unlock over Tailscale)
Documentation=file:$INSTALL_DIR/prelogin_unlock.py
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$VN_USER
Group=$(id -gn)
Environment=VN_USER=$VN_USER
Environment=VN_HOME=$VN_HOME
Environment=VN_CHECKOUT=$REPO_ROOT
Environment=VN_REVIVER_UNIT=$REVIVER_UNIT
Environment=VN_RUNTIME_DIR=/run/user/$VN_UID
ExecStart=/usr/bin/python3 $INSTALL_DIR/prelogin_unlock.py
Restart=always
RestartSec=5
# NOTE: no NoNewPrivileges/ProtectHome/PrivateTmp hardening — the service's
# whole job is running the setuid mount.ecryptfs_private helper and making the
# resulting /home mount visible system-wide. NoNewPrivileges silently breaks
# the setuid helper; a private mount namespace hides the unlock from everyone.

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now vibenode-prelogin.service
sleep 2
if systemctl is-active --quiet vibenode-prelogin.service; then
  echo
  echo "Installed and running. While your home is mounted it stays dormant;"
  echo "after an unattended reboot your phone gets the unlock page."
  echo
  echo "Logs   : journalctl -u vibenode-prelogin -f"
  echo "Remove : bash $HERE/setup_linux_boot.sh --uninstall"
  echo
  echo "Test it for real: reboot without logging in, open VibeNode on your"
  echo "phone, and enter your login password."
else
  echo "Service failed to start — check: journalctl -u vibenode-prelogin -n 50" >&2
  exit 1
fi
