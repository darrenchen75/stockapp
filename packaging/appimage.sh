#!/usr/bin/env bash
# Wraps the PyInstaller --onedir build (dist/StockBars/) into StockBars-linux.AppImage.
set -euo pipefail
app=build/StockBars.AppDir
rm -rf "$app"
mkdir -p "$app/usr/bin"
cp -r dist/StockBars/. "$app/usr/bin/"
cp packaging/stockbars.png "$app/stockbars.png"
cat > "$app/stockbars.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Stock Bars
Exec=StockBars
Icon=stockbars
Categories=Office;Finance;
EOF
cat > "$app/AppRun" <<'EOF'
#!/bin/sh
exec "$(dirname "$(readlink -f "$0")")/usr/bin/StockBars" "$@"
EOF
chmod +x "$app/AppRun"

tool=build/appimagetool
if [ ! -x "$tool" ]; then
  curl -fsSL -o "$tool" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$tool"
fi
# extract-and-run: CI runners have no FUSE
ARCH=x86_64 "$tool" --appimage-extract-and-run "$app" StockBars-linux.AppImage
