#!/usr/bin/env bash
# Builds the release files of Agents Web. Each target runs in a container with its tools (see
# .github/workflows/release.yml, which runs them all), or on any machine that has them:
#
#   packaging/build.sh deps <target>   install the build tools of a target (inside its container, as root)
#   packaging/build.sh tray            the Linux tray icon, built on an old glibc   (almalinux:8)
#   packaging/build.sh tarball         agents-web-<v>-linux-x86_64.tar.gz for install.sh
#   packaging/build.sh deb             agents-web_<v>_amd64.deb                    (ubuntu:22.04)
#   packaging/build.sh rpm             agents-web-<v>-1.x86_64.rpm                 (almalinux:9)
#   packaging/build.sh pacman          agents-web-<v>-1-x86_64.pkg.tar.zst         (archlinux)
#   packaging/build.sh appimage        Agents_Web-<v>-x86_64.AppImage, with its own Python  (ubuntu:22.04)
#   packaging/build.sh windows         AgentsWeb-Setup-<v>.exe, with its own Python (ubuntu:24.04)
#   packaging/build.sh sums            SHA256SUMS of everything in dist/
#
# Output goes to dist/ (or $DIST). VERSION overrides the version (tests build an older one to update from).
# Downloads are pinned to their SHA256 sum as published by python.org and GitHub.
# https://github.com/gabrielmf1998/Agents-Web (MIT license)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$(mkdir -p "${DIST:-$ROOT/dist}" && cd "${DIST:-$ROOT/dist}" && pwd)"
VERSION="${VERSION:-$(sed -n 's/^VERSAO = "\(.*\)"/\1/p' "$ROOT/src/server.py")}"
ARCH=x86_64
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Python for Windows: the embeddable package from python.org (sha256 from the python.org downloads API)
PY_WIN_URL="https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip"
PY_WIN_SHA="90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"
# Python for the AppImage: python-build-standalone (the builds uv uses), sha256 from its release
PY_LIN_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.14.10%2B20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PY_LIN_SHA="c2ff2e97422f5d3896a16e0317fd212ba977bbf2688d93a6f7a03d817506bd30"
# AppImage runtime from the AppImage project (release 20251108)
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

fetch() { # url sha256 file
    curl -fsSL --retry 3 -o "$3" "$1" || die "download failed: $1"
    echo "$2  $3" | sha256sum -c --quiet - || die "$1 does not match its pinned SHA256"
}

tray_bin() {
    local t="$DIST/agents-web-tray-$ARCH"
    [ -x "$t" ] || die "build the tray first (packaging/build.sh tray)"
    echo "$t"
}

# the app as it lives in /usr/lib/agents-web (or the AppImage, or a per user copy): $1 folder, $2 install kind
stage_app() {
    local d="$1"
    mkdir -p "$d/bin"
    sed "s/^VERSAO = \".*\"/VERSAO = \"$VERSION\"/" "$ROOT/src/server.py" > "$d/server.py"
    install -m 644 "$ROOT/src/index.html" "$ROOT/data/agents-web.svg" "$ROOT/data/icon-192.png" \
        "$ROOT/data/icon-512.png" "$d/"
    install -m 755 "$ROOT/src/bin/pkexec" "$d/bin/pkexec"
    [ "$2" = windows ] || install -m 755 "$(tray_bin)" "$d/agents-web-tray"
    echo "$2" > "$d/install-kind"
}

desktop_file() { # $1 file, $2 Exec line
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Name=Agents
GenericName=AI agents web interface
Comment=Use the Claude Code and Antigravity CLIs from your browser or phone
Exec=$2
Icon=agents-web
Terminal=false
Categories=Development;Utility;
Keywords=claude;antigravity;agent;ai;chat;
EOF
}

