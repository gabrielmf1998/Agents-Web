# Agents Web

A light, local web interface for the **Claude Code CLI** (`claude`) and the **Antigravity CLI** (`agy`).
Chat with your coding agents from the browser on your PC, or from your phone on the same Wi-Fi,
while they keep running on your own machine with your own logins, files and tools.

It is a single Python file (standard library only), one HTML page and a tiny C tray icon.
It doesn't run a cloud service or telemetry, and it doesn't need an API key: it drives the CLIs
you already use, so your existing subscriptions, settings and terminal sessions all carry over.

> Unofficial project. Not affiliated with, endorsed by or sponsored by Anthropic or Google.
> Claude, Claude Code and Antigravity are trademarks of their respective owners.

## Features

- **Both agents, one page.** Switch between Claude Code and Antigravity per chat, each with its own theme.
- **Several Claude accounts.** Add extra Claude logins and pick the account per chat. Each account has its own model, effort and permission choices. When one account reaches its usage limit, the chat continues the same conversation on another one, with one click or by itself. All accounts share one Claude memory.
- **Real usage limits.** Shows session and weekly limits as reported by the CLIs themselves, plus tokens per model.
- **Terminal conversations.** Your existing `claude` and `agy` sessions show up in the sidebar. You can open, continue, rename or delete them, and a Claude chat can move to another working folder.
- **Rich replies.** Markdown with tables, live previews of `html` and `svg` blocks in a sandboxed frame that follows the page theme, and inline images, videos, audio and PDFs for absolute file paths the agent mentions.
- **Phone friendly.** Pair a phone with a 6 digit PIN or a QR code. You get https on the same port with a private local CA, voice dictation and notifications when a reply is ready.
- **Approvals, questions and plans (Claude).** Permission prompts, questions from Claude and plans to approve show up as cards in the chat, with a diff for every edit.
- **Edit, retry and fork (Claude).** Change a message you sent and get a new reply, or fork any reply into a new chat. The original conversation stays in Claude Code.
- **Root approvals in the browser.** When an agent runs `pkexec`, a card shows the command and you approve it with your password (https or the PC only) or deny it.
- **Smart titles.** New chats get a short title written by Claude Haiku from your first message, shown while the reply is still coming in. One click in Settings names your older chats too. Empty chats you leave behind do not pile up in the sidebar.
- **Room to read.** Fold the chat list into a thin icon rail (Ctrl+B), pick a Normal, Wide or Full chat width and one of four text sizes, in a dark or light theme (or follow the system).
- **Code that reads well.** Syntax highlighting for the common languages without any library, Copy and Wrap buttons on every block, and runs of tool calls folded into one summary line with the lines changed.
- **Saved prompts.** Keep the prompts you reuse and insert one with "/" at the start of a message or the prompts button. They live on the PC, so the phone has them too.
- **Workflow helpers.** Message queue while the agent works, full text search, pinned chats, recent chats on the start screen, keyboard shortcuts, an image viewer, a draft kept per chat, export as Markdown, working folder picker with "New folder", file uploads, resizable sidebar, a jump to latest button and a stop button.
- **Linux and Windows.** Packages for Debian and Ubuntu (.deb), Fedora, RHEL and openSUSE (.rpm) and Arch (pacman), an AppImage for any other distribution, a one line install for your user, and an installer for Windows 10 and 11. The page can also be installed as an app.
- **Updates by themselves.** New releases install at a quiet moment, after a SHA256 check of the download. Settings can switch that to a notice only, or off. Packages, which need your password, show the notice and install with one click.
- **No dependencies.** The server uses only the Python standard library, the page needs no build step or CDN, and the tray uses under 4 MB of RAM.

## Requirements

- **Linux** (x86_64) with a desktop session (KDE Plasma, GNOME, etc), systemd 247 or newer and Python 3.9 or newer.
  Every release is installed, updated and removed in CI on Debian 12 and 13, Ubuntu 22.04 and 24.04, Fedora,
  AlmaLinux 9, openSUSE Leap 16.0 and Tumbleweed and Arch Linux. Ubuntu 20.04 is too old (systemd 245).
