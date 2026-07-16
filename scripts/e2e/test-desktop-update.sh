#!/usr/bin/env bash
# Real packaged Electron updater gate: signed file:// v1 -> v2 under xvfb.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DESKTOP="$REPO_ROOT/apps/desktop"
LAUNCHER_DIR="$REPO_ROOT/apps/hermes-launcher"
UV="${UV:-$(command -v uv || true)}"
XVFB_RUN="${XVFB_RUN:-$(command -v xvfb-run || true)}"

[ -n "$UV" ] || { echo "ERROR: uv is required" >&2; exit 1; }
[ -n "$XVFB_RUN" ] || { echo "ERROR: xvfb-run is required" >&2; exit 1; }

WORK=$(mktemp -d)
export HERMES_HOME="$WORK/home"
export HERMES_DESKTOP_E2E_RELEASES="$WORK/releases"
mkdir -p "$HERMES_HOME" "$HERMES_DESKTOP_E2E_RELEASES"
cleanup() {
    if [ -f "$HERMES_HOME/desktop-e2e-version.json" ]; then
        relaunch_pid=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid", ""))' \
            "$HERMES_HOME/desktop-e2e-version.json" 2>/dev/null || true)
        [ -z "$relaunch_pid" ] || kill "$relaunch_pid" 2>/dev/null || true
    fi
    chmod -R u+w "$WORK" 2>/dev/null || true
    rm -rf "$WORK" || { sleep 1; rm -rf "$WORK"; }
}
trap cleanup EXIT

PLATFORM=$(
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) echo linux-x64 ;;
        Linux-aarch64|Linux-arm64) echo linux-arm64 ;;
        *) echo "unsupported desktop E2E platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
    esac
)

readarray -t KEYS < <("$UV" run --with pynacl python - <<'PY'
import base64
from nacl.signing import SigningKey
key = SigningKey.generate()
print(base64.b64encode(bytes(key)).decode())
print(base64.b64encode(bytes(key.verify_key)).decode())
PY
)
SIGNING_KEY="${KEYS[0]}"
PUBLIC_KEY="${KEYS[1]}"

printf '==> building updater with ephemeral E2E trust key\n'
(
    cd "$LAUNCHER_DIR"
    if grep -qi '^ID=nixos' /etc/os-release 2>/dev/null; then
        HERMES_RELEASE_PUBLIC_KEY="$PUBLIC_KEY" nix shell nixpkgs#gcc nixpkgs#openssl -c cargo build --quiet
    else
        HERMES_RELEASE_PUBLIC_KEY="$PUBLIC_KEY" cargo build --quiet
    fi
)
LAUNCHER="$LAUNCHER_DIR/target/debug/hermes"
BOOTSTRAP="$WORK/hermes-updater"
cp "$LAUNCHER" "$BOOTSTRAP"
chmod +x "$BOOTSTRAP"

printf '==> building the real packaged Electron app\n'
PACKAGED="$DESKTOP/release/linux-unpacked"
if [ ! -x "$PACKAGED/Hermes" ]; then
    npm run --prefix "$DESKTOP" pack
fi
[ -x "$PACKAGED/Hermes" ] || { echo "ERROR: packaged Hermes binary missing" >&2; exit 1; }

make_bundle() {
    local version="$1"
    local tree="$WORK/bundle-$version"
    local version_dir="$HERMES_DESKTOP_E2E_RELEASES/$version"
    rm -rf "$tree"
    mkdir -p "$tree/bin" "$tree/runtime/venv/bin" "$tree/runtime/tools" \
        "$tree/runtime/node/bin" "$tree/runtime/python/bin" "$tree/app/hermes_cli" \
        "$tree/ui/tui/dist" "$tree/ui/web/dist" "$tree/desktop" "$version_dir"

    cp "$LAUNCHER" "$tree/bin/hermes"
    chmod +x "$tree/bin/hermes"
    cp -a "$PACKAGED/." "$tree/desktop/"
    printf '__version__ = "%s"\n' "$version" > "$tree/app/hermes_cli/__init__.py"
    printf '%s\n' "$version" > "$tree/VERSION"
    printf 'tui\n' > "$tree/ui/tui/dist/entry.js"
    printf 'web\n' > "$tree/ui/web/dist/index.html"
    cat > "$tree/runtime/venv/bin/python" <<'PY'
#!/bin/sh
exit 0
PY
    chmod +x "$tree/runtime/venv/bin/python"

    "$UV" run --with pynacl python "$REPO_ROOT/scripts/release/write-manifest.py" \
        --bundle-dir "$tree" --version "$version" --channel stable \
        --git-sha "$(printf 'a%.0s' {1..40})" --platform "$PLATFORM" \
        --signing-key "$SIGNING_KEY" >/dev/null
    cp "$tree/manifest.json" "$tree/manifest.json.sig" "$version_dir/"
    local normalized="$WORK/normalize-$version"
    rm -rf "$normalized"
    mkdir -p "$normalized/bundle"
    cp -a "$tree/." "$normalized/bundle/"
    tar --zstd -cf "$version_dir/hermes-$version-$PLATFORM.tar.zst" -C "$normalized" bundle
}

printf '==> creating and installing signed v1 bundle\n'
make_bundle 1.0.0
printf '1.0.0\n' > "$HERMES_DESKTOP_E2E_RELEASES/latest-stable.txt"
"$BOOTSTRAP" install --source "file://$HERMES_DESKTOP_E2E_RELEASES" --channel stable

printf '==> publishing signed v2 bundle\n'
make_bundle 2.0.0
printf '2.0.0\n' > "$HERMES_DESKTOP_E2E_RELEASES/latest-stable.txt"

printf '==> launching packaged v1 and driving update IPC with Playwright\n'
"$XVFB_RUN" -a node "$DESKTOP/e2e/desktop-update.mjs"

[ "$(cat "$HERMES_HOME/current.txt")" = "2.0.0" ]
[ "$(cat "$HERMES_HOME/previous.txt")" = "1.0.0" ]
[ ! -e "$HERMES_HOME/.hermes-update-in-progress" ]
printf 'E2E_PASS: packaged Electron applied signed update and relaunched v2\n'
