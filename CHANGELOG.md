# Changelog

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
