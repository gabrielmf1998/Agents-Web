#!/usr/bin/env bash
# Agents Web installer for Linux: Debian, Ubuntu and derivatives, Fedora, RHEL/Alma/Rocky, openSUSE and Arch.
#
#   curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/Agents-Web/main/install.sh | bash
#   curl -fsSL https://gitlab.com/gabriel17166/agents-web/-/raw/main/install.sh | bash -s -- --gitlab
#
# By default it installs a copy for your user (in ~/.local, it updates itself without a password).
# With --package it installs the .deb, .rpm or pacman package of the latest release instead.
#
# Options:
#   --package           install the system package for this distribution (asks for sudo)
#   --ref <tag|branch>  install that version instead of the latest release
#   --gitlab            download from the GitLab mirror instead of GitHub
#   --no-deps           do not install distribution packages
#   --no-start          do not start the app when the install ends
#   --uninstall         remove the app (chats and settings are kept)
#   --purge             with --uninstall, also delete chats, uploads, settings and extra account logins
#   --from <dir>        install from an unpacked release or a checkout (used by updates)
#
# Every download is checked against the SHA256SUMS file of the release.
# https://github.com/gabrielmf1998/Agents-Web (MIT license)
set -euo pipefail

GH_REPO="gabrielmf1998/Agents-Web"
GL_REPO="gabriel17166/agents-web"
APP="$HOME/.local/opt/agents-web"
SYS_APP="/usr/lib/agents-web"
BIN="$HOME/.local/bin"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
# server.py always keeps its own data here, whatever XDG_* say (see docs/ARCHITECTURE.md)
APP_CONF="$HOME/.config/agents-web"
APP_DATA="$HOME/.local/share/agents-web"
PORT="${AGENTS_WEB_PORT:-47830}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"
    elif command -v sudo >/dev/null; then sudo "$@"
    elif command -v pkexec >/dev/null; then pkexec "$@"
    else die "need sudo or pkexec to install packages (or rerun with --no-deps)"; fi
}

# package managers read answers from stdin, which is this script when piped from curl
pm() {
    if [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; then as_root "$@" </dev/tty
    else as_root "$@"; fi
}

distro() {
    local id="" like=""
    if [ -r /etc/os-release ]; then
        id="$(. /etc/os-release; echo "${ID:-}")"
        like="$(. /etc/os-release; echo "${ID_LIKE:-}")"
    fi
    case " $id $like " in
        *" arch "*|*" archlinux "*) echo arch ;;
        *" debian "*|*" ubuntu "*) echo debian ;;
        *" suse "*|*" opensuse "*|*" sles "*) echo suse ;;
        *" fedora "*|*" rhel "*|*" centos "*) echo fedora ;;
        *) if command -v pacman >/dev/null; then echo arch
           elif command -v apt-get >/dev/null; then echo debian
           elif command -v zypper >/dev/null; then echo suse
           elif command -v dnf >/dev/null; then echo fedora
           else echo "${id:-unknown}"; fi ;;
    esac
}

