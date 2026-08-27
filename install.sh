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
    python3-cryptography

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
    geopy
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

# --- Systemd services ---
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

# --- Install rBrowser ---
echo "Installing rBrowser..."
if [ ! -d "/root/rBrowser" ]; then
    git clone https://github.com/fr33n0w/rBrowser.git /root/rBrowser
    "$INSTALL_DIR/.venv/bin/pip" install waitress --quiet
    echo "  [OK] rBrowser cloned"
else
    echo "  [SKIP] rBrowser already installed"
fi

if [ ! -f /etc/systemd/system/rbrowser.service ]; then
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
    echo "  [OK] rbrowser.service installed"
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
