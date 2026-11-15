# Changelog

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