# the file tree of the system packages, in $1
stage_system() {
    local r="$1"
    stage_app "$r/usr/lib/agents-web" "$2"
    mkdir -p "$r/usr/bin" "$r/usr/share/applications" "$r/usr/share/icons/hicolor/scalable/apps" \
        "$r/usr/share/icons/hicolor/512x512/apps" "$r/usr/share/doc/agents-web"
    sed 's|^APP_DEFAULT=.*|APP_DEFAULT="/usr/lib/agents-web"|' "$ROOT/src/agents-web" > "$r/usr/bin/agents-web"
    chmod 755 "$r/usr/bin/agents-web"
    desktop_file "$r/usr/share/applications/agents-web.desktop" "agents-web start"
    install -m 644 "$ROOT/data/agents-web.svg" "$r/usr/share/icons/hicolor/scalable/apps/agents-web.svg"
    install -m 644 "$ROOT/data/icon-512.png" "$r/usr/share/icons/hicolor/512x512/apps/agents-web.png"
    install -m 644 "$ROOT/README.md" "$ROOT/CHANGELOG.md" "$r/usr/share/doc/agents-web/"
}

SUMMARY="Web interface for the Claude Code and Antigravity CLIs"
DESCRIPTION="Use the Claude Code and Antigravity command line agents from a browser on your PC or your phone:
chats with live previews, approvals, several Claude accounts and real usage limits. A small local server
(Python standard library) and a tray icon; the agents run as your user."

deps() {
    case "$1" in
        tray) dnf install -y -q gcc systemd-devel binutils ;;
        deb|tarball) apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates curl ;;
        rpm) dnf install -y -q rpm-build ;;
        pacman) pacman -Syu --noconfirm --needed base-devel ;;
        appimage) apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
                      ca-certificates curl squashfs-tools file ;;
        windows) apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
                     ca-certificates curl nsis gcc-mingw-w64-x86-64 binutils-mingw-w64-x86-64 unzip python3 ;;
        *) die "unknown target $1" ;;
    esac
}

build_tray() {
    say "Tray icon (glibc $(ldd --version | head -1 | awk '{print $NF}'))"
    gcc -O2 -Wall -Wextra -o "$DIST/agents-web-tray-$ARCH" "$ROOT/tray/agents-web-tray.c" -lsystemd -lm
    strip "$DIST/agents-web-tray-$ARCH"
    "$DIST/agents-web-tray-$ARCH" --version
}

build_tarball() {
    local n="agents-web-$VERSION" d
    d="$WORK/$n"
    mkdir -p "$d/src/bin" "$d/data" "$d/tray"
    sed "s/^VERSAO = \".*\"/VERSAO = \"$VERSION\"/" "$ROOT/src/server.py" > "$d/src/server.py"
    cp "$ROOT/src/index.html" "$ROOT/src/agents-web" "$d/src/"
    cp "$ROOT/src/bin/pkexec" "$d/src/bin/"
    cp "$ROOT"/data/agents-web.svg "$ROOT"/data/icon-*.png "$d/data/"
    cp "$ROOT/tray/agents-web-tray.c" "$d/tray/"
    install -m 755 "$(tray_bin)" "$d/tray/agents-web-tray"
    cp "$ROOT/install.sh" "$ROOT/LICENSE" "$ROOT/README.md" "$ROOT/CHANGELOG.md" "$d/"
    tar --sort=name --owner=0 --group=0 --numeric-owner -C "$WORK" -czf "$DIST/$n-linux-$ARCH.tar.gz" "$n"
    say "$DIST/$n-linux-$ARCH.tar.gz"
}

build_deb() {
    local r="$WORK/deb" size
    stage_system "$r" deb
    install -m 644 "$ROOT/LICENSE" "$r/usr/share/doc/agents-web/copyright"
    mkdir -p "$r/DEBIAN"
    size="$(du -sk --exclude=DEBIAN "$r" | cut -f1)"
    cat > "$r/DEBIAN/control" <<EOF
Package: agents-web
Version: $VERSION-1
Architecture: amd64
Maintainer: Agents Web <110578985+gabrielmf1998@users.noreply.github.com>
Installed-Size: $size
Depends: python3 (>= 3.9), libsystemd0 (>= 247), util-linux, iproute2, procps, curl, ca-certificates
Recommends: openssl, qrencode, xdg-utils, polkitd | policykit-1, pkexec | policykit-1
Section: devel
Priority: optional
Homepage: https://github.com/gabrielmf1998/Agents-Web
Description: $SUMMARY
$(sed 's/^/ /' <<<"$DESCRIPTION")
EOF
    find "$r" -type d -exec chmod 755 {} +
    dpkg-deb --root-owner-group -Zxz --build "$r" "$DIST/agents-web_${VERSION}_amd64.deb" >/dev/null
    say "$DIST/agents-web_${VERSION}_amd64.deb"
}

