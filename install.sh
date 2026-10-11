#!/bin/bash
# Install the FPC 10a5:e340 fingerprint driver.
#
# Installs libfprint into /opt/fpc-e340/lib and configures fprintd via a systemd
# drop-in. Nothing under /usr/lib is modified or overwritten.
# Run as root: sudo ./install.sh

set -euo pipefail

PREFIX=/opt/fpc-e340/lib
SRC="$(cd "$(dirname "$0")" && pwd)"
LIB="$SRC/dist/libfprint-2.so.2.0.0"
BLOB="$SRC/blob/libfpcbep.so"
UDEV=/etc/udev/rules.d/60-libfprint-2-device-fpc-e340.rules
DROPIN_DIR=/etc/systemd/system/fprintd.service.d
DROPIN="$DROPIN_DIR/10-fpc-e340.conf"

[ "$(id -u)" -eq 0 ] || { echo "must run as root: sudo ./install.sh" >&2; exit 1; }

for f in "$LIB" "$BLOB"; do
  [ -f "$f" ] || { echo "missing: $f (run ./build.sh first)" >&2; exit 1; }
done

echo "==> installing libfprint + FPC blob to $PREFIX"
install -d -m 0755 "$PREFIX"
install -m 0644 "$LIB"  "$PREFIX/libfprint-2.so.2.0.0"
install -m 0644 "$BLOB" "$PREFIX/libfpcbep.so"
ln -sf libfprint-2.so.2.0.0 "$PREFIX/libfprint-2.so.2"

echo "==> creating /var/log/fpc"
install -d -m 0755 /var/log/fpc

echo "==> installing udev rule -> $UDEV"
cat > "$UDEV" <<'EOF'
# FPC 10a5:e340 (Acer Swift Go 14) - handled by fpcmoh driver in /opt/fpc-e340/lib
SUBSYSTEM=="usb", ATTRS{idVendor}=="10a5", ATTRS{idProduct}=="e340", ATTRS{dev}=="*", TEST=="power/control", ATTR{power/control}="auto"
SUBSYSTEM=="usb", ATTRS{idVendor}=="10a5", ATTRS{idProduct}=="e340", ENV{LIBFPRINT_DRIVER}="FPC MOH Fingerprint Sensor"
EOF

echo "==> installing systemd drop-in -> $DROPIN"
install -d -m 0755 "$DROPIN_DIR"
cat > "$DROPIN" <<EOF
[Service]
# Load our custom libfprint and libfpcbep.so from $PREFIX
Environment=LD_LIBRARY_PATH=$PREFIX
# fprintd.service sets ProtectSystem=strict, so /var/log/fpc needs read-write access
ReadWritePaths=/var/log/fpc
EOF

echo "==> reloading udev + systemd"
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb
systemctl daemon-reload
systemctl restart fprintd 2>/dev/null || true

echo
echo "=========================================================="
echo "FINISHED! Driver installed and fprintd reloaded."
echo "You can now enroll your fingerprint by running:"
echo "    fprintd-enroll"
echo "To test verification:"
echo "    fprintd-verify"
echo "To uninstall: sudo ./uninstall.sh"
echo "=========================================================="