# $1: "run" (what the app needs) or "build" (also what compiling the tray icon needs)
install_deps() {
    local need=() want=() missing=() p d
    d="$(distro)"
    case "$d" in
        arch)
            need=(python util-linux iproute2 procps-ng systemd-libs curl tar)
            want=(openssl qrencode polkit xdg-utils)
            [ "$1" = build ] && need+=(gcc pkgconf)
            for p in "${need[@]}" "${want[@]}"; do pacman -Q "$p" >/dev/null 2>&1 || missing+=("$p"); done
            if [ "${#missing[@]}" -gt 0 ]; then
                say "Installing packages: ${missing[*]}"
                pm pacman -S --needed --noconfirm "${missing[@]}" \
                    || die "pacman failed (if the error is a 404, run 'sudo pacman -Syu' first)"
            fi ;;
        debian)
            need=(python3 util-linux iproute2 procps libsystemd0 curl tar ca-certificates)
            want=(openssl qrencode xdg-utils)
            # polkit: pkttyagent is in polkitd (newer releases) or policykit-1 (older)
            if apt-cache show polkitd >/dev/null 2>&1; then want+=(polkitd pkexec); else want+=(policykit-1); fi
            [ "$1" = build ] && need+=(gcc libsystemd-dev pkg-config)
            for p in "${need[@]}" "${want[@]}"; do
                dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed" || missing+=("$p")
            done
            if [ "${#missing[@]}" -gt 0 ]; then
                say "Installing packages: ${missing[*]}"
                pm env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" || {
                    as_root apt-get update -qq || true
                    pm env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
                } || die "apt-get failed"
            fi ;;
        fedora|suse)
            if [ "$d" = suse ]; then
                need=(python3 util-linux iproute2 procps libsystemd0 curl tar)
                want=(openssl qrencode polkit xdg-utils)
                [ "$1" = build ] && need+=(gcc systemd-devel)
            else
                need=(python3 util-linux iproute procps-ng systemd-libs curl tar)
                want=(openssl qrencode polkit xdg-utils)
                [ "$1" = build ] && need+=(gcc systemd-devel)
            fi
            for p in "${need[@]}" "${want[@]}"; do rpm -q --quiet "$p" || missing+=("$p"); done
            if [ "${#missing[@]}" -gt 0 ]; then
                say "Installing packages: ${missing[*]}"
                if [ "$d" = suse ]; then
                    pm zypper --non-interactive install --no-recommends "${missing[@]}" || die "zypper failed"
                else
                    # qrencode comes from EPEL on RHEL and its rebuilds: the rest still installs without it
                    pm dnf install -y --setopt=install_weak_deps=False --skip-broken "${missing[@]}" \
                        || pm dnf install -y --setopt=install_weak_deps=False "${need[@]}" \
                        || die "dnf failed"
                fi
            fi ;;
        *)
            warn "Unknown distribution '$d'. Install these yourself: python3 (3.9 or newer), util-linux (setpriv),"
            warn "iproute2, procps (pgrep), curl, libsystemd, and if you can openssl, qrencode, polkit and xdg-utils."
            [ "$1" = build ] && warn "To build the tray icon also gcc and the libsystemd headers." ;;
    esac
}

# The release JSON of GitHub or GitLab, read with python3 (always needed, so no jq)
release_json() {
    local url
    if [ -n "${AGENTS_WEB_RELEASES_API:-}" ]; then url="$AGENTS_WEB_RELEASES_API"
    elif [ "$SOURCE" = gitlab ]; then
        if [ -n "$REF" ]; then url="https://gitlab.com/api/v4/projects/${GL_REPO//\//%2F}/releases/$REF"
        else url="https://gitlab.com/api/v4/projects/${GL_REPO//\//%2F}/releases/permalink/latest"; fi
    else
        if [ -n "$REF" ]; then url="https://api.github.com/repos/$GH_REPO/releases/tags/$REF"
        else url="https://api.github.com/repos/$GH_REPO/releases/latest"; fi
    fi
    curl -fsSL -H "Accept: application/json" "$url" 2>/dev/null || true
}

# prints "<tag>" then one "<name> <url>" line per asset
release_assets() {
    python3 -c '
import json, sys
try:
    j = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
print(j.get("tag_name") or "")
a = j.get("assets")
for x in (a if isinstance(a, list) else []):
    print(x["name"], x["browser_download_url"])
for x in ((a or {}).get("links") or []) if isinstance(a, dict) else []:
    print(x["name"], x.get("direct_asset_url") or x["url"])
'
}

machine() {
    case "$(uname -m)" in x86_64|amd64) echo x86_64 ;; aarch64|arm64) echo aarch64 ;; *) uname -m ;; esac
}

# downloads the asset whose name matches $1 (an extended regex) into $TMP and checks its SHA256
fetch_asset() {
    local rx="$1" name="" url="" sums="" n u h expected
    while read -r n u; do
        [ -n "$u" ] || continue
        if [ "$n" = SHA256SUMS ]; then sums="$u"; fi
        if [ -z "$name" ] && [[ "$n" =~ ^${rx}$ ]]; then name="$n" url="$u"; fi
    done <<<"$ASSETS"
    [ -n "$name" ] || return 1
    [ -n "$sums" ] || die "release $TAG has no SHA256SUMS, refusing to install unchecked files"
    say "Downloading $name"
    curl -fsSL -o "$TMP/$name" "$url" || die "download failed: $url"
    curl -fsSL -o "$TMP/SHA256SUMS" "$sums" || die "download failed: $sums"
    # plain bash: minimal systems (openSUSE containers, say) have no awk
    expected=""
    while read -r h n; do if [ "${n#\*}" = "$name" ]; then expected="$h"; fi; done <"$TMP/SHA256SUMS"
    [ -n "$expected" ] || die "$name is not listed in SHA256SUMS"
    [ "$(sha256sum "$TMP/$name" | cut -d' ' -f1)" = "$expected" ] || die "$name does not match its SHA256 sum"
    say "SHA256 checked"
    ASSET="$TMP/$name"
}

