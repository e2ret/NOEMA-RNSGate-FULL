#!/bin/bash
# NOEMA RNSGate FULL uninstall script
# Reverses what install.sh set up so you can reinstall cleanly.
#
# Usage:
#   sudo bash uninstall.sh              # stop services, remove units + app dirs;
#                                       # keeps Reticulum / Nomadnet identities
#   sudo bash uninstall.sh --yes        # same, no confirmation prompt
#   sudo bash uninstall.sh --purge      # also delete identities, configs, DBs
#   sudo bash uninstall.sh --purge --yes
#
# Does not remove system packages (python, bluez, i2pd apt package, rtl-sdr, …).

set -euo pipefail

CURRENT_USER="${SUDO_USER:-$USER}"
[ "${EUID:-$(id -u)}" -eq 0 ] && [ -z "${SUDO_USER:-}" ] && CURRENT_USER=root
CURRENT_HOME=$(eval echo "~$CURRENT_USER")
INSTALL_DIR="${NOEMA_INSTALL_DIR:-$CURRENT_HOME/NOEMA-RNSGate-FULL}"
LXMF_TOOLS_DIR="${NOEMA_LXMF_TOOLS_DIR:-$CURRENT_HOME/lxmf-tools}"
RNS_CONFIG_DIR="${NOEMA_RNS_CONFIG_DIR:-$CURRENT_HOME/.reticulum}"

PURGE=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --purge|-p) PURGE=1 ;;
        --yes|-y)   ASSUME_YES=1 ;;
        -h|--help)
            sed -n '2,14p' "$0"
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown option: $arg (try --help)"
            exit 1
            ;;
    esac
done

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "[ERROR] Please run as root: sudo bash uninstall.sh"
    exit 1
fi

SERVICES=(
    wardrive_map
    wardrive_bot
    rbrowser
    nomadnet
    noema_lxmf_bridge
    dashboard
    noema-wifi-watchdog
)

echo "========================================"
echo "  NOEMA RNSGate FULL Uninstaller"
echo "========================================"
echo ""
echo "  Install dir     : $INSTALL_DIR"
echo "  lxmf-tools      : $LXMF_TOOLS_DIR"
echo "  Reticulum dir   : $RNS_CONFIG_DIR"
echo "  Purge identities: $([ "$PURGE" -eq 1 ] && echo YES || echo no — kept for reinstall)"
echo ""

if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Stop NOEMA services and remove the install? [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
fi

echo "[1/5] Stopping and disabling services..."
# Timers first (Wi-Fi watchdog)
systemctl disable --now noema-wifi-watchdog.timer 2>/dev/null || true
systemctl disable --now wifi-watchdog.timer 2>/dev/null || true

for s in "${SERVICES[@]}"; do
    if systemctl list-unit-files "${s}.service" 2>/dev/null | grep -q "^${s}.service"; then
        systemctl disable --now "${s}.service" 2>/dev/null || true
        echo "      stopped ${s}"
    fi
done

echo "[2/5] Removing systemd units..."
rm -f \
    /etc/systemd/system/dashboard.service \
    /etc/systemd/system/noema_lxmf_bridge.service \
    /etc/systemd/system/nomadnet.service \
    /etc/systemd/system/rbrowser.service \
    /etc/systemd/system/wardrive_bot.service \
    /etc/systemd/system/wardrive_map.service \
    /etc/systemd/system/noema-wifi-watchdog.service \
    /etc/systemd/system/noema-wifi-watchdog.timer \
    /etc/systemd/system/wifi-watchdog.service \
    /etc/systemd/system/wifi-watchdog.timer
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true
echo "      unit files removed"

echo "[3/5] Removing helper scripts and drop-ins..."
rm -f /usr/local/bin/noema-fix-ratchets.py
rm -f /usr/local/bin/noema-wifi-watchdog.sh
rm -f /usr/local/bin/wifi-watchdog.sh
rm -f /etc/logrotate.d/noema_tcp_watchdog
rm -f /etc/needrestart/conf.d/noema.conf
rm -f /etc/systemd/system.conf.d/noema-watchdog.conf
# Keep bluetooth.service.d/override.conf — other tools may rely on experimental mode.
# Keep /usr/local/bin/lorarx and rtl-sdr udev rules — optional hardware tooling.
echo "      helpers removed"

echo "[4/5] Removing application directories..."
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo "      removed $INSTALL_DIR"
else
    echo "      [SKIP] $INSTALL_DIR not found"
fi
if [ -d "$LXMF_TOOLS_DIR" ]; then
    rm -rf "$LXMF_TOOLS_DIR"
    echo "      removed $LXMF_TOOLS_DIR"
fi
if [ -d /root/rBrowser ]; then
    rm -rf /root/rBrowser
    echo "      removed /root/rBrowser"
fi

echo "[5/5] Config and data..."
# Always remove NOEMA-specific wardrive / bridge configs so a reinstall gets fresh
# port/settings (avoids stale wardrive_map.json keeping a bad port).
rm -f /etc/reticulum/wardrive_bot.json \
      /etc/reticulum/wardrive_map.json \
      /etc/reticulum/wardrive_bot.db \
      /etc/reticulum/wardrive_geo_cache.json \
      /var/log/noema_tcp_watchdog.log \
      /var/log/wardrive_map.log
rm -rf /etc/noema
echo "      removed /etc/noema and wardrive configs/DB under /etc/reticulum"

if [ "$PURGE" -eq 1 ]; then
    echo "      --purge: removing Reticulum + Nomadnet data (identities gone)"
    rm -rf "$RNS_CONFIG_DIR"
    rm -rf "$CURRENT_HOME/.nomadnetwork"
    # leftover if install used /etc/reticulum for more than wardrive
    rm -rf /etc/reticulum
else
    echo "      kept $RNS_CONFIG_DIR and $CURRENT_HOME/.nomadnetwork (identities)"
    echo "      use --purge to delete those too"
fi

echo ""
echo "========================================"
echo "  Uninstall complete"
echo "========================================"
echo ""
echo "  Reinstall:"
echo "    git clone https://github.com/e2ret/NOEMA-RNSGate-FULL.git"
echo "    cd NOEMA-RNSGate-FULL && sudo bash install.sh"
echo ""
echo "  Note: i2pd / bluez / python packages were left installed."
echo ""