- **Windows 10 or 11** (64 bit). Tested in CI on the Windows Server 2022 and 2025 runners of GitHub.
- At least one of the CLIs installed and signed in:
  - Claude Code: `curl -fsSL https://claude.ai/install.sh | bash` (Windows PowerShell: `irm https://claude.ai/install.ps1 | iex`), then run `claude` once to sign in.
  - Antigravity CLI: `curl -fsSL https://antigravity.google/cli/install.sh | bash`, then run `agy` once to sign in.
- On Linux the installer adds what is missing from: Python 3, curl, polkit, OpenSSL, qrencode, iproute2,
  util-linux and procps. The packages ask for the same ones. The AppImage brings its own Python.

## Install

### Linux

One line installs a copy for your user that updates itself without a password:

```bash
curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/Agents-Web/main/install.sh | bash
```

From the GitLab mirror:

```bash
curl -fsSL https://gitlab.com/gabriel17166/agents-web/-/raw/main/install.sh | bash -s -- --gitlab
```

It asks for your password (sudo) only to add missing distribution packages, and shows the list first. The
download is checked against the `SHA256SUMS` of the release, and the tray icon comes prebuilt.

To get the system package of your distribution instead (.deb, .rpm or pacman, picked for you), add
`--package`. It asks for your password, and updates do too:

```bash
curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/Agents-Web/main/install.sh | bash -s -- --package
```