new_tmp() {
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
}

read_release() {
    ASSETS="$(release_json | release_assets)"
    TAG="$(head -n1 <<<"$ASSETS")"
    ASSETS="$(tail -n +2 <<<"$ASSETS")"
}

# the release tarball (with a prebuilt tray icon), or the source of a tag or branch
fetch_source() {
    new_tmp
    read_release
    local v="${TAG#v}"
    if [ -n "$TAG" ] && fetch_asset "agents-web-${v//./\\.}-linux-$(machine)\\.tar\\.gz"; then
        mkdir "$TMP/src"
        tar -xzf "$ASSET" -C "$TMP/src" --strip-components=1 || die "could not unpack $ASSET"
        SRC="$TMP/src"
    else
        local ref="${REF:-${TAG:-main}}" url
        [ -n "$TAG" ] && warn "no prebuilt download for $(machine), building from source"
        if [ "$SOURCE" = gitlab ]; then url="https://gitlab.com/$GL_REPO/-/archive/$ref/agents-web-$ref.tar.gz"
        else url="https://github.com/$GH_REPO/archive/$ref.tar.gz"; fi
        say "Downloading the source of Agents Web $ref"
        mkdir "$TMP/src"
        curl -fsSL "$url" | tar -xz -C "$TMP/src" --strip-components=1 || die "download failed: $url"
        SRC="$TMP/src"
    fi
    [ -f "$SRC/src/server.py" ] || die "the download does not look like Agents Web"
}

stop_running() {
    WAS_RUNNING=0
    if pgrep -u "$(id -u)" -x agents-web-tray >/dev/null; then
        WAS_RUNNING=1
        say "Stopping the running Agents Web"
        pkill -u "$(id -u)" -x agents-web-tray || true
        for _ in $(seq 1 50); do pgrep -u "$(id -u)" -x agents-web-tray >/dev/null || break; sleep 0.1; done
    fi
}

desktop_entry() { # $1: file, $2: command
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Name=Agents
GenericName=AI agents web interface
Comment=Use the Claude Code and Antigravity CLIs from your browser or phone
Exec=$2 start
Icon=agents-web
Terminal=false
Categories=Development;Utility;
Keywords=claude;antigravity;agent;ai;chat;
EOF
}

