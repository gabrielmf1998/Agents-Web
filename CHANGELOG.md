# Changelog

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
