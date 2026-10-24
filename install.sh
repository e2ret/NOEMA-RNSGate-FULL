#!/bin/bash
# NOEMA RNSGate FULL install script
# Supports: Debian / Ubuntu / Armbian

set -e

REPO_URL="https://github.com/e2ret/NOEMA-RNSGate-FULL.git"
INSTALL_DIR="$HOME/NOEMA-RNSGate-FULL"
RNS_CONFIG_DIR="$HOME/.reticulum"
LXMF_TOOLS_DIR="$HOME/lxmf-tools"
PYTHON="$INSTALL_DIR/.venv/bin/python3"
CURRENT_USER="${SUDO_USER:-$USER}"
[ "$EUID" -eq 0 ] && CURRENT_USER=root
CURRENT_HOME=$(eval echo "~$CURRENT_USER")

echo "========================================"
echo "  NOEMA RNSGate FULL Installer"
echo "========================================"
echo ""

# --- Check root ---
if [ "$EUID" -ne 0 ]; then
    echo "[ERROR] Please run as root: sudo bash install.sh"
    exit 1
fi

# --- sudo (absent on minimal Debian) ---
# We run as root, but this script and the dashboard (restart/reboot buttons, i2pd, etc.)
# call `sudo`, so make sure it exists before anything else uses it.
if ! command -v sudo >/dev/null 2>&1; then
    echo "[0/7] Installing sudo (missing on minimal Debian)..."
    apt-get update -qq
    apt-get install -y sudo
fi

# Never let git stop to ask for a username/password: if any repository this script
# clones is removed or made private, GitHub answers with a login prompt and the install hangs.
export GIT_TERMINAL_PROMPT=0

# --- Journald log size limit ---
echo "[1/7] Limiting systemd journal size..."
if ! grep -q '^SystemMaxUse=' /etc/systemd/journald.conf 2>/dev/null; then
    sed -i 's/#SystemMaxUse=/SystemMaxUse=200M/' /etc/systemd/journald.conf
    systemctl restart systemd-journald
    echo "      journald capped at 200M."
fi

# --- Logrotate for tcp_watchdog log ---
if [ ! -f /etc/logrotate.d/noema_tcp_watchdog ]; then
    cat > /etc/logrotate.d/noema_tcp_watchdog << 'LOGROTATEEOF'
/var/log/noema_tcp_watchdog.log {
    weekly
    rotate 4
    missingok
    notifempty
    compress
    delaycompress
    su root root
}
LOGROTATEEOF
    echo "      Configured logrotate for tcp_watchdog log."
fi

# --- System dependencies ---
echo "[1/7] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    gcc \
    python3-dev \
    libffi-dev \
    python3-cffi \
    python3-cryptography \
    bluez \
    rfkill \
    python3-dbus \
    python3-gi \
    iputils-ping

# --- BLE (RNode over Bluetooth) setup ---
echo "[1b/7] Enabling BlueZ experimental mode (needed for BLE RNode support)..."
BLUEZ_BIN=$(command -v bluetoothd || echo "/usr/libexec/bluetooth/bluetoothd")
[ -x "$BLUEZ_BIN" ] || BLUEZ_BIN="/usr/lib/bluetooth/bluetoothd"
mkdir -p /etc/systemd/system/bluetooth.service.d
cat > /etc/systemd/system/bluetooth.service.d/override.conf << EOF
[Service]
ExecStart=
ExecStart=$BLUEZ_BIN -E
EOF
systemctl daemon-reload
systemctl restart bluetooth 2>/dev/null || echo "      [WARN] Could not restart bluetooth.service — no Bluetooth adapter on this machine?"

# --- Network resilience ---
echo "[1c/7] Network resilience..."

# 1) Don't let unattended-upgrades/needrestart restart networking.
#    After a library update (e.g. liblzma) needrestart restarts every service
#    using it, including NetworkManager/wpa_supplicant — Wi-Fi may not come back.
if [ -d /etc/needrestart ]; then
    mkdir -p /etc/needrestart/conf.d
    cat > /etc/needrestart/conf.d/noema.conf << 'NREOF'
$nrconf{override_rc}{qr(^NetworkManager)} = 0;
$nrconf{override_rc}{qr(^wpa_supplicant)} = 0;
$nrconf{override_rc}{qr(^systemd-networkd)} = 0;
NREOF
    echo "      needrestart: network services excluded from auto-restart."
fi

# 2) Wi-Fi watchdog — only if the default route goes through a wireless interface.
DEF_IF=$(ip route 2>/dev/null | awk '/^default/ {print $5; exit}')
if [ -n "$DEF_IF" ] && [ -d "/sys/class/net/$DEF_IF/wireless" ]; then
    cat > /usr/local/bin/noema-wifi-watchdog.sh << 'WDEOF'