do_install() {
    if [ -f "$SYS_APP/install-kind" ] && [ -z "$FROM" ]; then
        die "Agents Web is installed as a system package ($(cat "$SYS_APP/install-kind")). Update it with 'agents-web update' or remove the package first."
    fi
    command -v curl >/dev/null || [ -n "$SRC" ] || die "curl is required"
    if [ "$DEPS" = 1 ]; then install_deps run; fi
    command -v python3 >/dev/null || die "python3 is required"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is required"
    [ -n "$SRC" ] || fetch_source

    local tray
    tray="$(mktemp)"
    if [ -x "$SRC/tray/agents-web-tray" ] && "$SRC/tray/agents-web-tray" --version >/dev/null 2>&1; then
        cp "$SRC/tray/agents-web-tray" "$tray"
    else
        say "Building the tray icon"
        if ! command -v gcc >/dev/null && [ "$DEPS" = 1 ]; then install_deps build; fi
        command -v gcc >/dev/null || die "gcc is required to build the tray icon"
        gcc -O2 -Wall -o "$tray" "$SRC/tray/agents-web-tray.c" -lsystemd -lm \
            || die "build failed (it needs the libsystemd headers: libsystemd-dev, systemd-devel or systemd-libs)"
    fi

    stop_running
    say "Installing to $APP"
    mkdir -p "$APP/bin" "$BIN" "$SHARE/applications" "$SHARE/icons/hicolor/scalable/apps"
    install -m 644 "$SRC/src/server.py" "$SRC/src/index.html" "$APP/"
    install -m 755 "$SRC/src/bin/pkexec" "$APP/bin/pkexec"
    install -m 755 "$SRC/install.sh" "$APP/install.sh"
    install -m 755 "$tray" "$APP/agents-web-tray"
    install -m 644 "$SRC/data/agents-web.svg" "$SRC/data/icon-192.png" "$SRC/data/icon-512.png" "$APP/"
    echo "$SOURCE" > "$APP/source"   # where updates come from
    echo script > "$APP/install-kind"
    rm -f "$tray"
    rm -rf "$APP/__pycache__" "$APP/python"
    sed "s|^APP_DEFAULT=.*|APP_DEFAULT=\"$APP\"|" "$SRC/src/agents-web" > "$BIN/agents-web"
    chmod 755 "$BIN/agents-web"
    install -m 644 "$SRC/data/agents-web.svg" "$SHARE/icons/hicolor/scalable/apps/agents-web.svg"
    desktop_entry "$SHARE/applications/agents-web.desktop" "$BIN/agents-web"
    if [ -f "$SHARE/icons/hicolor/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
        gtk-update-icon-cache -q -t "$SHARE/icons/hicolor" 2>/dev/null || true
    fi

    say "Installed Agents Web $(sed -n 's/^VERSAO = "\(.*\)"/\1/p' "$APP/server.py")"
    after_install "$BIN/agents-web" "$APP/agents-web-tray"
}

after_install() { # $1: launcher, $2: tray
    check_clis
    case ":$PATH:" in *":$(dirname "$1"):"*) ;; *) warn "$(dirname "$1") is not in your PATH: open 'Agents' from the app menu or run $1";; esac
    if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
        echo "   firewalld is active. If your phone cannot reach the page, open the port on your LAN:"
        echo "   sudo firewall-cmd --permanent --add-port=$PORT/tcp && sudo firewall-cmd --reload"
    fi
    # a copy that was running (an update, say) starts again even without a display: the tray only needs D-Bus
    if [ "$START" = 1 ] && [ "$WAS_RUNNING" = 1 ]; then
        setsid -f "$2" >/dev/null 2>&1 </dev/null && say "Restarted, reload the page in your browser"
    elif [ "$START" = 1 ] && { [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; }; then
        "$1" start >/dev/null && say "Started: http://127.0.0.1:$PORT/ (it opens in your browser)"
    else
        say "Start it with: agents-web   (or 'Agents' in the app menu)"
    fi
}

# --package: the .deb, .rpm or pacman package of the release
do_package() {
    local d rx m
    d="$(distro)"
    m="$(machine)"
    command -v python3 >/dev/null || { [ "$DEPS" = 1 ] && install_deps run; }
    command -v python3 >/dev/null || die "python3 is required"
    new_tmp
    read_release
    [ -n "$TAG" ] || die "could not read the release from ${SOURCE^}"
    local v="${TAG#v}"
    v="${v//./\\.}"
    case "$d" in
        debian) rx="agents-web_${v}_$([ "$m" = x86_64 ] && echo amd64 || echo arm64)\\.deb" ;;
        fedora|suse) rx="agents-web-${v}-[0-9]+\\.${m}\\.rpm" ;;
        arch) rx="agents-web-${v}-[0-9]+-${m}\\.pkg\\.tar\\.zst" ;;
        *) die "no package for '$d', run the installer without --package" ;;
    esac
    fetch_asset "$rx" || die "release $TAG has no package for $d on $m"
    stop_running
    case "$d" in
        debian) pm env DEBIAN_FRONTEND=noninteractive apt-get install -y "$ASSET" ;;
        fedora) pm dnf install -y "$ASSET" ;;
        suse) pm zypper --non-interactive install --allow-unsigned-rpm "$ASSET" ;;
        arch) pm pacman -U --noconfirm --needed "$ASSET" ;;
    esac || die "the package manager failed"
    if [ -d "$APP" ]; then
        say "Removing the copy that was installed for your user (chats and settings stay)"
        rm -rf "$APP"
        rm -f "$BIN/agents-web" "$SHARE/applications/agents-web.desktop" \
              "$SHARE/icons/hicolor/scalable/apps/agents-web.svg"
        sed -i "s|^Exec=$APP/agents-web-tray|Exec=$SYS_APP/agents-web-tray|" "$HOME/.config/autostart/agents-web.desktop" 2>/dev/null || true
    fi
    say "Installed Agents Web ${TAG#v} ($(basename "$ASSET"))"
    after_install /usr/bin/agents-web "$SYS_APP/agents-web-tray"
}

