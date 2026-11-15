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
- **Several Claude accounts.** Add extra Claude logins and pick the account per chat. Each account has its own model, effort and permission choices. When one account reaches its usage limit, the chat offers to continue the same conversation on another one.
- **Real usage limits.** Shows session and weekly limits as reported by the CLIs themselves, plus tokens per model.
- **Terminal conversations.** Your existing `claude` and `agy` sessions show up in the sidebar. You can open, continue, rename or delete them, and a Claude chat can move to another working folder.
- **Rich replies.** Markdown with tables, live previews of `html` and `svg` blocks in a sandboxed frame that follows the page theme, and inline images, videos, audio and PDFs for absolute file paths the agent mentions.
- **Phone friendly.** Pair a phone with a 6 digit PIN or a QR code. You get https on the same port with a private local CA, voice dictation and notifications when a reply is ready.
- **Approvals, questions and plans (Claude).** Permission prompts, questions from Claude and plans to approve show up as cards in the chat, with a diff for every edit.
- **Edit, retry and fork (Claude).** Change a message you sent and get a new reply, or fork any reply into a new chat. The original conversation stays in Claude Code.
- **Root approvals in the browser.** When an agent runs `pkexec`, a card shows the command and you approve it with your password (https or the PC only) or deny it.
- **Workflow helpers.** Message queue while the agent works, full text search, pinned chats, a draft kept per chat, export as Markdown, working folder picker with "New folder", file uploads, resizable sidebar, a jump to latest button and a stop button.
- **Updates from the page.** Settings checks for a new release when you click and installs it. The page can also be installed as an app.
- **No dependencies.** The server uses only the Python standard library, the page needs no build step or CDN, and the tray uses under 4 MB of RAM.

## Requirements

- Linux with a graphical session (KDE Plasma, GNOME, etc). Tested on **Arch Linux** and **Fedora 45**.
- At least one of the CLIs installed and signed in:
  - Claude Code: `curl -fsSL https://claude.ai/install.sh | bash`, then run `claude` once to sign in.
  - Antigravity CLI: `curl -fsSL https://antigravity.google/cli/install.sh | bash`, then run `agy` once to sign in.
- The installer adds what is missing from: Python 3, polkit, OpenSSL, qrencode, iproute2, util-linux, procps, gcc and the libsystemd headers.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/gabrielmf1998/Agents-Web/main/install.sh | bash
```

From the GitLab mirror:

```bash
curl -fsSL https://gitlab.com/gabriel17166/agents-web/-/raw/main/install.sh | bash -s -- --gitlab
```

The installer works per user. It only asks for your password (sudo) to install missing distribution
packages, and it shows the package list first. Everything else goes into your home folder:

| Path | What |
|---|---|
| `~/.local/opt/agents-web/` | the app (server, page, tray icon, pkexec helper) |
| `~/.local/bin/agents-web` | launcher command |
| `~/.local/share/applications/agents-web.desktop` | menu entry "Agents" |
| `~/.config/agents-web/` | settings, access token, paired phones, extra Claude logins, https certificates |
| `~/.local/share/agents-web/` | chats, uploads, previews, usage cache, `server.log` |

Installer options: `--ref <tag>` (a specific version), `--gitlab`, `--no-deps`, `--no-start`.
You can also clone the repository and run `./install.sh` from it.

## Use

Open **Agents** from the app menu, or run `agents-web`. The tray icon starts the service and the page opens at
<http://127.0.0.1:47830/>. On the PC no login is needed.

```
agents-web            start (or open the page if it is already running)
agents-web stop       stop the service and every running agent
agents-web restart    restart the service
agents-web status     running or stopped
agents-web log        follow the server log
agents-web update     install the latest release
agents-web uninstall  remove the app
```

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

Settings covers animations, the alert when a reply is ready (chime, vibration and notification), the dictation language,
**Rich replies** (a short hint that tells agents about the previews, about 250 tokens),
**Root password here** (send `pkexec` prompts to the page instead of the desktop password dialog) and
**Updates** (see below).

### Keyboard

| Key | Action |
|---|---|
| Enter | send (on phones Enter adds a new line, use the send button) |
| Shift+Enter | new line |
| Ctrl+K (Cmd+K) | search chats and messages |
| Escape | clear the search, close a menu, leave selection mode |

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

In the page, open Settings and click **Check for updates**. If there is a new release you see its notes and an
**Update now** button. The service restarts and the page reloads by itself. Nothing is checked unless you
click. From a terminal:

```bash
agents-web update                         # latest release
bash ~/.local/opt/agents-web/install.sh --uninstall           # keeps chats and settings
bash ~/.local/opt/agents-web/install.sh --uninstall --purge   # deletes them too
```

Uninstalling leaves your Claude Code and agy sessions alone, and keeps the distribution packages.

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

## Development

Run the server straight from a checkout, then open <http://127.0.0.1:47830/>:

```bash
python3 src/server.py
```

The tests start the real server in a throwaway `HOME`, on a free port, with a fake `claude` binary, so they
never touch your chats, logins or quota. They need only Python 3.10+, `setpriv` and (for the https test)
`openssl`:

```bash
python3 -m unittest discover -s tests -v
```

GitHub Actions runs them on every push, together with a build of the tray icon and `shellcheck`.

## License

[MIT](LICENSE)