#!/bin/sh
# NOEMA network watchdog.
#   1. Gateway unreachable for ~3 min  -> restart the network stack that is actually running.
#   2. Still unreachable ~90 s later   -> reboot the machine (at most once per 30 min).
STATE_DIR=/var/lib/noema-wifi-watchdog
TAG=noema-wifi-watchdog
mkdir -p "$STATE_DIR"

gateway_ok() {
    GW=$(ip route | awk '/^default/ {print $3; exit}')
    [ -n "$GW" ] && ping -c1 -W5 "$GW" >/dev/null 2>&1
}

for i in 1 2 3; do
    gateway_ok && exit 0
    sleep 60
done

logger -t "$TAG" "gateway ${GW:-none} unreachable for ~3 min, restarting the network stack"
if systemctl is-active --quiet NetworkManager; then
    systemctl restart NetworkManager
else
    # Plain wpa_supplicant (e.g. wpa_supplicant@wlan0 on Armbian/TV boxes) + systemd-networkd
    for u in $(systemctl list-units --state=active --plain --no-legend 'wpa_supplicant*.service' | awk '{print $1}'); do
        systemctl restart "$u"
    done
    systemctl is-active --quiet systemd-networkd && systemctl restart systemd-networkd
fi

sleep 90
if gateway_ok; then
    logger -t "$TAG" "network is back after the restart"
    exit 0
fi

# Last resort: a wedged Wi-Fi driver is often only cured by a reboot.
NOW=$(date +%s)
LAST=$(cat "$STATE_DIR/last_reboot" 2>/dev/null || echo 0)
if [ $((NOW - LAST)) -gt 1800 ]; then
    echo "$NOW" > "$STATE_DIR/last_reboot"
    logger -t "$TAG" "still unreachable after restarting the network, rebooting"
    sync
    systemctl reboot
else
    logger -t "$TAG" "still unreachable, but rebooted less than 30 min ago — not rebooting again"
fi
WDEOF
    chmod +x /usr/local/bin/noema-wifi-watchdog.sh
    cat > /etc/systemd/system/noema-wifi-watchdog.service << 'WDEOF'
[Unit]
Description=NOEMA network watchdog
[Service]
Type=oneshot
ExecStart=/usr/local/bin/noema-wifi-watchdog.sh
WDEOF
    cat > /etc/systemd/system/noema-wifi-watchdog.timer << 'WDEOF'
