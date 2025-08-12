#!/bin/sh
set -euo pipefail

APPID=org.example.ModernDemo
APPNAME="${APPID##*.}"

echo "Building ${APPNAME}..."

# Clean & stage
rm -rf AppDir
mkdir -p AppDir/usr/bin AppDir/usr/share/applications AppDir/usr/share/icons/hicolor/scalable/apps

# Project files
install -Dm644 app.py AppDir/usr/bin/app.py
install -Dm644 "${APPID}.desktop" "AppDir/${APPID}.desktop"
install -Dm644 "${APPID}.desktop" "AppDir/usr/share/applications/${APPID}.desktop"
install -Dm644 "${APPID}.svg" "AppDir/${APPID}.svg"
install -Dm644 "${APPID}.svg" "AppDir/usr/share/icons/hicolor/scalable/apps/${APPID}.svg"
install -Dm755 AppRun.py AppDir/AppRun
install -Dm755 "${APPNAME}.sh" "AppDir/usr/bin/${APPNAME}" 2>/dev/null || true

# Ensure launchers are executable
chmod +x AppDir/AppRun
chmod +x "AppDir/usr/bin/${APPNAME}"

# Get appimagetool (arch-specific), then build
if [ ! -x appimagetool.AppImage ]; then
  curl -L "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$(uname -m).AppImage" -o appimagetool.AppImage
  chmod +x appimagetool.AppImage
fi

# Normalize line endings
sed -i 's/\r$//' *.py *.sh *.desktop

# Silverblue/immutable-friendly
export APPIMAGE_EXTRACT_AND_RUN=1
export ARCH=$(uname -m)

./appimagetool.AppImage -n AppDir "${APPID}.AppImage"

chmod +x "${APPID}.AppImage"

echo
echo "Built: ${APPID}.AppImage"
echo "This AppImage uses host Python + GTK4/Libadwaita/PyGObject."
