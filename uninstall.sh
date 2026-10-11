#!/bin/bash
# Uninstall the FPC 10a5:e340 fingerprint driver.
# Run as root: sudo ./uninstall.sh

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "must run as root: sudo ./uninstall.sh" >&2; exit 1; }

echo "==> removing /opt/fpc-e340"
rm -rf /opt/fpc-e340

echo "==> removing systemd drop-in and udev rule"
rm -f /etc/systemd/system/fprintd.service.d/10-fpc-e340.conf
rmdir /etc/systemd/system/fprintd.service.d 2>/dev/null || true
rm -f /etc/udev/rules.d/60-libfprint-2-device-fpc-e340.rules

echo "==> reloading systemd and restarting fprintd"
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb
systemctl daemon-reload
systemctl restart fprintd 2>/dev/null || true

echo "Uninstalled successfully. Stock system state restored."
