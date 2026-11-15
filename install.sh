#!/usr/bin/env bash
# Agents Web installer for Arch Linux and Fedora (per user install, no root files of its own).
#
#   curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/Agents-Web/main/install.sh | bash
#   curl -fsSL https://gitlab.com/gabriel17166/agents-web/-/raw/main/install.sh | bash -s -- --gitlab
#
# Options:
#   --ref <tag|branch>  install that version instead of the latest release
#   --gitlab            download from the GitLab mirror instead of GitHub
#   --no-deps           do not install distribution packages
#   --no-start          do not start the app when the install ends
#   --uninstall         remove the app (chats and settings are kept)
#   --purge             with --uninstall, also delete chats, uploads, settings and extra account logins
#
# https://github.com/gabrielmf1998/Agents-Web (MIT license)
set -euo pipefail

GH_REPO="gabrielmf1998/Agents-Web"
GL_REPO="gabriel17166/agents-web"
APP="$HOME/.local/opt/agents-web"
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
    if command -v sudo >/dev/null; then sudo "$@"
    elif command -v pkexec >/dev/null; then pkexec "$@"
    else die "need sudo or pkexec to install packages (or rerun with --no-deps)"; fi
}

# package managers read answers from stdin, which is this script when piped from curl
pm() {
    if [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; then as_root "$@" </dev/tty
    else as_root "$@" "$NOASK"; fi
}

install_deps() {
    local id="" like=""
    if [ -r /etc/os-release ]; then
        id="$(. /etc/os-release; echo "${ID:-}")"
        like="$(. /etc/os-release; echo "${ID_LIKE:-}")"
    fi
    case " $id $like " in
        *" arch "*)
            local pkgs=(python polkit openssl qrencode iproute2 util-linux procps-ng gcc pkgconf systemd-libs xdg-utils curl tar)
            local missing
            missing="$(pacman -T "${pkgs[@]}" || true)"
            if [ -n "$missing" ]; then
                say "Installing packages: $(echo $missing)"
                NOASK=--noconfirm pm pacman -S --needed $missing \
                    || die "pacman failed (if the error is a 404, run 'sudo pacman -Syu' first)"
            fi ;;
        *" fedora "*)
            local pkgs=(python3 polkit openssl qrencode iproute util-linux procps-ng gcc systemd-devel xdg-utils curl tar)
            local missing=() p
            for p in "${pkgs[@]}"; do rpm -q --quiet "$p" || missing+=("$p"); done
            if [ "${#missing[@]}" -gt 0 ]; then
                say "Installing packages: ${missing[*]}"
                NOASK=-y pm dnf install "${missing[@]}" || die "dnf failed"
            fi ;;
        *)
            warn "Unsupported distribution '${id:-unknown}'. Install these yourself: python3, polkit (pkttyagent),"
            warn "openssl, qrencode, iproute2, util-linux (setpriv), procps (pgrep), gcc and the libsystemd headers." ;;
    esac
}

latest_ref() {
    local ref=""
    if [ "$SOURCE" = gitlab ]; then
        ref="$(curl -fsSL "https://gitlab.com/api/v4/projects/${GL_REPO//\//%2F}/releases" 2>/dev/null \
            | grep -o '"tag_name":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
    else
        ref="$(curl -fsSL "https://api.github.com/repos/$GH_REPO/releases/latest" 2>/dev/null \
            | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
    fi
    echo "${ref:-main}"
}

fetch_source() {
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    [ -n "$REF" ] || REF="$(latest_ref)"
    local url
    if [ "$SOURCE" = gitlab ]; then url="https://gitlab.com/$GL_REPO/-/archive/$REF/agents-web-$REF.tar.gz"
    else url="https://github.com/$GH_REPO/archive/$REF.tar.gz"; fi
    say "Downloading Agents Web $REF"
    curl -fsSL "$url" | tar -xz -C "$TMP" --strip-components=1 || die "download failed: $url"
    [ -f "$TMP/src/server.py" ] || die "the download does not look like Agents Web"
    SRC="$TMP"
}

stop_running() {
    WAS_RUNNING=0
    if pgrep -x agents-web-tray >/dev/null; then
        WAS_RUNNING=1
        say "Stopping the running Agents Web"
        pkill -x agents-web-tray || true
        for _ in $(seq 1 50); do pgrep -x agents-web-tray >/dev/null || break; sleep 0.1; done
    fi
}

