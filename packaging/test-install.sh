#!/bin/bash
# Install test of the Linux builds, run as root inside a container of a distribution (see .github/workflows/ci.yml):
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/dist" debian:12 bash /src/packaging/test-install.sh pkg
# MODE: pkg (the .deb, .rpm or pacman package), script (install.sh piped from curl) or appimage.
# It installs the older build in /dist/old, starts it as a normal user (tray, session bus), updates it to the build
# in /dist the way the page does, and removes it. /dist/release.json and /dist/old/release.json are fake releases
# with file:// links (packaging/fake-release.py DIR file:///dist).
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root
M="$1"; P="${2:-47830}"
tag() { sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' "$1"; }
OLD="$(tag /dist/old/release.json)"; NEW="$(tag /dist/release.json)"
[ -n "$OLD" ] && [ -n "$NEW" ] || { echo "no fake releases in /dist and /dist/old"; exit 2; }
. /etc/os-release
FAILS=0
ok()   { echo "PASS [$ID $M] $*"; }
bad()  { echo "FAIL [$ID $M] $*"; FAILS=$((FAILS + 1)); }
step() { echo "---- $*"; }
U() { sudo -u tester -H env PATH=/home/tester/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
      XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus AGENTS_WEB_PORT="$P" \
      LANG=C.UTF-8 "$@"; }
# processes of tester by name, from /proc (no procps needed)
tpids() { for d in /proc/[0-9]*; do [ "$(stat -c %u "$d" 2>/dev/null)" = 1000 ] && [ "$(cat "$d/comm" 2>/dev/null)" = "$1" ] && echo "${d#/proc/}"; done; }
tserver() { for d in /proc/[0-9]*; do [ "$(stat -c %u "$d" 2>/dev/null)" = 1000 ] && tr '\0' ' ' <"$d/cmdline" 2>/dev/null | grep -q 'server\.py' && echo "${d#/proc/}"; done; }

if command -v apt-get >/dev/null; then F=deb; elif command -v zypper >/dev/null; then F=suse
elif command -v dnf >/dev/null; then F=dnf; else F=arch; fi

step "base tools"
case $F in
    deb) apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sudo curl ca-certificates dbus >/dev/null ;;
    dnf) dnf install -y -q sudo dbus-daemon >/dev/null ;;
    suse) zypper -q -n in sudo curl dbus-1-daemon >/dev/null ;;
    arch) pacman -Syu --noconfirm --needed sudo dbus >/dev/null 2>&1 ;;
esac || bad "base tools"
if [ "$M" = appimage ]; then   # desktops have fusermount3; procps is left out on purpose
    case $F in deb) apt-get install -y -qq fuse3 >/dev/null ;; dnf) dnf install -y -q fuse3 >/dev/null ;;
               suse) zypper -q -n in fuse3 >/dev/null ;; arch) pacman -S --noconfirm --needed fuse3 >/dev/null 2>&1 ;; esac || bad "fuse3"
fi
H="$(cat /proc/sys/kernel/hostname)"; grep -qs "$H" /etc/hosts || echo "127.0.1.1 $H" >> /etc/hosts
getent passwd 1000 | cut -d: -f1 | grep -qv tester && userdel -r "$(getent passwd 1000 | cut -d: -f1)" 2>/dev/null
id tester >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash tester
echo 'tester ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/tester; chmod 440 /etc/sudoers.d/tester
mkdir -p /run/user/1000 /home/tester/.local/bin && chown -R tester: /run/user/1000 /home/tester && chmod 700 /run/user/1000
# the stand in for pkexec: no polkit daemon in here, so sudo does its job
printf '#!/bin/sh\nexec sudo -n "$@"\n' > /home/tester/.local/bin/pkexec; chmod 755 /home/tester/.local/bin/pkexec
chown tester: /home/tester/.local/bin/pkexec
# a session bus that outlives the trays, as on a desktop
U setsid -f dbus-daemon --session --address=unix:path=/run/user/1000/bus --nopidfile --nofork >/dev/null 2>&1 </dev/null
espera() { # condition, seconds
    local i=0; while [ $i -lt $(($2 * 5)) ]; do eval "$1" >/dev/null 2>&1 && return 0; sleep 0.2; i=$((i + 1)); done; return 1; }
espera '[ -S /run/user/1000/bus ]' 10 || bad "session bus"