build_rpm() {
    local top="$WORK/rpm"
    mkdir -p "$top"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
    stage_system "$top/SOURCES/root" rpm
    install -m 644 "$ROOT/LICENSE" "$top/SOURCES/root/usr/share/doc/agents-web/LICENSE"
    cat > "$top/SPECS/agents-web.spec" <<EOF
%global debug_package %{nil}
%global __brp_python_bytecompile %{nil}
%global __brp_mangle_shebangs %{nil}
%global _build_id_links none
Name: agents-web
Version: $VERSION
Release: 1
Summary: $SUMMARY
License: MIT
URL: https://github.com/gabrielmf1998/Agents-Web
ExclusiveArch: x86_64
Requires: python3 >= 3.9
Requires: (systemd-libs or libsystemd0)
Requires: util-linux
Requires: (iproute or iproute2)
Requires: (procps-ng or procps)
Requires: curl
Recommends: openssl
Recommends: qrencode
Recommends: polkit
Recommends: xdg-utils

%description
$DESCRIPTION

%install
cp -a %{_sourcedir}/root/. %{buildroot}/

%files
%license /usr/share/doc/agents-web/LICENSE
/usr/bin/agents-web
/usr/lib/agents-web
/usr/share/applications/agents-web.desktop
/usr/share/icons/hicolor/scalable/apps/agents-web.svg
/usr/share/icons/hicolor/512x512/apps/agents-web.png
%doc /usr/share/doc/agents-web/README.md
%doc /usr/share/doc/agents-web/CHANGELOG.md
EOF
    rpmbuild --quiet --define "_topdir $top" -bb "$top/SPECS/agents-web.spec"
    cp "$top/RPMS/$ARCH/agents-web-$VERSION-1.$ARCH.rpm" "$DIST/"
    say "$DIST/agents-web-$VERSION-1.$ARCH.rpm"
}

build_pacman() {
    local b="$WORK/arch"
    mkdir -p "$b"
    stage_system "$b/root" pacman
    install -Dm 644 "$ROOT/LICENSE" "$b/root/usr/share/licenses/agents-web/LICENSE"
    cat > "$b/PKGBUILD" <<EOF
pkgname=agents-web
pkgver=$VERSION
pkgrel=1
pkgdesc="$SUMMARY"
arch=(x86_64)
url="https://github.com/gabrielmf1998/Agents-Web"
license=(MIT)
depends=(python systemd-libs util-linux iproute2 procps-ng curl)
optdepends=('openssl: https for the phone' 'qrencode: pairing QR code' 'polkit: root approvals in the page'
            'xdg-utils: open the page from the tray')
options=(!debug !strip !emptydirs)

package() {
    cp -a "\$startdir/root/." "\$pkgdir/"
}
EOF
    if [ "$(id -u)" -eq 0 ]; then
        id builder >/dev/null 2>&1 || useradd -m builder
        chown -R builder "$b"
        chmod 755 "$WORK"
        (cd "$b" && runuser -u builder -- makepkg --nodeps --noconfirm -f >/dev/null)
    else
        (cd "$b" && makepkg --nodeps --noconfirm -f >/dev/null)
    fi
    cp "$b/agents-web-$VERSION-1-$ARCH.pkg.tar.zst" "$DIST/"
    say "$DIST/agents-web-$VERSION-1-$ARCH.pkg.tar.zst"
}

