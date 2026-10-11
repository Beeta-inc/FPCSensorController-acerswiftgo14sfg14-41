#!/bin/bash
# Fetch libfpcbep.so if not already present in blob/

set -euo pipefail

DEST="$(cd "$(dirname "$0")" && pwd)/blob/libfpcbep.so"
MD5_EXPECTED="f7136fd774d5208e629bbeaa4974543a"

if [ -f "$DEST" ]; then
  if [ "$(md5sum "$DEST" | cut -d' ' -f1)" = "$MD5_EXPECTED" ]; then
    echo "==> blob/libfpcbep.so already present and verified!"
    exit 0
  fi
fi

echo "==> Downloading libfpcbep.so..."
curl -fsSL https://raw.githubusercontent.com/furcom/libfprint-patched/master/libfpcbep.so -o "$DEST"

MD5_GOT="$(md5sum "$DEST" | cut -d' ' -f1)"
if [ "$MD5_GOT" != "$MD5_EXPECTED" ]; then
  echo "ERROR: MD5 mismatch (got $MD5_GOT, expected $MD5_EXPECTED)" >&2
  exit 1
fi

echo "==> Verified blob/libfpcbep.so ($MD5_GOT)"