token() { python3 -c 'import json;print(json.load(open("/home/tester/.config/agents-web/config.json"))["token"])' 2>/dev/null \
          || sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' /home/tester/.config/agents-web/config.json; }
saude() { curl -fsS -m 3 "http://127.0.0.1:$P/saude" 2>/dev/null; }
api() { curl -fsS -m 30 -H "X-Token: $(token)" "$@" 2>&1; }
versao() { api "http://127.0.0.1:$P/api/estado" | grep -o '"versao": *"[^"]*"' | grep -o '[0-9.]*[0-9]'; }
start_tray() { # $1: command to start the tray
    U bash -c "cd ~; AGENTS_WEB_RELEASES_API=file:///dist/release.json AGENTS_WEB_UPDATE_DELAY=3600 setsid -f $1 >/tmp/tray-$P.log 2>&1 </dev/null"
    if espera saude 40; then ok "tray started the server ($(saude | head -c 60))"; else bad "server did not answer"; cat /tmp/tray-$P.log; fi
}

update_in_page() { # $1: expected kind
    local d
    d="$(api "http://127.0.0.1:$P/api/atualizacao?forcar=1")"
    if echo "$d" | grep -q '"nova": true' && echo "$d" | grep -q "\"tipo\": \"$1\""; then ok "page sees $NEW for kind $1"
    else bad "update check: $d"; fi
    d="$(api -X POST -H 'Content-Type: application/json' -d '{}' "http://127.0.0.1:$P/api/atualizar")"
    echo "$d" | grep -q '"ok": true' || bad "POST /api/atualizar: $d"
    if espera '[ "$(versao)" = $NEW ]' 120; then ok "updated in the page, now $(versao)"
    else bad "still $(versao) after the update"; tail -30 /home/tester/.local/share/agents-web/update.log; fi
}

stop_all() {
    sleep 1
    [ -n "$(tpids agents-web-tray)" ] || bad "no tray was running"
    tpids agents-web-tray | while read -r t; do kill "$t" 2>/dev/null; done
    espera '[ -z "$(tserver)" ]' 15 && ok "quitting the tray stopped the server" || bad "server left running"
    espera '[ -z "$(tpids agents-web-tray)" ]' 15 || bad "tray did not exit"
}

case $M in
pkg)
    case $F in
        deb) A=/dist/old/agents-web_${OLD}_amd64.deb; I=(env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq); R=(apt-get remove -y -qq agents-web) ;;
        dnf) A=/dist/old/agents-web-$OLD-1.x86_64.rpm; I=(dnf install -y -q); R=(dnf remove -y -q agents-web) ;;
        suse) A=/dist/old/agents-web-$OLD-1.x86_64.rpm; I=(zypper -q -n in --allow-unsigned-rpm); R=(zypper -q -n rm agents-web) ;;
        arch) A=/dist/old/agents-web-$OLD-1-x86_64.pkg.tar.zst; I=(pacman -U --noconfirm); R=(pacman -R --noconfirm agents-web) ;;
    esac
    step "install $A"
    if "${I[@]}" "$A" > /tmp/i.log 2>&1; then ok "package manager installed $(basename "$A") with its dependencies"
    else bad "install"; tail -20 /tmp/i.log; fi
    [ "$(U agents-web version)" = $OLD ] && ok "agents-web version = $OLD" || bad "version $(U agents-web version)"
    /usr/lib/agents-web/agents-web-tray --version >/dev/null && ok "prebuilt tray runs here" || bad "tray binary"
    step "installed server against the test suite"
    cp -r /src /tmp/aw && chown -R tester: /tmp/aw
    if U env PYTHONDONTWRITEBYTECODE=1 AGENTS_WEB_TEST_SERVER=/usr/lib/agents-web/server.py python3 -m unittest discover -s /tmp/aw/tests 2>&1 \
        | tail -4 | tee /tmp/u.log | grep -q '^OK'; then ok "unit tests against the installed copy ($(python3 --version))"
    else bad "unit tests"; cat /tmp/u.log; fi
    start_tray /usr/lib/agents-web/agents-web-tray
    [ "$(U agents-web status)" != stopped ] && ok "agents-web status: $(U agents-web status)" || bad "status"
    step "update in the page (pkexec stand in runs the package manager)"
    update_in_page "$(cat /usr/lib/agents-web/install-kind)"
    [ "$(U agents-web version)" = $NEW ] && ok "package is now $NEW" || bad "package version $(U agents-web version)"
    # against the fake release: the real GitHub API limits unauthenticated calls from shared CI addresses
    if U env AGENTS_WEB_RELEASES_API=file:///dist/release.json agents-web update >/tmp/up.log 2>&1 && grep -q "is the latest version" /tmp/up.log
    then ok "agents-web update: already latest"; else bad "terminal update"; cat /tmp/up.log; fi
    stop_all
    step "remove"
    "${R[@]}" >/tmp/r.log 2>&1 && [ ! -e /usr/lib/agents-web ] && ok "package removed, /usr/lib/agents-web gone" || { bad "remove"; tail /tmp/r.log; } ;;
