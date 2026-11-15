# How Agents Web works

Agents Web is three small programs. None of them talks to a model directly. Every reply comes from the
official `claude` or `agy` binary, started in print mode for each message.

```
 browser (PC or phone)
        |  http / https on port 47830, same port
        v
 server.py  (Python standard library, one process)
        |  starts one CLI process per reply, reads its JSON stream
        v      (and answers Claude's permission requests on its stdin)
 claude -p --input-format stream-json ...      agy --output-format stream-json -p ...
        |                                             |
   ~/.claude (or an extra CLAUDE_CONFIG_DIR)     ~/.gemini/antigravity-cli

 agents-web-tray  (C, sd-bus)  starts server.py as a child, shows the tray icon, restarts it if it crashes
```

## Components

| File | Role |
|---|---|
| `src/server.py` | HTTP and HTTPS server, chat store, CLI runner, usage limits, phone pairing, root relay |
| `src/index.html` | the whole web app (HTML, CSS and JavaScript in one file, no build step, no CDN) |
| `src/bin/pkexec` | a `pkexec` wrapper placed first in the agents' `PATH` (see Root approvals) |
| `tray/agents-web-tray.c` | StatusNotifierItem tray icon with a dbusmenu, supervises the server |
| `src/agents-web` | the launcher command |
| `install.sh` | installer, updater and uninstaller |

### Tray

The tray starts `python3 -u server.py` as its child and checks `GET /saude` every few seconds. When the
server exits it is restarted, at most 3 times per minute. Each start moves the previous `server.log` to
`server.log.1`, so the output of a crash survives the automatic restart. Quitting the tray (or logging out, which closes
the D-Bus session) stops the server, and that stops every running agent. "Start on login" writes or
removes `~/.config/autostart/agents-web.desktop`. With `--open` it opens the page in the default browser
as soon as the server answers.

### Server

`server.py` is a threaded `http.server`. A single listening socket serves both plain http and https:
the first byte of each connection tells a TLS handshake from plain http. It binds `0.0.0.0:47830`, and it
answers only loopback and the local networks (see [SECURITY.md](SECURITY.md)).

Every message starts a run.

1. The prompt (plus the paths of any uploaded files) goes to a new CLI process.
   - Claude: `claude -p --input-format stream-json --output-format stream-json --verbose
     --include-partial-messages --permission-prompt-tool stdio` with `--model`, `--permission-mode`,
     `--effort` and `--resume <session>` when set. The prompt goes in as a stream-json user message, and stdin
     stays open for the answers to permission requests (see Approvals). It is closed when the result arrives,
     and the CLI exits.
   - Antigravity: `agy --output-format stream-json` with `--model`, `--mode` or
     `--dangerously-skip-permissions` and `--conversation <id>` when set, and `-p <prompt>`.
2. The process runs under `setpriv --pdeathsig KILL`, so an agent never outlives the server.
3. Each JSON line is turned into events (text, thinking, tool calls with their output, usage, errors)
   and kept in memory for that run.
4. Browsers follow a run with Server Sent Events (`/api/chats/<id>/eventos`). A reconnecting client
   sends the index of the last event it saw, so a phone that sleeps resumes without losing text.
5. When the process ends, the message is saved and the next queued message, if any, starts.

The CLI session id from the first reply is stored in the chat, so the next message resumes the same
conversation and keeps the agent's context. The same session can be opened later in a terminal with
`claude --resume <id>`.

### Approvals, questions and plans

With `--permission-prompt-tool stdio`, whenever Claude Code would show a prompt in a terminal it writes a
`control_request` (`subtype: can_use_tool`, with the tool name and input) and waits.

1. If the chat always allows that tool (`permitidos` in the chat file), the server answers `allow` at once.
2. Otherwise the request becomes a card, an event of type `pedido` with `tipo` `ferramenta` (a tool),
   `pergunta` (AskUserQuestion) or `plano` (ExitPlanMode). For Edit and Write the server computes the diff
   against the file on disk, so the card shows real line numbers.
3. `POST /api/pedido` answers it. The server writes a `control_response` with `behavior: allow` (for a
   question, the input plus `answers`) or `behavior: deny` with a message that includes your note.
   Approving a plan with edits accepted adds a `setMode acceptEdits` permission update, and the chat leaves
   plan mode, as it does in the terminal.
4. When the run ends, requests still open are marked cancelled.

Stop sends a `control_request` of subtype `interrupt`. The CLI ends the turn and reports it, and a kill
follows only if it does not exit within 4 seconds. Antigravity runs have no such channel, so they are
stopped with a signal as before.

### Edit, retry and fork

