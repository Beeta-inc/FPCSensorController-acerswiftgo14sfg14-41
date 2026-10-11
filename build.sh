#!/bin/bash
# Build libfprint with the fpcmoh driver and Acer Swift Go 14 (10a5:e340) support.
#
# Produces dist/libfprint-2.so.2.0.0
# Run as a normal user, NOT root.

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BUILD="$SRC/build"
TREE="$BUILD/libfprint-src"
TAG="${LIBFPRINT_TAG:-v1.94.6}"
MR_URL="https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/396.patch"
BLOBDIR="$SRC/blob"

[ "$(id -u)" -ne 0 ] || { echo "do NOT run this as root" >&2; exit 1; }

if [ ! -f "$BLOBDIR/libfpcbep.so" ]; then
  echo "ERROR: $BLOBDIR/libfpcbep.so not found." >&2
  exit 1
fi

mkdir -p "$BUILD"

# Clone libfprint v1.94.6 if not already present
if [ ! -d "$TREE" ]; then
  echo "==> cloning libfprint $TAG from gitlab.freedesktop.org"
  git clone --depth 1 --branch "$TAG" \
      https://gitlab.freedesktop.org/libfprint/libfprint.git "$TREE"
fi

if [ ! -f "$BUILD/mr396.patch" ]; then
  echo "==> fetching merge request 396 (fpcmoh driver)"
  curl -fsSL --retry 3 -o "$BUILD/mr396.patch" "$MR_URL"
fi

echo "==> applying merge request 396 + patches/"
git -C "$TREE" reset --hard "$TAG"
git -C "$TREE" clean -fd
git -C "$TREE" apply "$BUILD/mr396.patch"

for p in "$SRC"/patches/*.patch; do
  [ -e "$p" ] || continue
  echo "    Applying $(basename "$p")..."
  git -C "$TREE" apply "$p"
done

echo "==> linking against $BLOBDIR/libfpcbep.so"
LINKARGS="-L$BLOBDIR -Wl,-rpath-link,$BLOBDIR"

echo "==> configuring meson"
rm -rf "$TREE/builddir"
meson setup "$TREE/builddir" "$TREE" \
  --prefix=/opt/fpc-e340 \
  -Ddrivers=fpcmoh \
  -Dgtk-examples=false -Ddoc=false -Dintrospection=false \
  -Dc_link_args="$LINKARGS" -Dcpp_link_args="$LINKARGS"

echo "==> building with ninja"
ninja -C "$TREE/builddir"

# Stage artifact
mkdir -p "$SRC/dist"
cp "$TREE/builddir/libfprint/libfprint-2.so.2.0.0" "$SRC/dist/libfprint-2.so.2.0.0"

echo
echo "==> dist/libfprint-2.so.2.0.0 built successfully!"

# Verify fpcmoh driver and e340 id
if strings -a "$SRC/dist/libfprint-2.so.2.0.0" | grep -q "FPC MOH Fingerprint Sensor"; then
  echo "    [OK] fpcmoh driver present"
else
  echo "    [ERROR] fpcmoh driver missing" >&2
  exit 1
fi

python3 - "$SRC/dist/libfprint-2.so.2.0.0" <<'PYEOF'
import sys
data = open(sys.argv[1], 'rb').read()
entry = (0xe340).to_bytes(4, 'little') + (0x10a5).to_bytes(4, 'little')
if data.count(entry):
    print("    [OK] id_table 10a5:e340 present")
else:
    sys.exit("    [ERROR] 10a5:e340 NOT found in id_table")
PYEOF

echo
echo "Done! You can now install it with: sudo ./install.sh"
