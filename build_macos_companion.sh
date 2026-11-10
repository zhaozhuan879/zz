#!/usr/bin/env bash
# Build on macOS. Produces a per-architecture .pkg containing the URL handler
# app and a self-contained launchd worker. Build-time Python is not required
# on students' Macs.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Run this script on a macOS build machine." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64)
    TRIPLE="aarch64-apple-darwin"
    CODEX_SHA256="34d12384d90f8e09b0b9abaabf2a0ac02a52093b131504aeaf282107f54a44b2"
    ;;
  x86_64)
    TRIPLE="x86_64-apple-darwin"
    CODEX_SHA256="792ecab0596006bd9f286430b2790aa487fd40fca2f351ca313ad85991720226"
    ;;
  *) echo "Unsupported Mac architecture: $ARCH" >&2; exit 2 ;;
esac
BUILD="$ROOT/build/macos-$ARCH"
DIST="$ROOT/dist"
mkdir -p "$BUILD" "$DIST"

python3 -m venv "$BUILD/venv"
PYTHON="$BUILD/venv/bin/python"
"$PYTHON" -m pip install --upgrade 'pyinstaller==6.22.2' 'httpx>=0.28,<1'
CODEX_VERSION="0.165.0"
ARCHIVE="$BUILD/codex-package-$TRIPLE.tar.gz"
CODEX_PACKAGE="$BUILD/codex-package"
curl --fail --location --retry 3 --output "$ARCHIVE" \
  "https://github.com/openai/codex/releases/download/rust-v$CODEX_VERSION/codex-package-$TRIPLE.tar.gz"
printf '%s  %s\n' "$CODEX_SHA256" "$ARCHIVE" | shasum -a 256 --check
mkdir -p "$CODEX_PACKAGE"
tar -xzf "$ARCHIVE" -C "$CODEX_PACKAGE"
CODEX="$CODEX_PACKAGE/bin/codex"
test -x "$CODEX"

BINARIES=(--add-binary "$CODEX:codex-bin")
CODE_MODE_HOST="$CODEX_PACKAGE/bin/codex-code-mode-host"
if [[ -f "$CODE_MODE_HOST" ]]; then
  BINARIES+=(--add-binary "$CODE_MODE_HOST:codex-bin")
fi
if [[ -f "$CODEX_PACKAGE/codex-path/rg" ]]; then
  BINARIES+=(--add-binary "$CODEX_PACKAGE/codex-path/rg:codex-bin")
fi
LEGAL=(--add-data "$ROOT/legal/CODEX_LICENSE.txt:legal"
       --add-data "$ROOT/legal/CODEX_NOTICE.txt:legal")

"$PYTHON" -m PyInstaller --noconfirm --clean --onefile \
  --name ResearchCompanionWorker \
  --distpath "$BUILD/worker-dist" --workpath "$BUILD/worker-build" \
  --specpath "$BUILD" "${BINARIES[@]}" "${LEGAL[@]}" "$ROOT/companion_macos.py"

"$PYTHON" -m PyInstaller --noconfirm --clean --onedir --windowed --argv-emulation \
  --name ResearchCompanionMac \
  --osx-bundle-identifier com.yunzhengdata.researchcompanion \
  --distpath "$BUILD/app-dist" --workpath "$BUILD/app-build" \
  --specpath "$BUILD" "$ROOT/companion_macos.py"

APP="$BUILD/app-dist/ResearchCompanionMac.app"
mkdir -p "$APP/Contents/Resources"
cp "$BUILD/worker-dist/ResearchCompanionWorker" "$APP/Contents/Resources/ResearchCompanionWorker"
cp "$ROOT/legal/CODEX_LICENSE.txt" "$APP/Contents/Resources/CODEX_LICENSE.txt"
cp "$ROOT/legal/CODEX_NOTICE.txt" "$APP/Contents/Resources/CODEX_NOTICE.txt"
chmod 755 "$APP/Contents/Resources/ResearchCompanionWorker"

"$PYTHON" - "$APP/Contents/Info.plist" <<'PY'
import plistlib
from pathlib import Path
import sys

path = Path(sys.argv[1])
info = plistlib.loads(path.read_bytes())
info["CFBundleURLTypes"] = [{
    "CFBundleTypeRole": "Editor",
    "CFBundleURLName": "com.yunzhengdata.researchcompanion.pair",
    "CFBundleURLSchemes": ["researchworkbench"],
}]
info["LSUIElement"] = True
path.write_bytes(plistlib.dumps(info))
PY

if [[ -n "${MAC_CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --deep --options runtime --timestamp \
    --sign "$MAC_CODESIGN_IDENTITY" "$APP"
else
  codesign --force --deep --sign - "$APP"
fi

PKG="$DIST/ResearchCompanionMac-$ARCH.pkg"
PKG_ARGS=(--component "$APP" --install-location /Applications
  --identifier com.yunzhengdata.researchcompanion --version 0.1.0)
if [[ -n "${MAC_INSTALLER_IDENTITY:-}" ]]; then
  PKG_ARGS+=(--sign "$MAC_INSTALLER_IDENTITY")
fi
pkgbuild "${PKG_ARGS[@]}" "$PKG"
if [[ -n "${MAC_NOTARY_PROFILE:-}" ]]; then
  if [[ -z "${MAC_CODESIGN_IDENTITY:-}" || -z "${MAC_INSTALLER_IDENTITY:-}" ]]; then
    echo "Notarization requires both app and installer signing identities." >&2
    exit 4
  fi
  xcrun notarytool submit "$PKG" --keychain-profile "$MAC_NOTARY_PROFILE" --wait
  xcrun stapler staple "$PKG"
fi
echo "macOS installer: $PKG"