Each saved Claude reply keeps the `uuid` of its last assistant message that has text and no pending tool
call. Editing or retrying message `n` starts the next run with `--resume <session> --resume-session-at <uuid
of reply n-1> --fork-session`. The CLI makes a new session that contains the conversation up to that reply,
and the original session file stays as it was. The page keeps only the messages before `n`, and the old
session id is remembered (`antigas`) so it is not listed again as a terminal session. Retrying the first
message simply starts a new session. Fork does the same in a new chat file, and the original chat is not
touched.

### Terminal sessions

The sidebar also lists sessions made in a terminal. They are read directly from the CLIs' own files.

- Claude: `<config dir>/projects/*/*.jsonl` for each Claude account.
- Antigravity: `~/.gemini/antigravity-cli/history.jsonl` and `conversations/*.db` (read only).

Opening one creates a web chat that resumes that session. Renaming appends a title record to the Claude
transcript, the same one `/rename` writes, and deleting removes the CLI's own session files. A Claude
chat can switch its working folder, and the next reply resumes the session from the new folder.

### Usage limits

- Claude: the server reads the account's OAuth access token from `<config dir>/.credentials.json` and calls
  `https://api.anthropic.com/api/oauth/usage`, the same endpoint the `/usage` command in Claude Code uses.
  It also records the `rate_limit_event` lines that come in the JSON stream.
- Antigravity: `agy -p /usage --output-format json`, which prints the quota without opening a turn.
- Token totals per model come from the CLIs' local transcripts.

### Rich replies

When "Rich replies" is on, a short hint is added to the system prompt (Claude, with
`--append-system-prompt`) or to the first message of a conversation (Antigravity). It tells the agent that
`html` and `svg` code blocks render live, which CSS variables match the page theme, and that absolute file
paths in backticks are shown inline.

The page stores each block with `/api/preview` and shows it in an iframe whose sandbox allows scripts but
not `allow-same-origin`, so a preview runs with an opaque origin and cannot read the page, its token or
its cookies. Old previews are deleted after 60 days.

Absolute paths of images, video, audio, PDF and html files become signed links, `/f/<signature>/<path>`.
The signature is an HMAC, with a key kept in `config.json`, of the file's folder, so an html page can load
the files next to it. Hidden folders are never covered, and a file directly in the home folder or in a
hidden folder gets a link for that one file only.

## Storage

| Path | Content |
|---|---|
| `~/.config/agents-web/config.json` | accounts, defaults, PC token, paired devices, pinned chats (mode 600) |
| `~/.config/agents-web/contas/<id>/` | `CLAUDE_CONFIG_DIR` of each extra Claude account |
| `~/.config/agents-web/tls/` | the local CA and the server certificate (folder 700, keys 600) |
| `~/.local/share/agents-web/chats/<id>.json` | one file per web chat (messages, tool calls, settings) |
| `~/.local/share/agents-web/uploads/` | files sent from the browser |
| `~/.local/share/agents-web/previews/` | the source of rendered html previews |
| `~/.local/share/agents-web/limites.json`, `uso.json` | usage caches |
| `~/.local/share/agents-web/server.log` | stdout and stderr of the server, written by the tray (`server.log.1`: the run before) |
| `~/.local/share/agents-web/update.log` | output of updates started from the page |

Writes go to a temporary file (one per writer) followed by a rename, so a crash never leaves half a JSON
file and two threads saving the same file never mix their bytes.

These paths do not follow `XDG_CONFIG_HOME` or `XDG_DATA_HOME`. The installer only uses those for the menu
entry and the icon. `AGENTS_WEB_CONFIG_DIR` and `AGENTS_WEB_DATA_DIR` replace the two folders, for a second
instance.

## HTTP API

All endpoints live under `/api/` and need the `X-Token` header (the PC token, or the device token of a
paired phone). Request bodies and responses are JSON. Names are in Portuguese because the project started
that way. They are stable identifiers, not text shown to users.

