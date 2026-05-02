#!/usr/bin/env bash
# Download MediaMTX binary into ./mediamtx/ for the host arch.
# Usage: ./scripts/install-mediamtx.sh [version]
# If version is omitted, fetches latest from GitHub releases.

set -euo pipefail

cd "$(dirname "$0")/.."

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
    VERSION=$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')
fi

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$OS-$ARCH" in
    darwin-arm64)  ASSET="mediamtx_${VERSION}_darwin_arm64.tar.gz" ;;
    darwin-x86_64) ASSET="mediamtx_${VERSION}_darwin_amd64.tar.gz" ;;
    linux-x86_64)  ASSET="mediamtx_${VERSION}_linux_amd64.tar.gz" ;;
    linux-aarch64) ASSET="mediamtx_${VERSION}_linux_arm64.tar.gz" ;;
    *) echo "unsupported host: $OS-$ARCH" >&2; exit 1 ;;
esac

URL="https://github.com/bluenviron/mediamtx/releases/download/${VERSION}/${ASSET}"

mkdir -p mediamtx
cd mediamtx

echo "Downloading ${URL}"
curl -fsSL "$URL" -o "$ASSET"
tar -xzf "$ASSET" mediamtx
rm "$ASSET"
chmod +x mediamtx

./mediamtx --version
echo "Installed to mediamtx/mediamtx"