Or download a file from the [latest release](https://github.com/gabrielmf1998/Agents-Web/releases/latest)
(the same files are on [GitLab](https://gitlab.com/gabriel17166/agents-web/-/releases)) and install it yourself:

| Distribution | File | Install with |
|---|---|---|
| Debian, Ubuntu and derivatives | `agents-web_<version>_amd64.deb` | `sudo apt install ./agents-web_*_amd64.deb` |
| Fedora, RHEL, AlmaLinux, Rocky | `agents-web-<version>-1.x86_64.rpm` | `sudo dnf install ./agents-web-*.x86_64.rpm` |
| openSUSE | the same `.rpm` | `sudo zypper install --allow-unsigned-rpm ./agents-web-*.x86_64.rpm` |
| Arch and derivatives | `agents-web-<version>-1-x86_64.pkg.tar.zst` | `sudo pacman -U ./agents-web-*.pkg.tar.zst` |
| any other | `Agents_Web-<version>-x86_64.AppImage` | `chmod +x Agents_Web-*.AppImage`, then run it |

The packages are not signed with a repository key. Check a download against `SHA256SUMS` with
`sha256sum -c SHA256SUMS --ignore-missing`.

| Path | What |
|---|---|
| `~/.local/opt/agents-web/` | the app, for the per user install (server, page, tray icon, pkexec helper) |
| `/usr/lib/agents-web/` | the app, for the packages |
| `~/.local/bin/agents-web` or `/usr/bin/agents-web` | launcher command |
| `agents-web.desktop` in `~/.local/share/applications` or `/usr/share/applications` | menu entry "Agents" |
| `~/.config/agents-web/` | settings, access token, paired phones, extra Claude logins, https certificates |
| `~/.local/share/agents-web/` | chats, uploads, previews, usage cache, `server.log`, `update.log` |

Installer options: `--package`, `--ref <tag>` (a specific version), `--gitlab`, `--no-deps`, `--no-start`,
`--uninstall`, `--purge`. You can also clone the repository and run `./install.sh` from it. Moving from the per
user copy to a package (or back) keeps chats and settings.

### Windows 10 and 11

In PowerShell:

```powershell
irm https://raw.githubusercontent.com/gabrielmf1998/Agents-Web/main/install.ps1 | iex
```

From the GitLab mirror:

```powershell
& ([scriptblock]::Create((irm https://gitlab.com/gabriel17166/agents-web/-/raw/main/install.ps1))) -GitLab
```

It downloads `AgentsWeb-Setup-<version>.exe` from the latest release, checks it against `SHA256SUMS` and installs
it for your user, without administrator rights. You can also run that file from the release page yourself. It
is not signed, so SmartScreen may say "Windows protected your PC": click **More info**, then **Run anyway**.
The installer brings its own Python.

| Path | What |
|---|---|
| `%LOCALAPPDATA%\Programs\Agents Web\` | the app (`AgentsWeb.exe` tray icon, server, page, Python) |
| `%APPDATA%\agents-web\` | settings, access token, paired phones, extra Claude logins, https certificates |
| `%LOCALAPPDATA%\agents-web\` | chats, uploads, previews, usage cache, `server.log`, `update.log` |

It adds **Agents Web** to the Start menu and to Apps (Settings, Apps, Installed apps). Everything works as on
Linux except root approvals, which are a Linux feature, and the QR code for pairing, which needs `qrencode`
(type the PIN instead). The https certificates need `openssl`, which comes with Git for Windows.

## Use

Open **Agents** from the app menu, or run `agents-web`. The tray icon starts the service and the page opens at
<http://127.0.0.1:47830/>. On the PC no login is needed.

```
agents-web            start (or open the page if it is already running)
agents-web stop       stop the service and every running agent
agents-web restart    restart the service
agents-web status     running or stopped
agents-web log        follow the server log
agents-web version    print the installed version
agents-web update     install the latest release now
agents-web uninstall  remove the app
```

The AppImage takes the same commands (`./Agents_Web-*.AppImage stop`, `status`, `update` and so on) and
`server`, which runs only the server in the foreground, for a machine without a desktop. On Windows, open
**Agents Web** from the Start menu. `AgentsWeb.exe --quit` stops it from a terminal.

The tray menu has Open Agents, Pair phone, Restart service, Start on login and Quit. On desktops without a tray
area (stock GNOME), use the commands above or an AppIndicator extension.

### Phone

Click **Mobile** in the sidebar (or "Pair a phone" in the tray) and scan the QR code, or open the address shown
and type the PIN. The phone must be on the same local network. The https address is needed for voice
dictation and notifications. Your browser warns about the certificate once. You can accept the warning, or
install the PC certificate offered on the pairing screen. That certificate is limited to private IP
addresses, so it can never vouch for an internet site.

If your firewall blocks the port, open it for your LAN. On firewalld:
`sudo firewall-cmd --permanent --add-port=47830/tcp && sudo firewall-cmd --reload`.

### Accounts

**Accounts** lists the Claude and Antigravity logins. Your normal `~/.claude` login is the default Claude
account. **Add account** creates an extra Claude login with its own config folder
(`CLAUDE_CONFIG_DIR`), and **Sign in** opens a terminal for it. Antigravity uses the single Google
account of the `agy` CLI.

A Claude chat can change account at any time from the Account chip in its header, history included: the
CLI session moves to the config folder of the new account and the next message resumes it there. When a
reply stops because the account reached its usage limit, a card under it offers **Continue on <account>**
for each other Claude account, with how much of its 5-hour limit is used, and continues the conversation
there with one click. Antigravity conversations stay with their account.

A move takes the whole conversation along (every branch made by Edit and Fork, the subagents, the file
history) and is all or nothing: if a file cannot be moved, nothing changes and the chat stays where it was.
A chat that used the account's default model or effort keeps the same ones on the new account.

With two or more Claude accounts, Settings has a **Claude accounts** section:

- **Switch account at the limit** (off by default). When a reply stops at the usage limit, the chat moves to
  the Claude account with the most room left and sends "Continue where you left off." for you. The reply
  shows a note about the switch. It stops when every account is at its limit.
- **One memory for all accounts** (on by default). Claude Code keeps its memory (`/memory`, the notes Claude
  saves about you and your projects) per login. With this on, every Claude account uses the memory of the main
  account (the first one) for the same project, so what Claude learned on one account is there on the
  others. The memories the other accounts had before are merged in once, and the old index is kept in
  `~/.local/share/agents-web/memory-backups/`.

### Permissions

New chats start in **Full access** (`--permission-mode bypassPermissions` for Claude, `--dangerously-skip-permissions`
for agy). Pick another mode per chat from the permission chip.

| Claude mode | What happens |
|---|---|
| Full access | everything runs without asking |
| Accept edits, ask for the rest | file edits and basic file commands such as `touch` in the working folder run, other commands show a card |
| Ask before each action | reading files and read only commands such as `echo` run, anything else shows a card first |
| Plan first, then approve | Claude explores and writes a plan, you approve it (then edits are accepted, or Claude asks before each change) or send it back |
| Auto | Claude Code decides on its own which actions to allow |
| Don't ask (deny) | anything not already allowed is refused without a card |

A card shows the command, the URL or a diff of the edit. **Allow**, **Deny** (with an optional note that Claude
reads) or **Always allow <tool> here**, which stops the questions for that tool in that chat. The header lists
those tools, and clicking the list makes Claude ask again. Questions from Claude (multiple choice or your own
words) appear the same way, in every mode. You get a notification when the page is in the background.

Antigravity has no prompts in print mode, so its modes (Full access, Accept edits, Auto, Plan) act alone. Only
run agents in folders you trust.

### Edit, retry and fork

Hover a message you sent (on phones the buttons are always visible): **Edit** changes it and gets a new reply,
**Retry** sends it again. Under a reply, **Fork** opens a new chat that continues from that point. The chat
in the page moves to the new branch, and the original conversation stays in Claude Code, where
`claude --resume` still finds it. These work for Claude chats, on messages from version 1.4.0 on.

### Settings

Settings covers the appearance (Dark, Light or System theme, chat width, text size, the chat list and animations),
saved prompts, the alert when a reply is ready (chime, vibration and notification), the dictation language,
**Rich replies** (a short hint that tells agents about the previews, about 250 tokens),
**Root password here** (send `pkexec` prompts to the page instead of the desktop password dialog),
**Claude accounts** (see Accounts) and **Updates** (see below).

### Keyboard

| Key | Action |
|---|---|
| Enter | send (on phones Enter adds a new line, use the send button) |
| Shift+Enter | new line |
| Ctrl+K (Cmd+K) | search chats and messages |
| Ctrl+B | hide or show the chat list |
| Ctrl+Shift+O | new chat |
| Alt+Up, Alt+Down | previous or next chat in the list |
| Ctrl+, | Settings |
| / | focus the message box (outside a text field); at the start of a message, open the saved prompts |
| ? or Ctrl+/ | list of shortcuts |
| Escape | clear the search, close a menu or the image viewer, leave selection mode |

What you type stays with its chat: switch to another chat and back, and the unsent text is still there.

### Port

The default port is 47830. To change it, set `AGENTS_WEB_PORT` in your session environment, for example in
`~/.config/environment.d/agents-web.conf`:

```
AGENTS_WEB_PORT=48000
```

`AGENTS_WEB_CONFIG_DIR` and `AGENTS_WEB_DATA_DIR` move the settings and the chats to other folders. Together
with another port they run a second instance that shares nothing with the main one, for example to try a
version from a checkout:

```bash
AGENTS_WEB_PORT=47950 AGENTS_WEB_CONFIG_DIR=/tmp/aw/cfg AGENTS_WEB_DATA_DIR=/tmp/aw/data python3 src/server.py
```

## Update and uninstall

Agents Web looks for a new release two minutes after it starts and then twice a day. Settings, **Updates**,
shows the installed version, how it was installed and the choice for **Automatic updates**:

| Choice | What happens |
|---|---|
| Install (default) | the new release is downloaded, checked against its SHA256 sum and installed when no reply has run for 10 minutes. The service restarts and an open page reloads by itself |
| Notify | a green dot on Settings and a notice with the release notes and an **Update now** button |
| Off | nothing is checked until you click **Check for updates** |

| Installed with | How it updates |
|---|---|
| `install.sh` (per user) | by itself, no password |
| AppImage | by itself, the file is replaced in place (it must be writable by you) |
| Windows installer | by itself, the new installer runs silently and starts Agents Web again |
| .deb, .rpm, pacman | a notice. **Install now** asks for your password in a polkit window on the PC and installs the package with apt, dnf, zypper or pacman |

A download that does not match the release's SHA256 sum is never installed. From a terminal:

```bash
agents-web update                    # the same, now (sudo for packages)
agents-web uninstall                 # per user copy: keeps chats and settings
agents-web uninstall --purge         # deletes them too
sudo apt remove agents-web           # packages: sudo dnf remove, zypper remove or pacman -R
```

For the AppImage, delete the file (and `~/.config/agents-web` and `~/.local/share/agents-web` to drop the chats
and settings). On Windows, uninstall **Agents Web** from Settings, Apps. It asks whether to delete chats and
settings too.

Uninstalling leaves your Claude Code and agy sessions alone, and keeps the distribution packages it added.

## Documentation

- [How it works](docs/ARCHITECTURE.md): processes, storage, the HTTP API and how the CLIs are driven
- [Security](docs/SECURITY.md): network exposure, pairing, https, root approvals and what is not protected
- [Changelog](CHANGELOG.md)

## Troubleshooting

- **Page does not open.** Run `agents-web status` and `agents-web log`. A busy port shows up in the log.
- **The service keeps restarting.** The log of the run before the last restart is kept in
  `~/.local/share/agents-web/server.log.1`.
- **Account shows "signed out".** Click Sign in in Accounts, or run `claude` (or `agy`) in a terminal once.
- **Phone cannot connect.** Both devices must be on the same network. Check the firewall (see Phone). VPN
  and container interfaces are ignored on purpose.
- **No tray icon on GNOME.** Install an AppIndicator extension, or use the `agents-web` commands.
- **An update failed.** Settings, Updates, shows the error, and `~/.local/share/agents-web/update.log`
  (`%LOCALAPPDATA%\agents-web\update.log` on Windows) has the whole output. Automatic updates try again after
  the next restart. `agents-web update` runs the same update in a terminal.
- **The AppImage does not start.** It needs the `fusermount` program of FUSE (the `fuse3` package on most
  distributions). Without it, set `APPIMAGE_EXTRACT_AND_RUN=1` in your environment, and the AppImage unpacks
  itself to a temporary folder each time it starts.

## Development

Run the server straight from a checkout, then open <http://127.0.0.1:47830/>:

```bash
python3 src/server.py
```

The tests start the real server in a throwaway `HOME`, on a free port, with a fake `claude` binary, so they
never touch your chats, logins or quota. They need only Python 3.9+, `setpriv` and (for the https test)
`openssl`:

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_atualizacao.py` runs the updater of an installed copy against a fake release (`file://` links): the
SHA256 check, every kind of install, a password that is refused, automatic and notify only.

The release files are built in the official distribution containers, with Docker or Podman. `packaging/build.sh`
builds one of them inside a container, and `packaging/build-all.sh` builds them all into `dist/`:

```bash
packaging/build-all.sh                          # dist/: .deb, .rpm, .pkg.tar.zst, .AppImage, .exe, .tar.gz, SHA256SUMS
OUT=dist/old VERSION=1.9.9 packaging/build-all.sh   # an older build to update from
```

`packaging/test-install.sh` installs the old build in a fresh container, checks the tray, runs the unit tests
against the installed copy, updates it from the page, and removes it. `packaging/test-windows.ps1` does the same
on Windows with a fake `claude.exe`, and also runs `install.ps1` in Windows PowerShell 5.1.

GitHub Actions runs all of it on every push: the tests on Python 3.10, 3.13 and 3.14, `shellcheck`, the build,
20 install and update tests in containers (9 distributions) and the Windows test on Windows Server 2022 and 2025.
A `v*` tag that passes is published as a GitHub release with the files and their `SHA256SUMS`.

`tests/e2e_contas.py` checks account moves and the shared memory with the real `claude` CLI and two of your
own Claude accounts. It starts its own server with temporary data and cleans up the sessions it made, but it
spends about 15 short Claude Haiku replies, so it is not part of the unit tests:

```bash
python3 tests/e2e_contas.py            # the first two Claude accounts
python3 tests/e2e_contas.py claude1 claude2
```

## License

[MIT](LICENSE)