script)
    step "curl installer, user copy of $OLD"
    if U bash -c 'curl -fsSL file:///src/install.sh | AGENTS_WEB_RELEASES_API=file:///dist/old/release.json bash' >/tmp/i.log 2>&1; then
        ok "install.sh piped from curl"; else bad "install.sh"; fi
    grep -E "Installing packages|SHA256|Building|Installed" /tmp/i.log | sed 's/\x1b\[[0-9;]*m//g'
    grep -q "Building the tray" /tmp/i.log && bad "it compiled the tray instead of using the prebuilt one" || ok "prebuilt tray used (no gcc)"
    [ "$(U agents-web version)" = $OLD ] && ok "agents-web version = $OLD" || bad "version $(U agents-web version)"
    start_tray /home/tester/.local/opt/agents-web/agents-web-tray
    step "update in the page (no password)"
    update_in_page script
    [ "$(U agents-web version)" = $NEW ] && ok "user copy is now $NEW" || bad "version $(U agents-web version)"
    [ -n "$(tpids agents-web-tray)" ] && ok "tray running again after the update" || bad "tray not restarted"
    step "install.sh --package over the user copy"
    U bash -c 'sudo bash /src/install.sh --package' >/tmp/p.log 2>&1 && bad "install.sh --package ran through sudo" \
        || { grep -q "without sudo" /tmp/p.log && ok "install.sh refuses to run through sudo" || { bad "sudo guard"; cat /tmp/p.log; }; }
    if U bash -c 'curl -fsSL file:///src/install.sh | AGENTS_WEB_RELEASES_API=file:///dist/release.json bash -s -- --package' \
        >/tmp/p.log 2>&1 && [ -f /usr/lib/agents-web/install-kind ] && [ ! -e /home/tester/.local/opt/agents-web ]; then
        ok "installed the $(cat /usr/lib/agents-web/install-kind) package and removed the user copy"
    else bad "install.sh --package"; tail -20 /tmp/p.log; fi
    [ "$(U agents-web version)" = $NEW ] && ok "/usr/bin/agents-web reports $NEW" || bad "package version $(U agents-web version)"
    [ -f /home/tester/.config/agents-web/config.json ] && ok "settings kept across the move" || bad "settings lost"
    # cmdline, not exe: a container without CAP_SYS_PTRACE cannot read the exe link of another user's process
    if espera saude 40 && tr '\0' ' ' <"/proc/$(tpids agents-web-tray | head -1)/cmdline" | grep -q '^/usr/lib/agents-web/agents-web-tray'; then
        ok "the running copy started again from the package"; else bad "tray not restarted from the package"; fi
    stop_all
    step "remove the package and purge"
    case $F in
        deb) RM=(apt-get remove -y -qq agents-web) ;; dnf) RM=(dnf remove -y -q agents-web) ;;
        suse) RM=(zypper -q -n rm agents-web) ;; arch) RM=(pacman -R --noconfirm agents-web) ;;
    esac
    "${RM[@]}" >/tmp/r.log 2>&1 || { bad "package remove"; tail /tmp/r.log; }
    U bash /src/install.sh --uninstall --purge >/tmp/r.log 2>&1
    [ ! -e /usr/lib/agents-web ] && [ ! -e /home/tester/.local/bin/agents-web ] && [ ! -e /home/tester/.config/agents-web ] \
        && ok "package removed, uninstall --purge removed the data" || { bad "uninstall"; cat /tmp/r.log; } ;;
appimage)
    step "AppImage $OLD"
    install -o tester -m 755 -D /dist/old/Agents_Web-$OLD-x86_64.AppImage /home/tester/Applications/Agents_Web.AppImage
    chown -R tester: /home/tester/Applications
    AI=/home/tester/Applications/Agents_Web.AppImage
    if U "$AI" version >/tmp/v.log 2>&1; then MODE=""; else MODE="APPIMAGE_EXTRACT_AND_RUN=1"; echo "(no FUSE here: $(head -c 200 /tmp/v.log); extract and run)"; fi
    [ "$(U env $MODE "$AI" version 2>/dev/null)" = $OLD ] && ok "AppImage runs, version $OLD ${MODE:+($MODE)}" || bad "AppImage version $(U env $MODE "$AI" version 2>&1 | tail -3)"
    start_tray "env $MODE $AI tray"
    step "update in the page (replaces the file)"
    update_in_page appimage
    [ "$(U env $MODE "$AI" version 2>/dev/null)" = $NEW ] && ok "AppImage file is now $NEW" || bad "file version"
    stop_all
    U env $MODE "$AI" status >/dev/null; [ $? = 3 ] && ok "AppImage status: stopped" || bad "status after stop" ;;
esac
exit $FAILS
