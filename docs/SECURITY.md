# Security

Agents Web gives a browser the same power as a terminal signed in to your agents. Anyone who can use the
page can make an agent read, change or delete your files and run commands as your user. The defaults
below keep that power on the PC and on devices you paired yourself.

## Who can reach it

- The server listens on port 47830 on IPv4 and answers only loopback addresses and the subnets of your
  real network interfaces. Requests from other addresses get `403`. ZeroTier, Docker, veth, bridge,
  libvirt, tun, tap, WireGuard and Tailscale interfaces are left out, so VPN and container peers are
  refused too.
- The `Host` header must be `localhost`, `127.0.0.1` or one of the PC's LAN addresses. This blocks DNS
  rebinding, where a website points its own name at your PC.
- The port is not opened in any firewall by the installer, and nothing is exposed to the internet unless
  you forward the port yourself. Do not do that.

## Who can use it

- **The PC.** A request from loopback gets the page with the PC token inside. Any program running on the
  PC, under any user, can therefore use Agents Web. On a shared computer, keep it stopped when you are
  not using it.
- **Paired devices.** Phones get a token only after a pairing. "Mobile" (or "Pair phone" in the tray)
  shows a random 6 digit PIN that works once and expires after 5 minutes. After 5 wrong PINs, pairing is
  locked for 10 minutes. Tokens and PINs are compared in constant time. Each device gets its own 256 bit token in an `HttpOnly`, `SameSite=Strict`
  cookie (`Secure` over https). The list of devices, with their last use, is in Mobile, where any of
  them can be revoked.
- Every API call needs the token in the `X-Token` header. Browsers do not let other websites set that
  header or read the page, and no CORS headers are ever sent, so a website you visit cannot use the API.
- The page is served with a Content Security Policy that blocks outside scripts, styles and connections
  (`default-src 'self'`, `connect-src 'self'`) and framing by other sites (`frame-ancestors 'none'`).

## https

Phones need https for the microphone and notifications, so the server makes its own certificate
authority on first start, with OpenSSL (EC P-256), and a server certificate for the PC's current
addresses. The certificate is renewed automatically when the addresses change or 30 days before it
expires.

The CA carries a critical `nameConstraints` extension that permits only `10.0.0.0/8`, `172.16.0.0/12`,
`192.168.0.0/16`, `127.0.0.0/8` and `localhost`. Even if its private key leaked, a certificate it
signs for a public website would be rejected by the phone. The private keys stay in
`~/.config/agents-web/tls/` (folder 700, files 600).

Plain http also works on the same port, so a phone can open the pairing page and download the CA. A
password for a root request is only accepted over https or from the PC itself.

## Agents and permissions

New chats run with full access (`bypassPermissions` for Claude, `--dangerously-skip-permissions` for agy),
because print mode cannot ask you about each tool. This is the same as running those flags in a terminal.
Choose "Accept edits", "Auto" or "Plan only" per chat if you want less. The agents run as your user, in
the folder you pick, with your normal environment.

## Root approvals

When an agent calls `pkexec`, the request appears in the chat with the exact command. Approving sends
your password once to a `pkttyagent` that serves only that `pkexec` process, and polkit checks it as it
always does. Details:

- The relay socket is `$XDG_RUNTIME_DIR/agents-web-pk.sock`, mode 600. The server accepts only peers that
  run as the same user (`SO_PEERCRED`) and present the token of a reply that is still running.
- The password is held in memory only while it is handed over. It is never written to disk or to a log.
- Requests nobody answers are denied after 10 minutes, and a request whose `pkexec` exits is cancelled.
- `sudo` is not relayed. Turn "Root password here" off in Settings to use the normal desktop polkit
  dialog instead.

## Files and outgoing connections

- Settings, tokens and certificates are in `~/.config/agents-web` (folder 700, `config.json` 600). Chats
  are saved with mode 600 in `~/.local/share/agents-web`. Uploads there follow your `umask`.
- Inline files use links signed with an HMAC key from `config.json`. A link covers one folder and the
  folders below it, never hidden ones, and never your whole home folder. It works without the token, so
  treat a copied link like the file itself.
- Agents Web itself connects only to `api.anthropic.com/api/oauth/usage` (Claude usage limits, with the
  account's own OAuth token). The CLIs make their own connections as usual. There is no telemetry and no
  update check. `agents-web update` downloads only when you run it.

## Reporting a problem

Please open a private security advisory on GitHub
(<https://github.com/gabrielmf1998/Agents-Web/security/advisories/new>) rather than a public issue.
