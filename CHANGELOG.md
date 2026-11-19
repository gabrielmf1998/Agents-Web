# Changelog

## 2.2.0 (2026-11-19)

New

- **Call another AI.** A new button in the chat header brings a second AI into the conversation you are in:
  Antigravity into a Claude chat, Claude into an Antigravity chat, or another Claude account. Pick who, the
  model, the effort (for Claude) and how it takes part. It reads the chat so far and answers right there, under
  its own name.
- **Join the chat, the two AIs as a team.** With **Join the chat** (the default), every message you send goes
  to both: the chat's own AI answers first and the other one answers right after, with that reply in front of
  it, building on it, fixing what is wrong and adding what is missing instead of repeating it. The page takes a
  joined theme with both marks side by side and a mixed accent color, the box says "Message Claude and
  Antigravity", and the header shows both AIs, with the guest's model and effort and a **Leave** button. If the
  other AI cannot answer (its account was removed, for example) it leaves the chat with a note and your queued
  messages go on.
- **Every reply says who wrote it.** Replies carry the model and effort that wrote them, and in chats with two
  AIs each one has its own color line (orange for Claude, blue to purple for Antigravity).
- **One reply.** The other AI answers once, about the last reply or about what you ask it, and your message to
  it shows "You to Antigravity". The chat's own AI gets that answer with your next message.
- **Debates.** Choose Debate and 2 to 10 rounds, and the two AIs take turns by themselves, answering each
  other's points, while you watch. A bar above the message box shows "Debate with Antigravity, reply 3 of 6"
  and has **End after this reply**. Stop ends it at once, and a message you send in the middle waits for the
  current reply, goes to the chat's own AI, and then the debate goes on. You get one notification at the end,
  not one per turn.
- Each AI keeps its own session, so the other one only gets what it has not seen yet. It never gets more
  freedom than the chat: a chat in Plan mode calls the other AI in Plan mode too. Edit, Retry and Fork keep
  working in chats with another AI, and deleting a chat with its sessions also deletes the other AI's sessions.

## 2.0.0 (2026-11-16)

New

- **Packages for every big Linux family.** Each release now has a `.deb` (Debian, Ubuntu and derivatives), an
  `.rpm` (Fedora, RHEL, AlmaLinux, Rocky, openSUSE), a pacman package (Arch and derivatives) and an AppImage with
  its own Python for anything else, all with a prebuilt tray icon. `install.sh --package` picks and installs
  the right package for you.
- **Windows 10 and 11.** `AgentsWeb-Setup-<version>.exe` installs for your user without administrator rights,
  with its own Python, a tray icon (Open, Pair phone, Restart, Start on login, Quit), a Start menu entry and an
  entry in Apps. In PowerShell, `irm https://raw.githubusercontent.com/gabrielmf1998/Agents-Web/main/install.ps1 | iex`
  downloads it, checks its SHA256 and installs it.
- **Automatic updates.** Agents Web looks for a new release two minutes after it starts and then twice a day.
  The per user install, the AppImage and Windows install it by themselves when no reply has run for 10 minutes;
  the service restarts and an open page reloads. Packages show a notice and install with one click and your
  password. Settings, Updates, has Install, Notify or Off, says how this copy was installed, and shows the error
  of a failed update. A green dot on Settings marks a new release.
- **Every download is checked.** Updates and both installers refuse a file whose SHA256 does not match the
  release's `SHA256SUMS` (or GitHub's own digest), and refuse to install when there is no sum at all.
- **One line installer for more distributions.** `install.sh` was for Arch and Fedora. It now also knows Debian,
  Ubuntu, RHEL, AlmaLinux, Rocky and openSUSE, and downloads the release file with the prebuilt tray icon, so it
  no longer needs gcc or the libsystemd headers.