[Unit]
Description=NOEMA network watchdog every 5 min
[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
WDEOF
    # Replace a hand-made "wifi-watchdog" from earlier manual setups, if any
    systemctl disable --now wifi-watchdog.timer 2>/dev/null || true
    rm -f /etc/systemd/system/wifi-watchdog.timer /etc/systemd/system/wifi-watchdog.service /usr/local/bin/wifi-watchdog.sh
    systemctl daemon-reload
    # Non-critical: a failure here must not abort the whole install (set -e)
    if systemctl enable --now noema-wifi-watchdog.timer; then
        echo "      Default route via Wi-Fi ($DEF_IF) — watchdog enabled."
    else
        echo "      [WARN] Could not enable the Wi-Fi watchdog timer."
    fi
else
    echo "      Default route via ${DEF_IF:-unknown} (not Wi-Fi) — watchdog skipped."
fi

# 2b) Hardware watchdog: if the whole machine hangs (e.g. a wedged Wi-Fi driver on a TV box),
#     systemd keeps feeding the board's watchdog; when it stops, the board resets itself.
#     Only if the board has one and nothing else (e.g. the `watchdog` daemon) already uses it.
if [ -e /dev/watchdog ] && ! systemctl is-active --quiet watchdog 2>/dev/null \
   && ! grep -qs '^RuntimeWatchdogSec=[1-9]' /etc/systemd/system.conf /etc/systemd/system.conf.d/*.conf 2>/dev/null; then
    mkdir -p /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/noema-watchdog.conf << 'WDEOF'
[Manager]
RuntimeWatchdogSec=60
WDEOF
    echo "      Hardware watchdog enabled (takes effect after the next reboot)."
fi

# 3) Leftover standalone rnsd (e.g. from NOEMA Lite). In FULL the dashboard owns
#    the Reticulum instance — a separate rnsd conflicts or crash-loops.
if systemctl list-unit-files rnsd.service 2>/dev/null | grep -q '^rnsd.service'; then
    systemctl disable --now rnsd.service 2>/dev/null || true
    echo "      [FIX] Disabled leftover rnsd.service (FULL dashboard owns the RNS instance)."
fi

# --- Clone or update NOEMA FULL ---
echo "[2/7] Getting NOEMA RNSGate FULL..."
if [ -d "$INSTALL_DIR" ]; then
    echo "      Directory $INSTALL_DIR already exists, pulling latest..."
    git -C "$INSTALL_DIR" pull
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# --- Install i2pd ---
echo "[2b] Installing i2pd..."
ARCH=$(uname -m)

# Detect distro so we don't force an Ubuntu-jammy-built .deb onto Debian
# (different libc/OpenSSL ABI — can fail to install, or install broken).
DISTRO_ID="unknown"
if [ -f /etc/os-release ]; then
    DISTRO_ID=$(. /etc/os-release && echo "$ID")
fi

install_i2pd_from_apt() {
    apt-get update -qq 2>/dev/null || true
    apt-get install -y i2pd 2>/dev/null
}

install_i2pd_from_github() {
    echo "      [INFO] Downloading i2pd from GitHub releases..."
    local ver="2.53.1"
    local url
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
        url="https://github.com/PurpleI2P/i2pd/releases/download/${ver}/i2pd_${ver}_arm64.deb"
    elif [ "$ARCH" = "armv7l" ] || [ "$ARCH" = "armhf" ]; then
        url="https://github.com/PurpleI2P/i2pd/releases/download/${ver}/i2pd_${ver}_armhf.deb"
    else
        url="https://github.com/PurpleI2P/i2pd/releases/download/${ver}/i2pd_${ver}_amd64.deb"
    fi
    if wget -q --timeout=60 -O /tmp/i2pd_gh.deb "$url" 2>/dev/null; then
        dpkg -i /tmp/i2pd_gh.deb 2>/dev/null || apt-get install -f -y 2>/dev/null || true
        rm -f /tmp/i2pd_gh.deb
        echo "      i2pd installed from GitHub."
    else
        echo "      [WARN] i2pd not available, skipping. Install manually later."
    fi
}

if [ "$DISTRO_ID" = "debian" ]; then
    # Debian ships i2pd in its own repos — use it directly, don't touch
    # the Ubuntu-jammy-built bundle at all.
    echo "      Debian detected — installing i2pd from the distro repository..."
    if install_i2pd_from_apt; then
        echo "      i2pd installed from apt."
    else
        echo "      [INFO] apt install failed, falling back to GitHub release..."
        install_i2pd_from_github
    fi
else
    # Ubuntu / Armbian / other — prefer the bundled jammy package (fast,
    # works offline), then fall back to apt / i2pd's own repo / GitHub.
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
        I2PD_DEB="$INSTALL_DIR/packages/i2pd_2.61.0-1jammy1_arm64.deb"
    elif [ "$ARCH" = "armv7l" ] || [ "$ARCH" = "armhf" ]; then
        I2PD_DEB="$INSTALL_DIR/packages/i2pd_2.61.0-1jammy1_armhf.deb"
    else
        I2PD_DEB="$INSTALL_DIR/packages/i2pd_2.61.0-1jammy1_amd64.deb"
    fi

    if [ -f "$I2PD_DEB" ]; then
        sudo dpkg -i "$I2PD_DEB" 2>/dev/null || true
        # dpkg -i can exit non-zero here even though it unpacked the package
        # fine — jammy's i2pd .deb pulls in libboost-program-options1.74.0
        # and libminiupnpc17, which aren't installed by this point, so dpkg
        # reports "dependency problems" and returns non-zero regardless of
        # whether the unpack itself succeeded. Always run --fix-broken to
        # pull in whatever's missing and finish configuring the package.
        sudo apt-get install -f -y -qq 2>/dev/null || true
    fi

    if command -v i2pd >/dev/null 2>&1; then
        echo "      i2pd installed from bundle."
    else
        echo "      [INFO] Bundle unavailable or failed, trying apt..."
        if ! install_i2pd_from_apt; then
            echo "      [INFO] apt failed, trying i2pd's own repository..."
            I2PD_APT_OK=0
            if wget -q --timeout=15 -O /tmp/add_i2pd_repo.sh https://repo.i2pd.xyz/.help/add_repo 2>/dev/null; then
                bash /tmp/add_i2pd_repo.sh 2>/dev/null || true
                install_i2pd_from_apt && I2PD_APT_OK=1
            fi
            [ "$I2PD_APT_OK" -eq 0 ] && install_i2pd_from_github
        fi
    fi
fi

if systemctl list-unit-files i2pd.service &>/dev/null; then
    sudo systemctl enable --now i2pd
    echo "      i2pd service enabled."
else
    echo "      [WARN] i2pd service not available, skipping."
fi

# --- Fix i2pd file permissions for backup ---
if [ -f /var/lib/i2pd/reticulum.dat ]; then
    chmod 644 /var/lib/i2pd/reticulum.dat
fi
# Allow root to read all i2pd data for backup
if [ -d /var/lib/i2pd ]; then
    chmod 755 /var/lib/i2pd
fi

# --- Copy logo to dashboard ---
if [ -f "$INSTALL_DIR/docs/logo.png" ] && [ ! -f "$INSTALL_DIR/dashboard/logo.png" ]; then
    cp "$INSTALL_DIR/docs/logo.png" "$INSTALL_DIR/dashboard/logo.png"
    echo "      Logo copied to dashboard."
fi

# --- Install RTL-SDR tools (for the optional SDR Spectrum tab) ---
echo "[2c] Installing RTL-SDR tools (for the SDR Spectrum tab)..."
# Most installs won't have the hardware — this just means the driver is
# already there and working the moment a dongle IS plugged in, instead of a
# manual build step buried in the dashboard's own tab. The SDR tab itself
# stays fully opt-in regardless (nothing auto-starts; Start/Stop is manual).
#
# Using the rtl-sdr-blog fork, not the distro's rtl-sdr package: RTL-SDR
# Blog V4 dongles (R828D tuner) need it specifically — the stock package is
# too old to recognize V4 correctly — and it's a strict superset for older
# R820T2 dongles too, so there's no downside to always building this one.
if command -v rtl_power >/dev/null 2>&1; then
    echo "      rtl_power already installed, skipping."
else
    RTLSDR_BUILD_DIR="$CURRENT_HOME/rtl-sdr-blog"
    if (
        set -e
        sudo apt-get install -y git build-essential cmake libusb-1.0-0-dev pkg-config -qq
        [ -f /etc/modprobe.d/blacklist-rtl.conf ] || echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf >/dev/null
        [ -d "$RTLSDR_BUILD_DIR" ] || git clone --quiet https://github.com/rtlsdrblog/rtl-sdr-blog.git "$RTLSDR_BUILD_DIR"
        mkdir -p "$RTLSDR_BUILD_DIR/build"
        cd "$RTLSDR_BUILD_DIR/build"
        cmake .. -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON >/dev/null
        make -j"$(nproc)" >/dev/null
        sudo make install >/dev/null
        sudo ldconfig
        sudo cp -f ../rtl-sdr.rules /etc/udev/rules.d/
        sudo udevadm control --reload-rules
        sudo udevadm trigger
    ); then
        echo "      RTL-SDR tools installed (rtl_power, rtl_test, etc.) — plug in a dongle and use the SDR tab whenever you want."
    else
        echo "      [WARN] RTL-SDR driver build failed (no internet? disk space?) — the SDR tab will show its 'not installed' hint with manual build instructions instead. Not fatal, continuing the rest of the install."
    fi
fi

# --- Install lorarx (LoRa packet detector, for SDR tab's Packet Detect mode) ---
echo "[2d] Installing lorarx (LoRa packet detector)..."
# lorarx (from the dxlAPRS toolchain, GPL, by OE5DXL) does actual LoRa PHY
# demodulation — SF/BW/CR/level/SNR per packet — that rtl_power's plain
# power sweep can't. We build only the `lorarx` make target, not the whole
# dxlAPRS toolchain (which pulls in X11/graphics deps for GUI map tools we
# never use here) — its object list (lorarx.o, fft.o, complex.o + a handful
# of shared utility objects) links against nothing but libm.
if command -v lorarx >/dev/null 2>&1; then
    echo "      lorarx already installed, skipping."
else
    DXLAPRS_BUILD_DIR="$CURRENT_HOME/dxlAPRS"
    if (
        set -e
        sudo apt-get install -y build-essential -qq
        [ -d "$DXLAPRS_BUILD_DIR" ] || git clone --quiet https://github.com/oe5hpm/dxlAPRS.git "$DXLAPRS_BUILD_DIR"
        cd "$DXLAPRS_BUILD_DIR/src"
        make lorarx
        LORARX_BIN=$(find .. -maxdepth 2 -name lorarx -type f | head -1)
        [ -n "$LORARX_BIN" ]
        sudo cp -f "$LORARX_BIN" /usr/local/bin/lorarx
        sudo chmod +x /usr/local/bin/lorarx
    ); then
        echo "      lorarx installed to /usr/local/bin/lorarx."
    else
        echo "      [WARN] lorarx build failed — the SDR tab's Packet Detect mode won't be available (Waterfall mode is unaffected). Not fatal, continuing the rest of the install."
    fi
fi

# --- Python venv ---
echo "[3/7] Setting up Python virtual environment..."

# Ensure Python 3.11+ for lxmfy on Ubuntu 22.04
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MINOR" -lt 11 ]; then
    echo "      Python 3.${PY_MINOR} detected, installing Python 3.11..."
    apt-get install -y python3.11 python3.11-venv python3.11-dev -q
    PYTHON_BIN="python3.11"
else
    PYTHON_BIN="python3"
fi

$PYTHON_BIN -m venv --system-site-packages "$INSTALL_DIR/.venv"
source "$INSTALL_DIR/.venv/bin/activate"

pip install --upgrade pip -q

# On ARM without Rust compiler cbor2>=6 fails — install pure-python fallback first
if ! pip install cbor2 -q 2>/dev/null; then
    echo "      [INFO] cbor2 build failed (no Rust), installing pure-python fallback..."
    pip install "cbor2<5.5" -q || true
fi

pip install \
    rns \
    lxmf \
    lxmfy \
    flask \
    paho-mqtt \
    nomadnet \
    pytz \
    requests \
    geopy \
    bleak \
    pexpect
# Force reinstall rns to ensure the RNS library is properly linked in the venv
# (dashboard.py imports it directly now — there's no separate rnsd process)
pip install --force-reinstall rns -q

echo "      Python dependencies installed."

# --- Reticulum config ---
echo "[4/7] Setting up Reticulum config..."
mkdir -p "$RNS_CONFIG_DIR"

if [ -f "$RNS_CONFIG_DIR/config" ]; then
    echo "      Reticulum config already exists, skipping."
else
    cat > "$RNS_CONFIG_DIR/config" << 'RNSEOF'
[reticulum]
  enable_transport = No
  share_instance = Yes
  shared_instance_port = 37428
  instance_control_port = 37429
  panic_on_interface_error = No

[interfaces]

  [[Default Interface]]
    type = AutoInterface
    enabled = yes

  [[TCP]]
    type = TCPServerInterface
    enabled = true
    port = 4242
    listen_ip = 0.0.0.0
RNSEOF
    echo "      Default Reticulum config created."
fi

# --- MQTT configuration ---
echo ""
echo "========================================"
echo "  MQTT Broker Configuration"
echo "========================================"
echo ""
read -rp "  MQTT broker IP [localhost]: " MQTT_HOST
MQTT_HOST="${MQTT_HOST:-localhost}"
read -rp "  MQTT port [1883]: " MQTT_PORT
MQTT_PORT="${MQTT_PORT:-1883}"
read -rp "  MQTT username [mqtt]: " MQTT_USER
MQTT_USER="${MQTT_USER:-mqtt}"
read -rsp "  MQTT password: " MQTT_PASS
echo ""
echo "      MQTT: $MQTT_USER@$MQTT_HOST:$MQTT_PORT"
echo ""

# --- Nomadnet node name ---
echo ""
echo "========================================"
echo "  Nomadnet Node Name"
echo "========================================"
echo ""
echo "  This name is visible to other peers on the network"
echo "  and included in announces. If you run several gateways,"
echo "  give each one a distinct name so they're easy to tell apart."
echo ""
HOSTNAME_SHORT=$(hostname -s 2>/dev/null || echo "gateway")
read -rp "  Node name [NOEMA RNSGate ($HOSTNAME_SHORT)]: " NN_NODE_NAME
NN_NODE_NAME="${NN_NODE_NAME:-NOEMA RNSGate ($HOSTNAME_SHORT)}"
echo "      Node name: $NN_NODE_NAME"
echo ""
# Escape characters that are special inside a sed replacement (delimiter |, & and \)
NN_NODE_NAME_ESCAPED=$(printf '%s' "$NN_NODE_NAME" | sed 's/[\&|]/\\&/g')

# --- lxmf-tools ---
echo "[5/7] Setting up lxmf-tools..."
mkdir -p "$LXMF_TOOLS_DIR"
mkdir -p /etc/noema
cp "$INSTALL_DIR/lxmf-tools/noema_lxmf_bridge.py" "$LXMF_TOOLS_DIR/noema_lxmf_bridge.py"
if [ -f "$INSTALL_DIR/lxmf-tools/tcp_watchdog.py" ]; then
    cp "$INSTALL_DIR/lxmf-tools/tcp_watchdog.py" "$LXMF_TOOLS_DIR/tcp_watchdog.py"
    echo "      Copied tcp_watchdog.py"
fi

if [ -f "/etc/noema/bridge.cfg" ]; then
    echo "      LXMF Bridge config already exists, skipping."
else
    cat > /etc/noema/bridge.cfg << BRIDGECFG
[lxmf]
display_name = LXMF Bridge

[mqtt]
host = ${MQTT_HOST}
port = ${MQTT_PORT}
client_id = noema_lxmf_bridge
username = ${MQTT_USER}
password = ${MQTT_PASS}

[topics]
send     = message/lxmf/send
receive  = message/lxmf/receive
state    = message/lxmf/rm_state
power    = message/lxmf/power
rm_power = message/lxmf/rm_power
BRIDGECFG
    echo "      Created /etc/noema/bridge.cfg"
fi

# --- Serial port access for RNode ---
if groups "$CURRENT_USER" | grep -qw dialout; then
    echo "      User $CURRENT_USER already in dialout group."
else
    sudo usermod -aG dialout "$CURRENT_USER"
    echo "      Added $CURRENT_USER to dialout group."
    echo "      [!] You need to log out and back in for serial port access."
fi

# --- USB access for an optional RTL-SDR (SDR Spectrum tab) ---
# rtl-sdr's udev rules (stock or the rtl-sdr-blog fork, for V4 dongles) grant
# device access to the plugdev group, not dialout. Not fatal to skip this —
# no dongle means the SDR tab just shows its "not installed" state — but a
# CURRENT_USER outside plugdev with rtl_power actually installed hits a
# confusing silent USB permission error instead of a working tab.
if getent group plugdev >/dev/null; then
    if groups "$CURRENT_USER" | grep -qw plugdev; then
        echo "      User $CURRENT_USER already in plugdev group."
    else
        sudo usermod -aG plugdev "$CURRENT_USER"
        echo "      Added $CURRENT_USER to plugdev group (RTL-SDR USB access, if one is ever plugged in)."
        echo "      [!] You need to log out and back in for this to take effect."
    fi
else
    echo "      [SKIP] plugdev group doesn't exist on this system — if you install RTL-SDR tools later (see the SDR tab), you may need to create it yourself: sudo groupadd plugdev && sudo usermod -aG plugdev $CURRENT_USER"
fi

# --- Systemd services ---

# Self-healing for Reticulum ratchet files. A hard power-off (or a hang) in the middle of a
# write leaves a truncated *.ratchets file, and then dashboard / LXMF bridge / Nomadnet crash
# on every start ("Could not read ratchet file"), looping forever. Before each start this
# script moves unreadable ones aside (*.bad); Reticulum simply creates a new file.
cat > /usr/local/bin/noema-fix-ratchets.py << 'FIXEOF'
#!/usr/bin/env python3
import os
import sys
import time

try:
    from RNS.vendor import umsgpack
except Exception:
    sys.exit(0)

SKIP = {".venv", ".git", "node_modules", "__pycache__"}
root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~")


def unreadable(path):
    try:
        with open(path, "rb") as fh:
            umsgpack.unpackb(fh.read())
        return False
    except Exception:
        return True


for d, dirs, files in os.walk(root):
    dirs[:] = [x for x in dirs if x not in SKIP]
    for f in files:
        if not f.endswith(".ratchets"):
            continue
        p = os.path.join(d, f)
        try:
            # A file another service is writing right now must not be touched.
            if time.time() - os.path.getmtime(p) < 10:
                continue
            if unreadable(p):
                time.sleep(1)
                if unreadable(p):
                    os.rename(p, p + ".bad")
                    print(f"noema-fix-ratchets: moved unreadable {p}")
        except OSError:
            pass
FIXEOF
chmod +x /usr/local/bin/noema-fix-ratchets.py
echo "[6/7] Installing systemd services..."

install_service() {
    local NAME=$1
    local DESC=$2
    local EXEC=$3
    local WORKDIR=$4
    local AFTER=${5:-"network.target"}
    local WAIT_FOR_DASHBOARD=${6:-""}

    local EXECSTARTPRE=""
    if [ "$WAIT_FOR_DASHBOARD" = "wait_dashboard" ]; then
        # Actively poll dashboard's own API until it responds, instead of a
        # fixed sleep. systemd's After=dashboard.service only guarantees the
        # PROCESS was started — not that it finished RNS.Reticulum() init
        # (which must complete before this service can safely become a
        # shared-instance client of it). Up to 30s, 1s apart; if it never
        # comes up the ExecStartPre fails and systemd's Restart=always will
        # retry the whole unit later.
        EXECSTARTPRE="ExecStartPre=/bin/bash -c 'for i in \$(seq 1 30); do curl -sf http://localhost:8081/api/version >/dev/null 2>&1 && exit 0; sleep 1; done; echo \"dashboard not ready after 30s\"; exit 1'"
    fi

    sudo tee "/etc/systemd/system/${NAME}.service" > /dev/null << EOF
[Unit]
Description=${DESC}
After=${AFTER}

[Service]
User=${CURRENT_USER}
WorkingDirectory=${WORKDIR}
Environment=HOME=${CURRENT_HOME}
Environment=LXMFY_LANDLOCK=0
Environment=LXMF_TOOLS_DIR=${LXMF_TOOLS_DIR}
ExecStartPre=-${PYTHON} /usr/local/bin/noema-fix-ratchets.py ${CURRENT_HOME}
${EXECSTARTPRE}
ExecStart=${EXEC}
StandardOutput=journal
StandardError=journal
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    echo "      Installed ${NAME}.service"
}

install_service "dashboard" \
    "NOEMA FULL Dashboard (Reticulum instance owner)" \
    "$PYTHON $INSTALL_DIR/dashboard/app.py" \
    "$INSTALL_DIR/dashboard" \
    "network.target"

install_service "noema_lxmf_bridge" \
    "NOEMA LXMF Bridge" \
    "$PYTHON -u $LXMF_TOOLS_DIR/noema_lxmf_bridge.py" \
    "$LXMF_TOOLS_DIR" \
    "network.target dashboard.service" \
    "wait_dashboard"

SERVICES_LIST="dashboard noema_lxmf_bridge"

# --- Nomadnet node ---
echo "[7b] Setting up Nomadnet node..."
NOMADNET_PAGES="$CURRENT_HOME/.nomadnetwork/storage/pages"
mkdir -p "$NOMADNET_PAGES"

if [ ! -f "$NOMADNET_PAGES/index.mu" ]; then
    cat > "$NOMADNET_PAGES/index.mu" << 'MUEOF'
> NOEMA RNSGate

`c`fNOEMA RNSGate`f

`c`bReticulum Mesh Gateway`b

---

`c`[Подключение`]

TCP: <IP>:4242

---

`c`[Контакт`]

LXMF Bridge: <адрес из дашборда>
Group Chat:  <адрес из дашборда>

---

`c`aNOEMA — передавай смысл, а не просто данные`a
MUEOF
    echo "      Default index.mu created."
fi

# --- Nomadnet ChatRoom ---
echo "[7c] Setting up Nomadnet ChatRoom..."
cd /tmp && git clone https://github.com/fr33n0w/thechatroom.git thechatroom_install 2>/dev/null || true
if [ -d "/tmp/thechatroom_install" ]; then
    cp /tmp/thechatroom_install/nomadnet.mu "$NOMADNET_PAGES/"
    cp /tmp/thechatroom_install/meshchat.mu "$NOMADNET_PAGES/"
    cp /tmp/thechatroom_install/fullchat.mu "$NOMADNET_PAGES/"
    cp /tmp/thechatroom_install/last100.mu  "$NOMADNET_PAGES/"
    cp /tmp/thechatroom_install/chat_log.json "$NOMADNET_PAGES/"
    cp /tmp/thechatroom_install/topic.json "$NOMADNET_PAGES/"
    cp /tmp/thechatroom_install/emoticon.txt "$NOMADNET_PAGES/"
    chmod +x "$NOMADNET_PAGES/nomadnet.mu" "$NOMADNET_PAGES/meshchat.mu" \
              "$NOMADNET_PAGES/fullchat.mu" "$NOMADNET_PAGES/last100.mu"
    # Fix emoticon filename
    ln -sf "$NOMADNET_PAGES/emoticon.txt" "$NOMADNET_PAGES/emoticons.txt"
    rm -rf /tmp/thechatroom_install
    echo "      ChatRoom installed."
else
    echo "      [WARN] Could not clone thechatroom, skipping."
fi
cd "$INSTALL_DIR"

# Enable nomadnet node in config — init first run to create config
NOMADNET_CONFIG="$CURRENT_HOME/.nomadnetwork/config"
if [ ! -f "$NOMADNET_CONFIG" ]; then
    echo "      Initializing Nomadnet config..."
    timeout 5 "$INSTALL_DIR/.venv/bin/nomadnet" --daemon --rnsconfig "$RNS_CONFIG_DIR" 2>/dev/null || true
    sleep 2
fi
if [ -f "$NOMADNET_CONFIG" ]; then
    sed -i "s/^enable_node = no/enable_node = yes/" "$NOMADNET_CONFIG"
    sed -i "s|^node_name = None|node_name = ${NN_NODE_NAME_ESCAPED}|" "$NOMADNET_CONFIG"
    echo "      Nomadnet node enabled in config."
fi

install_service "nomadnet" \
    "Nomadnet Node" \
    "$INSTALL_DIR/.venv/bin/nomadnet --daemon --rnsconfig $RNS_CONFIG_DIR" \
    "$CURRENT_HOME" \
    "network.target dashboard.service" \
    "wait_dashboard"

# Add ExecStartPost for recalc_nn_addr.py
sudo sed -i "/ExecStart=.*/a ExecStartPost=/bin/bash -c 'sleep 10 && $PYTHON $INSTALL_DIR/recalc_nn_addr.py'" /etc/systemd/system/nomadnet.service

SERVICES_LIST="$SERVICES_LIST nomadnet"

sudo systemctl daemon-reload
sudo systemctl enable $SERVICES_LIST
echo "      Services enabled."
echo "      Starting services..."
sudo systemctl start $SERVICES_LIST

# --- Install rBrowser (optional extra) ---
# rBrowser ships inside this repository (rBrowser/): the upstream project's GitHub repo was removed.
echo "Installing rBrowser..."
# An empty leftover dir (e.g. from an interrupted clone) would block the copy below.
[ -d /root/rBrowser ] && [ ! -f /root/rBrowser/rBrowser.py ] && rmdir /root/rBrowser 2>/dev/null || true
if [ -f "/root/rBrowser/rBrowser.py" ]; then
    echo "  [SKIP] rBrowser already installed"
elif [ -d "/root/rBrowser" ]; then
    echo "  [WARN] /root/rBrowser exists but has no rBrowser.py — leaving it alone, skipping rBrowser."
elif [ -f "$INSTALL_DIR/rBrowser/rBrowser.py" ]; then
    cp -a "$INSTALL_DIR/rBrowser" /root/rBrowser
    echo "  [OK] rBrowser installed from this repository"
else
    echo "  [WARN] rBrowser/ is not in the repository — skipping rBrowser (everything else is installed)."
fi

if [ -f /root/rBrowser/rBrowser.py ]; then
    "$INSTALL_DIR/.venv/bin/pip" install waitress --quiet
cat > /etc/systemd/system/rbrowser.service << SVCEOF
[Unit]
Description=rBrowser Nomadnet Browser
After=network.target dashboard.service
Wants=dashboard.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/rBrowser
ExecStartPre=/bin/bash -c 'for i in \$(seq 1 30); do curl -sf http://localhost:8081/api/version >/dev/null 2>&1 && exit 0; sleep 1; done; echo "dashboard not ready after 30s"; exit 1'
ExecStart=$INSTALL_DIR/.venv/bin/python3 rBrowser.py
Restart=on-failure
RestartSec=5
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable --now rbrowser
echo "  [OK] rbrowser.service installed (ExecStart refreshed to $INSTALL_DIR)"
else
    echo "  [SKIP] rbrowser.service not created (no /root/rBrowser)"
fi

# --- Get LXMF Bridge address ---
echo "Getting LXMF Bridge address..."
sleep 10
LXMF_ADDR=$(journalctl -u noema_lxmf_bridge -n 100 --no-pager 2>/dev/null | grep -oE '[0-9a-f]{32}' | tail -1 || true)
if [ -n "$LXMF_ADDR" ]; then
    echo -n "$LXMF_ADDR" > "$LXMF_TOOLS_DIR/lxmf_address"
    echo "  LXMF Bridge address: $LXMF_ADDR"
else
    echo "  [WARN] Could not get LXMF address, check dashboard after startup"
fi

# --- Calculate Nomadnet node address ---
echo "Calculating Nomadnet node address..."
sleep 10
"$INSTALL_DIR/.venv/bin/python3" "$INSTALL_DIR/recalc_nn_addr.py" || true

# --- Done ---
LXMF_ADDR=$(cat "$LXMF_TOOLS_DIR/lxmf_address" 2>/dev/null || echo "check dashboard")
NN_ADDR=$(cat "$INSTALL_DIR/.nomadnetwork/node_address" 2>/dev/null || echo "check dashboard")
IP=$(hostname -I | awk '{print $1}')

echo ""
echo "========================================"
echo "  Installation complete!"
echo "========================================"
echo ""
echo "  Dashboard          : http://${IP}:8081"
echo "  LXMF Bridge address: $LXMF_ADDR"
echo "  Nomadnet address   : $NN_ADDR"
echo ""
echo "  NOEMA RNSGate dir  : $INSTALL_DIR"
echo "  lxmf-tools         : $LXMF_TOOLS_DIR"
echo "  Reticulum config   : $RNS_CONFIG_DIR/config"
echo "  LXMF Bridge config : /etc/noema/bridge.cfg"
echo "  Python venv        : $INSTALL_DIR/.venv"
echo ""
echo "  Edit Reticulum config:"
echo "    nano $RNS_CONFIG_DIR/config"
echo ""
echo "  Check status:"
echo "    systemctl status $SERVICES_LIST"
echo ""