| Endpoint | Method | Purpose |
|---|---|---|
| `estado` | GET | accounts, chats, limits, defaults, network info, pending root requests, pinned chats |
| `chats` | POST | create a chat |
| `chats/<id>` | GET | a chat with all its messages |
| `chats/<id>/enviar` | POST | send a message (queued when a reply is running) |
| `chats/<id>/eventos?desde=<n>` | GET | Server Sent Events of the running reply, from event `n` |
| `chats/<id>/parar` | POST | stop the running reply |
| `chats/<id>/config` | POST | rename, change model, effort, permission mode, account or folder |
| `chats/<id>/fila` | POST | edit, reorder or clear queued messages |
| `chats/<id>/duplicar` | POST | copy a chat |
| `chats/<id>/reenviar` | POST | edit or retry message `indice` (`texto`, or `null` to resend it as it was) |
| `chats/<id>/bifurcar` | POST | a new chat that continues from reply `indice` |
| `chats/<id>/conta` | POST | continue a Claude chat on another Claude account (`conta`), moving its CLI session |
| `chats/<id>/permitidos` | POST | forget the always allowed tools (`tirar`: one name, or all) |
| `chats/<id>/apagar`, `chats/apagar` | POST | delete one or several chats, optionally with their files and CLI sessions |
| `fixar` | POST | pin or unpin a chat |
| `busca` | GET | full text search over chats and terminal sessions |
| `upload` | POST | upload a file (raw body up to 8 GB, name in the query) |
| `arquivo?id=<id>` | GET | download an uploaded file |
| `preview` | POST | store an html preview and get its URL |
| `pastas`, `pastas/criar` | GET, POST | browse folders and create one |
| `modelos` | GET | models and effort levels offered by each CLI |
| `uso` | GET | limits and token totals per account |
| `contas/status`, `contas/login`, `contas/nova`, `contas/editar` | GET, POST | accounts |
| `cli/sessoes`, `cli/lista`, `cli/renomear`, `cli/apagar`, `cli/importar` | GET, POST | terminal sessions |
| `preferencias` | POST | default settings |
| `pedido` | POST | answer a card: a root request (password or deny), a tool (`aprovar`, `sempre`, `negar` with `motivo`), a question (`responder` with `respostas`) or a plan (`aprovar` with `modo`, or `negar`) |
| `atualizacao?forcar=1`, `atualizar` | GET, POST | check the latest release, install it |
| `celular/pin`, `celular/passe`, `celular/dispositivos`, `celular/revogar` | GET, POST | phone pairing |
| `assinar` | POST | turn absolute file paths from a reply into signed `/f/` links |

Other routes are `/` (the page), `/sw.js` (service worker for notifications), `/manifest.webmanifest` and the
icons (`/icon.svg`, `/icon-192.png`, `/icon-512.png`), `/ca.crt` (the local CA),
`/f/<signature>/<path>` (inline files) and `/saude` (health check, loopback only). The event stream may
take the token as `?t=`, because `EventSource` cannot send headers.

## Tests

`tests/test_server.py` starts `server.py` in a temporary `HOME` with a fake `claude` that speaks stream-json.
The fake reads its stdin like the real CLI and can ask for a permission, a question or a plan. The tests cover the
access rules (token, `Host` check, https on the same port, the CA name constraints), a full reply with
`--resume`, the queue, stop by interrupt, approvals (allow, deny with a note, always allow), questions, plans,
diffs, edit, retry and fork, search, delete, uploads, signed `/f/` links (folder scope, hidden folders, `..`,
ranges), the preview sandbox, the manifest and icons.

## Updates

Settings, Updates, calls `api.github.com/repos/gabrielmf1998/Agents-Web/releases/latest` (or the GitLab
`releases/permalink/latest` endpoint when `~/.local/opt/agents-web/source` says `gitlab`) only when you
click, and compares the tag with the running version. **Update now** is offered only for a copy installed
by `install.sh` and only when no reply is running. It starts the same `curl … install.sh | bash -s --
--no-deps` as `agents-web update` in a transient systemd user unit of its own (`agents-web-update-<time>`,
`ExitType=cgroup`). The installer stops the tray, which ends the server's whole process group and, when
the tray itself runs in a unit, that unit too. A separate unit survives both, and the tray the installer
starts again stays alive in it. Without `systemd-run` it falls back to a new session. The output goes to
`update.log`, and the page reloads when the server reports the new version.

## Root approvals

When "Root password here" is on, the agent's `PATH` starts with `~/.local/opt/agents-web/bin`, which holds
a `pkexec` wrapper, and the agent gets two environment variables, a per-run token and the path of a unix
socket.

1. The wrapper starts `pkttyagent --process <its own pid>`, so the polkit text agent handles only this one
   request, and runs the real `/usr/bin/pkexec` with the same arguments.
2. When polkit asks for a password, the wrapper sends the command, its folder and the polkit message to
   the server over the socket. The server checks that the peer runs as the same user (`SO_PEERCRED`) and that
   the token belongs to a reply that is still running.
3. The chat shows a card with the command. Approving sends the password back to the wrapper, which types
   it into `pkttyagent`. Polkit verifies it, as usual. Denying, or 10 minutes without an answer, cancels
   the request.
4. Outside a web chat, or when the socket is missing, the wrapper simply runs the real `pkexec`.

The password is kept only in memory, for the moment it takes to hand it over, and is never logged or
saved.