- **Releases are built and tested by CI.** The files are built in the official distribution containers. Each one
  is installed, used, updated from an older build and removed on 9 Linux distributions (20 combinations) and on
  Windows Server 2022 and 2025, and a passing `v*` tag is published with its `SHA256SUMS`.
- `agents-web update` now runs the same checked update as the page, with sudo for packages.

## 1.8.0 (2026-11-15)

New

- **Switch account at the limit.** Settings, Claude accounts. When a reply stops because the account reached its
  usage limit, the chat moves by itself to the Claude account with the most room left and goes on with
  "Continue where you left off.". The reply shows a note ("EmpresaGabriel reached its limit, so the chat moved
  to Kxinha") and the message sent for you is tagged "automatic". It stops when every account is at its limit.
  Off by default; the limit card has a "Switch by itself next time" button to turn it on.
- **One memory for all accounts.** Claude Code keeps its memory per login, so a chat on a second account did not
  see what Claude learned on the first one. Now every Claude account reads and writes the memory of the main
  account (the first one, your `~/.claude`) for the same project. The memories the other accounts already had
  are merged in once, with a backup of the old index. On by default; Settings, Claude accounts, turns it off.
- **Model and effort stay the same after a move.** A chat that used the account's default model or effort gets
  them pinned when it changes account, so it does not suddenly run on the other account's defaults. The toast
  says what was kept.
- **A better limit card.** It reads fresh usage for each account, puts the accounts with the most room first,
  marks the ones that are full and says how big the conversation is (the next account reads all of it again).
- `tests/e2e_contas.py`: an end to end check of account moves with the real `claude` CLI and two of your own
  accounts (round trip, tool calls, an edited branch, a fork, `claude --resume` in a terminal and the shared
  memory). Run it by hand; it costs about 15 short Haiku replies.

Fixes

- Moving a chat to another account is now all or nothing. Before, an error halfway (a full disk, a permission
  problem) could leave part of the session on each account. Now every file is moved or nothing is, and the
  chat says the move failed and stays where it was.
- "Continue on <account>" replaced the text you were typing with "Continue where you left off.". Now it sends
  that line and leaves your draft and attachments alone.
- The limit card used the limits saved at the end of the last reply, which could be hours old. Now it uses the
  same fresh numbers as the Usage panel.
- The limit card had no inner padding, because a rule of the Usage panel matched it too.
- A model pinned after a move showed its raw id in the Model chip.
- `POST /api/chats/<id>/conta` answers 400 with a clear message instead of 500 when the move fails, and returns
  `fixado` with the model and effort it pinned.

## 1.7.0 (2026-11-15)

New

- **Hide the chat list.** The button next to the logo (or Ctrl+B) folds the sidebar into a thin icon rail, so
  the chat gets the whole window. The rail has New chat, Search, Chats (opens the list over the chat), usage,
  accounts, phone and Settings. A badge counts chats that wait for you, and a small ring shows the session
  limit of the account in use. The choice is remembered.
- **Light theme.** Settings, Appearance, Theme: Dark, Light or System (follows the desktop). Both the Claude and
  the Antigravity look have a light version, and html previews in replies follow it.
- **Chat width and text size.** Normal, Wide or Full width for the conversation, and four text sizes.
- **Syntax highlighting** in code blocks (JavaScript/TypeScript, Python, shell, C/C++/Java, Go, Rust, SQL, JSON,
  CSS, HTML/XML, YAML/TOML/INI, diff and Lua), without any library. Code blocks also get a Wrap button for long
  lines and show their line count.
- **Grouped tool calls.** Three or more tool calls in a row fold into one line with the tool names, the lines
  added and removed and any failures. During a reply the group stays open until the agent starts writing text.
- **Saved prompts.** The new button in the message box (or "/" at the start of a message) lists your prompts;
  arrows and Enter pick one. Save the current message as a prompt in one click, and rename, reorder or delete
  them in "Manage prompts". The first three show up as cards in an empty chat. Stored on the PC, so the phone
  sees them too.
- **Image viewer.** Click an image in a reply or an attachment to see it large; Escape closes it.
- **Keyboard shortcuts.** Ctrl+B (chat list), Ctrl+Shift+O (new chat), Alt+Up and Alt+Down (previous and next
  chat), Ctrl+, (Settings), "/" to focus the message box and "?" or Ctrl+/ for the list of shortcuts.
- **Recent chats** on the start screen, to pick up where you left off.
- **Times in the chat list**: each chat shows when it last changed (5m, 3h, Mon, 12 Nov).
- **Copy on your own messages**, and long messages you sent fold after 14 lines with Show more.
- New endpoint `POST /api/prompts`; `GET /api/estado` returns the prompts.

Changes

- Copy buttons say "Copied" in place instead of only showing a toast, and copying also works over plain http
  on the phone.
- On phones the stats bar above the message box is one row that scrolls sideways, instead of taking two or
  three lines.
- The Settings dialog has an Appearance section and a link to Manage prompts.

Fixes

- The glow behind the Antigravity theme did not pulse and drew a faint red frame, because the dictation button
  had an animation with the same name.

## 1.6.0 (2026-11-15)

New

- **Smart titles.** A new chat gets a short title written by Claude Haiku, like "Ryzen 9950X3D compatibility"
  instead of the first 60 characters of your message. It appears about 2 seconds after you send, while the
  reply is still coming in. Very short first messages ("hi", only a file) are named after the reply. The
  Claude Code session gets the same name, so `claude --resume` shows it too. Costs about 800 tokens per chat
  and can be turned off in Settings.
- **Name old chats.** Settings, Old chat titles, gives a title to every chat still named after its first
  message (up to 100 at a time, with progress). Titles you typed yourself are never touched.
- **Name it with AI** in the chat menu, for any single chat.
- Without smart titles the first message still becomes a tidier title: links turn into their last path part,
  the first letter is a capital, and the cut falls between words with an ellipsis.

Changes

- Empty "New chat" entries no longer pile up. A chat you open and leave without a message is removed, "New
  chat" reuses the empty chat you are in, and leftovers from other devices are removed after a week. Chats
  with changed settings, pinned chats and chats with a draft are always kept.
- The sidebar no longer shows `~` for chats in your home folder.

## 1.5.0 (2026-11-15)

New

- **Continue on another account.** When a Claude reply stops because the account reached its usage limit,
  a card offers "Continue on <account>" for every other Claude account, with how much of its 5-hour limit
  is used. One click moves the conversation and Claude picks up where it stopped.
- The Account chip of a Claude chat now works after the first message too. The CLI session (transcript,
  subagents, file history) moves to the config folder of the new account, where `claude --resume` finds
  it. A session another chat of the old account still uses is copied instead of moved.
- New endpoint `POST /api/chats/<id>/conta`.

## 1.4.1 (2026-11-15)

Fixes

- "Update now" could stop halfway when the tray runs inside a systemd unit (for example one started with
  `systemd-run`). Stopping the tray ended the unit, and systemd killed the installer with it. The update now
  runs in a unit of its own (`agents-web-update-<time>`), and the restarted tray lives on in it. Without
  systemd it runs in its own session as before.

## 1.4.0 (2026-11-15)

New

- **Approvals in the page (Claude).** Claude chats now talk to the CLI over stdio, so a permission prompt
  becomes a card in the chat instead of a silent denial. Allow, deny with a note for Claude, or "Always allow
  <tool> here" for the rest of that chat. The header shows the tools a chat always allows, and one click makes
  Claude ask again.
- **Questions and plans.** When Claude asks a question (AskUserQuestion), the card shows its options and a
  field for your own answer. In "Plan first, then approve" mode the plan is shown in full, and you approve it
  with edits accepted automatically or with Claude asking before each change, or send it back with a note.
- **Diffs.** Edits and new files show a colored diff with line numbers, both on the approval card and in the
  finished tool call, with a +/− count in the tool summary.
- **Edit, retry and fork (Claude).** Edit a message you sent or send it again, and the reply below it is
  replaced. Fork any reply into a new chat that continues from there. The original conversation stays in
  Claude Code untouched, because each of these branches the CLI session (`--resume-session-at`,
  `--fork-session`).
- **Export as Markdown** from the chat menu in the sidebar.
- **Updates from the page.** Settings, Updates, checks GitHub (or GitLab, for installs from the mirror) when you
  click, shows the release notes and installs the new version. The page reloads by itself.
- **Installable app.** A web app manifest and icons, so "Add to home screen" and "Install app" give a proper
  icon and a window of its own.
- Stop is now a clean interrupt of the CLI, with a kill only if it does not answer within 4 seconds.
- `AGENTS_WEB_CONFIG_DIR` and `AGENTS_WEB_DATA_DIR` run a second, separate instance (for tests, or to try a
  version).

Changes

- The permission modes have clearer names. "Manual" is now "Ask before each action" and works, because
  prompts reach the page.
- The root relay socket of an instance on another port is `agents-web-pk-<port>.sock`, so a second instance
  never touches the socket of the main one.
- `agents-web update` uses the GitLab installer when the app was installed from GitLab.

Notes

- Edit, retry and fork work on replies made with 1.4.0 or later. Older messages do not have the reference
  the CLI needs to branch.
- Antigravity chats keep their permission modes as before. The agy CLI has no stdio prompt in print mode.

## 1.3.1 (2026-11-15)

Fixes

- A chat whose account was removed in Accounts no longer runs silently with the default Claude login. Sending
  now says so and how to fix it.
- Revoking a paired device removes exactly that device. Before, the id was matched as a prefix.
- An `X-Token` header or PIN with non-ASCII characters got a 500 error instead of being refused.
- Two runs saving the same file at once (for example the usage limits) could mix their writes. Every write
  now has its own temporary file.
- Connections from a phone that went away mid stream are closed after 5 idle minutes instead of keeping a
  thread forever.
- Folders without read permission give a clear error in the folder picker instead of a 500.
- Paths were shown with `~` when they only began with the same letters as the home folder
  (`/home/ann2` looked like `~2`).
- `--uninstall --purge` deleted the wrong folders when `XDG_CONFIG_HOME` or `XDG_DATA_HOME` were set, and
  left the "Start on login" entry behind.
- Errors from the Accounts, Mobile, duplicate and folder buttons now show up instead of failing silently.

Improvements

- Unsent text is kept per chat, also after a reload.
- Ctrl+K (Cmd+K) jumps to the search box, Escape clears it.
- A button jumps back to the latest message when you scrolled up.
- "Last seen" of paired devices survives a restart.
- The tray keeps the log of the previous run as `server.log.1`.
- End to end tests (`tests/`) with a fake Claude CLI, and GitHub Actions for the tests, the tray build and
  `shellcheck`.

## 1.3.0 (2026-11-14)

First public release.

- Web interface for the Claude Code CLI and the Antigravity CLI, with streaming replies, thinking, tool
  calls and their output, a message queue, stop, search and per chat model, effort and permission mode.
- Several Claude accounts, each with its own `CLAUDE_CONFIG_DIR`, and a terminal sign-in from the page.
- Session and weekly usage limits and tokens per model.
- Terminal sessions of both CLIs in the sidebar (open, continue, rename, delete).
- Pinned chats and a resizable, compact sidebar.
- Rich replies with themed live previews of html and svg blocks and inline files.
- Phone pairing with a PIN or QR code, https on the same port with a name constrained local CA,
  dictation and notifications.
- Root approvals from the browser for `pkexec`.
- Tray icon (StatusNotifierItem), `agents-web` launcher, and a curl installer for Arch Linux and Fedora.
