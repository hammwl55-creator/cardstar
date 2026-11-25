#!/bin/sh
# Build cardstar_<version>_all.deb in the project folder. Needs dpkg-deb only.
# Nothing is installed or downloaded.
# Copyright (C) 2026 TheBeardedHamm
# SPDX-License-Identifier: GPL-2.0-only
set -eu
cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^Version: //p' packaging/control)
ROOT=$(mktemp -d "$PWD/.stage.XXXXXX")
trap 'rm -rf "$ROOT"' EXIT

AL="$ROOT/usr/share/APPLaunch"
APP="$AL/apps/cardstar"
DOC="$ROOT/usr/share/doc/cardstar"
mkdir -p "$ROOT/DEBIAN" "$AL/applications" "$AL/bin" "$AL/share/images" "$APP/vendor" "$DOC"

cp src/cardstar.py src/cardstar_setup.py src/cardstar_mic.py "$APP/"
cp -R vendor/iax2 vendor/iax2-0.1.0b2.dist-info "$APP/vendor/"
cp packaging/cardstar "$AL/bin/cardstar"
cp packaging/cardstar.desktop "$AL/applications/"
cp packaging/cardstar_100.png "$AL/share/images/"
cp LICENSE README.md packaging/copyright "$DOC/"
find "$ROOT" \( -name __pycache__ -o -name '._*' -o -name .DS_Store \) -prune -exec rm -rf {} +

find "$ROOT" -type d -exec chmod 0755 {} +
find "$ROOT" -type f -exec chmod 0644 {} +
chmod 0755 "$AL/bin/cardstar"

SIZE=$(du -sk "$ROOT/usr" | cut -f1)
sed "s/@SIZE@/$SIZE/" packaging/control > "$ROOT/DEBIAN/control"
chmod 0644 "$ROOT/DEBIAN/control"

dpkg-deb --root-owner-group -Zxz -b "$ROOT" "cardstar_${VERSION}_all.deb"