check_clis() {
    local ok=0
    if command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ]; then ok=1
    else
        warn "Claude Code CLI not found. Install it with:  curl -fsSL https://claude.ai/install.sh | bash"
    fi
    if command -v agy >/dev/null || [ -x "$HOME/.local/bin/agy" ]; then ok=1
    else
        warn "Antigravity CLI (agy) not found. Install it with:  curl -fsSL https://antigravity.google/cli/install.sh | bash"
    fi
    [ "$ok" = 1 ] || warn "Agents Web needs at least one of the two CLIs, signed in."
}

do_uninstall() {
    if [ ! -d "$APP" ] && [ -f "$SYS_APP/install-kind" ]; then
        local k
        k="$(cat "$SYS_APP/install-kind")"
        say "Agents Web is installed as a $k package. Remove it with:"
        case "$k" in
            deb) echo "   sudo apt remove agents-web" ;;
            rpm) echo "   sudo dnf remove agents-web   (openSUSE: sudo zypper remove agents-web)" ;;
            pacman) echo "   sudo pacman -R agents-web" ;;
        esac
        [ "$PURGE" = 1 ] && { stop_running; rm -rf "$APP_CONF" "$APP_DATA"; say "Deleted chats, uploads and settings"; }
        exit 0
    fi
    stop_running
    say "Removing Agents Web"
    rm -rf "$APP"
    rm -f "$BIN/agents-web" "$SHARE/applications/agents-web.desktop" \
          "$SHARE/icons/hicolor/scalable/apps/agents-web.svg" "$CONF/autostart/agents-web.desktop" \
          "$HOME/.config/autostart/agents-web.desktop"
    if [ "$PURGE" = 1 ]; then
        rm -rf "$APP_CONF" "$APP_DATA"
        say "Deleted chats, uploads and settings ($APP_CONF, $APP_DATA)"
        echo "   If you installed the PC certificate on a phone, remove it there too (user certificates)."
    else
        say "Kept your chats and settings in $APP_CONF and $APP_DATA (use --purge to delete them)"
    fi
    echo "   Packages installed for it were left in place. Your Claude Code and agy sessions are untouched."
}

main() {
    MODE=install REF="" SOURCE=github DEPS=1 START=1 PURGE=0 SRC="" FROM="" WAS_RUNNING=0 TMP="" ASSETS="" TAG=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --uninstall) MODE=uninstall ;;
            --purge) PURGE=1 ;;
            --package) MODE=package ;;
            --ref) REF="${2:?--ref needs a tag or branch}"; shift ;;
            --from) FROM="${2:?--from needs a folder}"; shift ;;
            --gitlab) SOURCE=gitlab ;;
            --no-deps) DEPS=0 ;;
            --no-start) START=0 ;;
            -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done
    if [ "$MODE" = uninstall ]; then
        [ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root"
        do_uninstall; exit 0
    fi
    # through sudo it would stop and clean up root's copy, not yours (a real root login may install the package)
    if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}${PKEXEC_UID:-}" ]; then
        die "run this as your normal user, without sudo: it asks for the password itself"
    fi
    if [ "$MODE" = package ]; then do_package; exit 0; fi
    [ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root (sudo is only used to install packages), or use --package"
    if [ -n "$FROM" ]; then
        SRC="$(cd "$FROM" && pwd)"
    else
        # a local checkout installs itself unless another version was asked for
        local here=""
        if [ -f "${BASH_SOURCE[0]:-}" ]; then here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
        if [ -z "$REF" ] && [ -n "$here" ] && [ -f "$here/src/server.py" ] && [ -f "$here/tray/agents-web-tray.c" ] \
           && [ "$here" != "$APP" ]; then
            SRC="$here"
        fi
    fi
    [ -z "$SRC" ] || [ -f "$SRC/src/server.py" ] || die "$SRC does not look like Agents Web"
    do_install
}

main "$@"
