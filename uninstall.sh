#!/usr/bin/env bash

set -e

if [[ "$EUID" -ne 0 ]]; then
  echo "Uninstaller must be run as root"
  exit 1
fi

INSTALL_DIR="/usr/bin"
TARGET="$INSTALL_DIR/bootableusb"

echo "Uninstalling bootableusb..."

if [[ -f "$TARGET" ]]; then
  rm "$TARGET"
  echo "bootableusb uninstalled."
else
  echo "bootableusb installation not found."
fi