#!/bin/bash
# 从本源码树构建 deb。用法: ./build-deb.sh [版本号]
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
VER="${1:-$(sed -n 's/^Version: //p' "$ROOT/pkg/DEBIAN/control")}"
BUILD_ROOT="$(mktemp -d)"
trap 'rm -rf "$BUILD_ROOT"' EXIT
BUILD="$BUILD_ROOT/pdf-translate"
mkdir -p "$BUILD/opt/pdf-translate"
cp -r "$ROOT/pkg/." "$BUILD/"
sed -i "s/^Version:.*/Version: $VER/" "$BUILD/DEBIAN/control"
cp "$ROOT/server.py" "$ROOT/zcode_bridge.py" "$BUILD/opt/pdf-translate/"
cp -r "$ROOT/static" "$BUILD/opt/pdf-translate/static"
cp "$ROOT/scripts/setup-venv.sh" "$BUILD/opt/pdf-translate/"
mkdir -p "$BUILD/opt/pdf-translate/scripts"
cp "$ROOT/scripts/"*zcode*bridge.mjs "$BUILD/opt/pdf-translate/scripts/"
mkdir -p "$BUILD/usr/share/doc/pdf-translate"
cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$BUILD/usr/share/doc/pdf-translate/"
# 部署用户占位符替换(仓库内不含用户名)
grep -rl "__APP_USER__" "$BUILD" | xargs -r sed -i "s/__APP_USER__/$USER/g"
chmod 755 "$BUILD/DEBIAN/"* "$BUILD/usr/bin/translate-pdf"
chmod 755 "$BUILD/opt/pdf-translate/setup-venv.sh"
find "$BUILD/opt" -type f ! -name "setup-venv.sh" -exec chmod 644 {} \;
dpkg-deb --build --root-owner-group "$BUILD" "$ROOT/pdf-translate_${VER}_all.deb"
echo "构建完成: $ROOT/pdf-translate_${VER}_all.deb"