do_install() {
    command -v curl >/dev/null || [ -n "$SRC" ] || die "curl is required"
    if [ "$DEPS" = 1 ]; then install_deps; fi
    command -v python3 >/dev/null || die "python3 is required"
    command -v gcc >/dev/null || die "gcc is required to build the tray icon"
    [ -n "$SRC" ] || fetch_source

    say "Building the tray icon"
    local tray
    tray="$(mktemp)"
    gcc -O2 -Wall -o "$tray" "$SRC/tray/agents-web-tray.c" -lsystemd -lm \
        || die "build failed (Arch: systemd-libs, Fedora: systemd-devel)"

    stop_running
    say "Installing to $APP"
    mkdir -p "$APP/bin" "$BIN" "$SHARE/applications" "$SHARE/icons/hicolor/scalable/apps"
    install -m 644 "$SRC/src/server.py" "$SRC/src/index.html" "$APP/"
    install -m 755 "$SRC/src/bin/pkexec" "$APP/bin/pkexec"
    install -m 755 "$SRC/install.sh" "$APP/install.sh"
    install -m 755 "$tray" "$APP/agents-web-tray"
    install -m 644 "$SRC/data/agents-web.svg" "$SRC/data/icon-192.png" "$SRC/data/icon-512.png" "$APP/"
    echo "$SOURCE" > "$APP/source"   # where Settings looks for updates
    rm -f "$tray"
    rm -rf "$APP/__pycache__"
    install -m 755 "$SRC/src/agents-web" "$BIN/agents-web"
    install -m 644 "$SRC/data/agents-web.svg" "$SHARE/icons/hicolor/scalable/apps/agents-web.svg"
    cat > "$SHARE/applications/agents-web.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Agents
GenericName=AI agents web interface
Comment=Use the Claude Code and Antigravity CLIs from your browser or phone
Exec=$BIN/agents-web start
Icon=agents-web
Terminal=false
Categories=Development;Utility;
Keywords=claude;antigravity;agent;ai;chat;
EOF
    if [ -f "$SHARE/icons/hicolor/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null; then
        gtk-update-icon-cache -q -t "$SHARE/icons/hicolor" 2>/dev/null || true
    fi

    say "Installed Agents Web $(sed -n 's/^VERSAO = "\(.*\)"/\1/p' "$APP/server.py")"
    check_clis
    case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN is not in your PATH: open 'Agents' from the app menu or run $BIN/agents-web";; esac
    if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
        echo "   firewalld is active. If your phone cannot reach the page, open the port on your LAN:"
        echo "   sudo firewall-cmd --permanent --add-port=$PORT/tcp && sudo firewall-cmd --reload"
    fi

    if [ "$START" = 1 ] && { [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; }; then
        if [ "$WAS_RUNNING" = 1 ]; then
            setsid -f "$APP/agents-web-tray" >/dev/null 2>&1 && say "Restarted, reload the page in your browser"
        else
            "$BIN/agents-web" start >/dev/null && say "Started: http://127.0.0.1:$PORT/ (it opens in your browser)"
        fi
    else
        say "Start it with: agents-web   (or 'Agents' in the app menu)"
    fi
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
    MODE=install REF="" SOURCE=github DEPS=1 START=1 PURGE=0 SRC="" WAS_RUNNING=0 NOASK=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --uninstall) MODE=uninstall ;;
            --purge) PURGE=1 ;;
            --ref) REF="${2:?--ref needs a tag or branch}"; shift ;;
            --gitlab) SOURCE=gitlab ;;
            --no-deps) DEPS=0 ;;
            --no-start) START=0 ;;
            -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) die "unknown option: $1" ;;
        esac
        shift
    done
    [ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root (sudo is only used to install packages)"
    if [ "$MODE" = uninstall ]; then do_uninstall; exit 0; fi
    # a local checkout installs itself unless another version was asked for
    local here=""
    if [ -f "${BASH_SOURCE[0]:-}" ]; then here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
    if [ -z "$REF" ] && [ -n "$here" ] && [ -f "$here/src/server.py" ] && [ -f "$here/tray/agents-web-tray.c" ]; then
        SRC="$here"
    fi
    do_install
}

main "$@"