build_appimage() {
    local a="$WORK/AppDir" app
    app="$a/usr/lib/agents-web"
    stage_app "$app" appimage
    say "Python for the AppImage"
    fetch "$PY_LIN_URL" "$PY_LIN_SHA" "$WORK/python.tar.gz"
    tar -xzf "$WORK/python.tar.gz" -C "$app"
    # parts of the standard library the server never uses
    (cd "$app/python" && rm -rf include share lib/pkgconfig lib/libpython3*.a lib/itcl* lib/tcl* lib/tk* \
        lib/python3.*/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data,venv,config-*} \
        lib/python3.*/lib-dynload/_tkinter* bin/idle3* bin/pydoc3* bin/pip* lib/python3.*/site-packages/pip*)
    find "$app/python" -name __pycache__ -prune -exec rm -rf {} +
    "$app/python/bin/python3" -c 'import ssl, sqlite3, json, http.server; print("python", __import__("sys").version.split()[0])'
    install -m 755 "$ROOT/packaging/AppRun" "$a/AppRun"
    desktop_file "$a/agents-web.desktop" "agents-web"
    cp "$ROOT/data/agents-web.svg" "$a/agents-web.svg"
    cp "$ROOT/data/icon-512.png" "$a/.DirIcon"
    mkdir -p "$a/usr/share/metainfo"
    say "AppImage runtime"
    fetch "$RUNTIME_URL" "$RUNTIME_SHA" "$WORK/runtime"
    mksquashfs "$a" "$WORK/app.squashfs" -root-owned -noappend -comp zstd -Xcompression-level 19 -quiet >/dev/null
    cat "$WORK/runtime" "$WORK/app.squashfs" > "$DIST/Agents_Web-$VERSION-$ARCH.AppImage"
    chmod 755 "$DIST/Agents_Web-$VERSION-$ARCH.AppImage"
    say "$DIST/Agents_Web-$VERSION-$ARCH.AppImage ($(du -h "$DIST/Agents_Web-$VERSION-$ARCH.AppImage" | cut -f1))"
}

build_windows() {
    local s="$WORK/stage"
    mkdir -p "$s/app" "$s/python"
    stage_app "$s/app" windows
    rm -rf "$s/app/bin"
    python3 "$ROOT/packaging/ico.py" "$ROOT/data/icon-192.png" "$s/agents-web.ico"
    say "Tray program"
    cat > "$WORK/res.rc" <<EOF
1 ICON "$s/agents-web.ico"
1 VERSIONINFO
FILEVERSION ${VERSION//./,},0
PRODUCTVERSION ${VERSION//./,},0
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040904B0"
    BEGIN
      VALUE "FileDescription", "Agents Web"
      VALUE "ProductName", "Agents Web"
      VALUE "ProductVersion", "$VERSION"
      VALUE "FileVersion", "$VERSION"
      VALUE "OriginalFilename", "AgentsWeb.exe"
      VALUE "LegalCopyright", "MIT license"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x409, 1200
  END
END
EOF
    x86_64-w64-mingw32-windres "$WORK/res.rc" -O coff -o "$WORK/res.o"
    x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -municode -mwindows -static -o "$s/AgentsWeb.exe" \
        "$ROOT/windows/agents-web-tray.c" "$WORK/res.o" -lws2_32 -lshell32 -luser32 -lgdi32 -ladvapi32
    x86_64-w64-mingw32-strip "$s/AgentsWeb.exe"
    say "Python for Windows"
    fetch "$PY_WIN_URL" "$PY_WIN_SHA" "$WORK/python.zip"
    unzip -q "$WORK/python.zip" -d "$s/python"
    cp "$ROOT/LICENSE" "$s/LICENSE.txt"
    say "Installer"
    makensis -V2 -DVERSION="$VERSION" -DSTAGE="$s" -DOUT="$DIST/AgentsWeb-Setup-$VERSION.exe" "$ROOT/windows/installer.nsi"
    say "$DIST/AgentsWeb-Setup-$VERSION.exe ($(du -h "$DIST/AgentsWeb-Setup-$VERSION.exe" | cut -f1))"
}

sums() {
    (cd "$DIST" && sha256sum -- *.deb *.rpm *.pkg.tar.zst *.AppImage *.exe *.tar.gz 2>/dev/null > SHA256SUMS) || true
    cat "$DIST/SHA256SUMS"
}

case "${1:-}" in
    deps) deps "${2:?which target}" ;;
    tray) build_tray ;;
    tarball) build_tarball ;;
    deb) build_deb ;;
    rpm) build_rpm ;;
    pacman) build_pacman ;;
    appimage) build_appimage ;;
    windows) build_windows ;;
    sums) sums ;;
    *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
