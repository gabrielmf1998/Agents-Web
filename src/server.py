#!/usr/bin/env python3
# Agents Web: a local web interface for the Claude Code CLI and the Antigravity CLI (agy).
# Python standard library only. Listens on 0.0.0.0 (IPv4) but only accepts loopback and the
# local network (zt*, docker, VPN and similar interfaces are left out). Phones pair with a PIN.
# https://github.com/gabrielmf1998/Agents-Web (MIT license)
import json, os, sys, time, threading, subprocess, signal, secrets, re, ipaddress, errno
import shutil, uuid, urllib.parse, http.server, socketserver, html, mimetypes, platform
import ssl, hmac, hashlib, socket, unicodedata, struct, shlex, sqlite3
from pathlib import Path

VERSAO = "2.2.0"
BASE = Path(__file__).resolve().parent
HOME = Path.home()
WIN = os.name == "nt"
if WIN:
    # The server runs windowless (pythonw): without this flag every CLI it starts would open a console window
    # of its own. Sign in asks for CREATE_NEW_CONSOLE, which Windows lets win over it.
    _Popen = subprocess.Popen

    class _PopenSemJanela(_Popen):
        def __init__(self, *a, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000  # CREATE_NO_WINDOW
            super().__init__(*a, **kw)
    subprocess.Popen = _PopenSemJanela
    _APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming")
    _LOCAL = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local")
# AGENTS_WEB_CONFIG_DIR and AGENTS_WEB_DATA_DIR run a second, separate instance (tests, trying a new version)
CFG_DIR = Path(os.environ.get("AGENTS_WEB_CONFIG_DIR") or (_APPDATA / "agents-web" if WIN else HOME / ".config/agents-web"))
DATA = Path(os.environ.get("AGENTS_WEB_DATA_DIR") or (_LOCAL / "agents-web" if WIN else HOME / ".local/share/agents-web"))
CHATS = DATA / "chats"
UPLOADS = DATA / "uploads"
CFG_FILE = CFG_DIR / "config.json"
PORTA = int(os.environ.get("AGENTS_WEB_PORT", "47830"))
LOCAL_BIN = HOME / ".local/bin"   # where the official Claude Code and agy installers put their commands


def _cli(nome):
    return shutil.which(nome) or str(LOCAL_BIN / (nome + ".exe" if WIN else nome))


CLAUDE = _cli("claude")
AGY = _cli("agy")

for d in (CFG_DIR, CHATS, UPLOADS, CFG_DIR / "contas"):
    d.mkdir(parents=True, exist_ok=True)
os.chmod(CFG_DIR, 0o700)

trava = threading.RLock()


# ---------------------------------------------------------------- config
def salvar_json(caminho, dados, modo=0o600):
    # one temp file per writer, so two threads saving the same file never mix their bytes
    tmp = Path(f"{caminho}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(dados, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, modo)
        troca_arquivo(tmp, caminho)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


SETPRIV = None if WIN else shutil.which("setpriv")


def troca_arquivo(org, dst):
    """os.replace. On Windows it fails while another thread has the target open for reading, so try again."""
    for i in range(40 if WIN else 1):
        try:
            return os.replace(org, dst)
        except PermissionError:
            if i == 39 or not WIN:
                raise
            time.sleep(0.05)


def mata_arvore(proc, forte=False):
    """Stop an agent and everything it started (its own process group on Linux, its process tree on Windows)."""
    if not proc or proc.poll() is not None:
        return
    if WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if forte else signal.SIGTERM)
    except ProcessLookupError:
        pass


def pid_vivo(pid):
    if WIN:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        codigo = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(codigo))
        k.CloseHandle(h)
        return bool(ok) and codigo.value == 259  # STILL_ACTIVE
    return Path(f"/proc/{int(pid)}").exists()


def carregar_config():
    cfg = {}
    if CFG_FILE.exists():
        try:
            cfg = json.loads(CFG_FILE.read_text())
        except Exception:
            cfg = {}
    antes = json.loads(json.dumps(cfg))
    cfg.setdefault("token", secrets.token_urlsafe(32))
    cfg.setdefault("dispositivos", [])
    cfg.setdefault("contas", [
        {"id": "claude1", "agente": "claude", "nome": "Claude", "dir": ""},
        {"id": "google", "agente": "agy", "nome": "Antigravity", "dir": ""},
    ])
    cfg.setdefault("padrao", {"cwd": str(HOME), "perm_claude": "bypassPermissions", "perm_agy": "skip"})
    if cfg != antes:
        salvar_json(CFG_FILE, cfg)
    return cfg


CFG = carregar_config()
if "segredo" not in CFG:
    CFG["segredo"] = secrets.token_hex(32)
    salvar_json(CFG_FILE, CFG)
SEGREDO = bytes.fromhex(CFG["segredo"])


def conta_por_id(cid):
    for c in CFG["contas"]:
        if c["id"] == cid:
            return c
    return None


def env_conta(conta):
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDECODE") or k.startswith("CLAUDE_CODE_")
                   or k in ("CLAUDE_CONFIG_DIR", "CLAUDE_PID", "CLAUDE_EFFORT"))}
    env["PATH"] = str(LOCAL_BIN) + os.pathsep + env.get("PATH", "" if WIN else "/usr/bin")
    if WIN:
        env.setdefault("PYTHONUTF8", "1")
    if conta and conta["agente"] == "claude" and conta.get("dir"):
        Path(conta["dir"]).mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = conta["dir"]
    return env


def dir_claude(conta):
    return Path(conta["dir"]) if conta.get("dir") else HOME / ".claude"


def conta_principal():
    """The Claude account whose memory every Claude account shares: the first one (claude1, ~/.claude)."""
    cs = [c for c in CFG["contas"] if c["agente"] == "claude"]
    return next((c for c in cs if c["id"] == "claude1"), cs[0] if cs else None)


def raiz_projeto(cwd):
    """The folder Claude Code files a project's memory under: the main checkout of the git repository that
    holds cwd (a linked worktree counts as its main checkout), or cwd itself outside a repository."""
    p = Path(os.path.realpath(cwd))
    for d in (p, *p.parents):
        g = d / ".git"
        if g.is_dir():
            return d
        if g.is_file():
            try:
                alvo = Path(g.read_text().split("gitdir:", 1)[1].strip())
            except (OSError, IndexError):
                return d
            alvo = alvo if alvo.is_absolute() else (d / alvo)
            if alvo.parent.name == "worktrees" and alvo.parent.parent.name == ".git":
                return alvo.parent.parent.parent
            return d
    return p


def memoria_comum(conta, cwd):
    """The memory folder a Claude run on this account should use (passed as autoMemoryDirectory), so all
    accounts read and write one memory per project: the main account's. None for the main account itself
    (its own default is that folder), when the option is off, or when the folder cannot be named for sure."""
    if not CFG["padrao"].get("memoria_unica", True) or not conta or conta["agente"] != "claude":
        return None
    princ = conta_principal()
    if not princ or princ["id"] == conta["id"] or dir_claude(princ).resolve() == dir_claude(conta).resolve():
        return None
    nome = re.sub(r"[^A-Za-z0-9]", "-", str(raiz_projeto(cwd)))
    base = dir_claude(princ) / "projects"
    if len(nome) > 200:
        # Claude Code shortens long names with a hash of its own: reuse the folder it already made
        achados = [p for p in base.glob(nome[:200] + "-*") if p.is_dir()]
        return achados[0] / "memory" if len(achados) == 1 else None
    return base / nome / "memory"


MEM_TRAVA = threading.Lock()


def _copia_memoria(org, dst, conta):
    """Copy the memory files of one project from another account into the shared folder. Files already
    there with other content are kept, and the copy gets the account id in its name. Index lines of
    MEMORY.md that point to files the shared index does not list yet are appended under a heading."""
    arqs = sorted(p for p in org.iterdir() if p.is_file() and not p.is_symlink() and not p.name.startswith("."))
    if not arqs:
        return None
    dst.mkdir(parents=True, exist_ok=True)
    nomes, n = {}, 0
    for p in arqs:
        if p.name == "MEMORY.md":
            continue
        alvo = dst / p.name
        dados = p.read_bytes()
        if alvo.exists() and alvo.read_bytes() != dados:
            alvo = dst / f"{p.stem}-{conta['id']}{p.suffix}"
        nomes[p.name] = alvo.name
        if alvo.exists() and alvo.read_bytes() == dados:
            continue
        shutil.copy2(p, alvo)
        n += 1
    idx = org / "MEMORY.md"
    if idx.is_file():
        destino = dst / "MEMORY.md"
        linhas = idx.read_text(errors="replace").splitlines()
        ler = lambda: destino.read_text(errors="replace") if destino.exists() else ""
        atual = ler()
        for _ in range(5):
            novas = []
            for linha in linhas:
                m = re.search(r"\]\(([^)\s]+)\)", linha)
                if not m:
                    continue
                ref = nomes.get(m.group(1), m.group(1))
                if f"]({ref})" in atual or any(f"]({ref})" in x for x in novas):
                    continue
                novas.append(linha.replace(f"]({m.group(1)})", f"]({ref})", 1))
            if not novas:
                break
            texto = (atual.rstrip("\n") + "\n\n" if atual.strip() else "") + \
                f"From the {conta.get('nome') or conta['id']} account\n" + "\n".join(novas) + "\n"
            tmp = destino.with_name(f".MEMORY.md.{secrets.token_hex(4)}")
            tmp.write_text(texto)
            agora = ler()
            if agora != atual:
                # Claude wrote to the index meanwhile: start again from what it wrote
                tmp.unlink()
                atual = agora
                continue
            if atual:
                guarda = DATA / "memory-backups"
                guarda.mkdir(parents=True, exist_ok=True)
                (guarda / f"{dst.parent.name}-MEMORY-{time.strftime('%Y%m%d-%H%M%S')}-{conta['id']}-{secrets.token_hex(2)}.md").write_text(atual)
            troca_arquivo(tmp, destino)
            n += 1
            break
    return n


def unir_memorias(cwd=None):
    """Once per account and project, bring what the other Claude accounts remembered on their own into the
    shared memory, so turning the shared memory on loses nothing. Their own folders stay as they were.
    With `cwd`, only the project of that folder (before a reply); without it, every project."""
    if not CFG["padrao"].get("memoria_unica", True):
        return
    princ = conta_principal()
    if not princ:
        return
    padrao = "*"
    if cwd:
        nome = re.sub(r"[^A-Za-z0-9]", "-", str(raiz_projeto(cwd)))
        padrao = nome if len(nome) <= 200 else nome[:200] + "-*"
    with MEM_TRAVA:
        with trava:
            feitos = {k: list(v) for k, v in (CFG.get("memorias_unidas") or {}).items()}
            contas = [dict(c) for c in CFG["contas"]]
        novos = {}
        for conta in contas:
            if conta["agente"] != "claude" or conta["id"] == princ["id"]:
                continue
            a, b = dir_claude(princ), dir_claude(conta)
            if a.resolve() == b.resolve():
                continue
            lista = feitos.get(conta["id"]) or []
            for org in sorted((b / "projects").glob(padrao + "/memory")):
                nome = org.parent.name
                if nome in lista or org.is_symlink() or not org.is_dir():
                    continue
                try:
                    n = _copia_memoria(org, a / "projects" / nome / "memory", conta)
                except OSError as e:
                    print(f"memory of {conta['id']} for {nome} not merged: {e!r}", flush=True)
                    continue
                if n is None:
                    continue    # nothing there yet: look again next time
                if n:
                    print(f"memory of {conta['id']} for {nome}: {n} files merged into {princ['id']}", flush=True)
                novos.setdefault(conta["id"], []).append(nome)
        if novos:
            with trava:
                todos = CFG.setdefault("memorias_unidas", {})
                for cid, nomes in novos.items():
                    todos[cid] = list(dict.fromkeys((todos.get(cid) or []) + nomes))
                salvar_json(CFG_FILE, CFG)


# ---------------------------------------------------------------- network
_rede = {"t": 0, "redes": [], "ips": []}


def _interfaces_linux():
    out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
    return [(p[1], p[3]) for p in (l.split() for l in out.splitlines()) if len(p) > 3]


def _interfaces_win():
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength"
                        " | ConvertTo-Json -Compress"], capture_output=True, text=True, timeout=20)
    j = json.loads(r.stdout or "[]")
    return [(str(x.get("InterfaceAlias") or ""), f"{x['IPAddress']}/{x['PrefixLength']}")
            for x in (j if isinstance(j, list) else [j]) if x.get("IPAddress")]


def info_rede():
    if time.time() - _rede["t"] < 30:
        return _rede
    if WIN and _rede["t"]:
        if not _rede.get("lendo"):
            _rede["lendo"] = True
            threading.Thread(target=_le_rede, daemon=True).start()
        return _rede
    return _le_rede()


def _le_rede():
    redes, ips = [], []
    try:
        for nome, cidr in (_interfaces_win() if WIN else _interfaces_linux()):
            if re.match(r"^(lo|zt|docker|veth|br-|virbr|tun|tap|wg|tailscale)", nome) or re.search(
                    r"(?i)loopback|zerotier|vethernet|docker|wsl|hyper-v|tailscale|wireguard|vpn|virtualbox|vmware|tap-", nome):
                continue
            itf = ipaddress.ip_interface(cidr)
            if not itf.ip.is_private or itf.ip.is_link_local or itf.ip.is_loopback:
                continue
            redes.append(itf.network)
            ips.append(str(itf.ip))
    except Exception:
        pass
    _rede.update(t=time.time(), redes=redes, ips=ips, lendo=False)
    if TLS["ips"] is not None and sorted(set(ips)) != TLS["ips"]:
        threading.Thread(target=garante_tls, args=(ips,), daemon=True).start()
    return _rede


# ---------------------------------------------------------------- https on the same port
# Own CA limited by nameConstraints to private IPs and localhost: even when installed on a
# phone it cannot sign any internet site.
TLS_DIR = CFG_DIR / "tls"
TLS = {"ctx": None, "ips": None, "erro": ""}
_tls_trava = threading.Lock()


def _acha_openssl():
    achado = shutil.which("openssl")
    if achado or not WIN:
        return achado
    for raiz in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "") + r"\Programs"):
        for sub in (r"Git\usr\bin\openssl.exe", r"Git\mingw64\bin\openssl.exe"):
            if raiz and os.path.isfile(os.path.join(raiz, sub)):
                return os.path.join(raiz, sub)
    return None


OPENSSL = _acha_openssl()


def _ossl(*args):
    r = subprocess.run([OPENSSL, *args], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "openssl failed").strip()[-300:])


def garante_tls(ips):
    with _tls_trava:
        alvo = sorted(set(ips))
        if TLS["ctx"] and TLS["ips"] == alvo:
            return
        try:
            TLS_DIR.mkdir(mode=0o700, exist_ok=True)
            os.chmod(TLS_DIR, 0o700)
            ca_k, ca_c = TLS_DIR / "ca.key", TLS_DIR / "ca.crt"
            sk, sc, sips = TLS_DIR / "srv.key", TLS_DIR / "srv.crt", TLS_DIR / "srv.ips"
            if not (ca_k.exists() and ca_c.exists()):
                _ossl("req", "-x509", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                      "-keyout", str(ca_k), "-out", str(ca_c), "-days", "3650",
                      "-subj", "/CN=Agents on this PC (local network only)",
                      "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                      "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                      "-addext", "nameConstraints=critical,permitted;IP:192.168.0.0/255.255.0.0,"
                                 "permitted;IP:10.0.0.0/255.0.0.0,permitted;IP:172.16.0.0/255.240.0.0,"
                                 "permitted;IP:127.0.0.0/255.0.0.0,permitted;DNS:localhost")
                sips.unlink(missing_ok=True)
            valido = False
            if sips.exists() and sips.read_text().split() == alvo and sc.exists():
                valido = subprocess.run([OPENSSL, "x509", "-checkend", str(30 * 86400), "-noout", "-in", str(sc)],
                                        capture_output=True).returncode == 0
            if not valido:
                san = ",".join(["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ip}" for ip in alvo])
                ext, csr = TLS_DIR / "srv.ext", TLS_DIR / "srv.csr"
                ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                               f"extendedKeyUsage=serverAuth\nsubjectAltName={san}\n"
                               "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
                _ossl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                      "-keyout", str(sk), "-out", str(csr), "-subj", "/CN=Agents")
                _ossl("x509", "-req", "-in", str(csr), "-CA", str(ca_c), "-CAkey", str(ca_k), "-set_serial",
                      str(secrets.randbits(62)), "-days", "397", "-sha256", "-extfile", str(ext), "-out", str(sc))
                csr.unlink(missing_ok=True)
                ext.unlink(missing_ok=True)
                sips.write_text(" ".join(alvo))
            for k in (ca_k, sk):
                os.chmod(k, 0o600)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(str(sc), str(sk))
            TLS.update(ctx=ctx, ips=alvo, erro="")
        except Exception as e:
            TLS["erro"] = str(e)
            print("tls:", e, flush=True)


# ---------------------------------------------------------------- files from this PC in the chat
# /f/<signature>/<path>: the HMAC signature unlocks one file, or its folder when the folder is
# specific (3+ levels deep, not the home folder, no hidden folders), so images, videos and HTML
# pages show in the chat without a token in the URL. Everything is served with a CSP sandbox.
PREV = DATA / "previews"
PREV.mkdir(exist_ok=True)
PREV_REAL = Path(os.path.realpath(PREV))
HOME_REAL = Path(os.path.realpath(HOME))
EXT_MIDIA = {".png": "img", ".jpg": "img", ".jpeg": "img", ".gif": "img", ".webp": "img", ".avif": "img",
             ".bmp": "img", ".svg": "img", ".ico": "img", ".mp4": "video", ".webm": "video", ".mkv": "video",
             ".mov": "video", ".m4v": "video", ".ogv": "video", ".mp3": "audio", ".ogg": "audio", ".oga": "audio",
             ".opus": "audio", ".wav": "audio", ".flac": "audio", ".m4a": "audio", ".aac": "audio",
             ".pdf": "pdf", ".html": "html", ".htm": "html"}
TIPOS_EXTRA = {".opus": "audio/ogg", ".mkv": "video/x-matroska", ".js": "text/javascript", ".mjs": "text/javascript",
               ".wasm": "application/wasm", ".m4a": "audio/mp4", ".avif": "image/avif", ".webp": "image/webp"}
CSP_DOC = ("sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads allow-pointer-lock; "
           "default-src * data: blob: 'unsafe-inline' 'unsafe-eval'")
AUTO_ALTURA = (b'\n<script>(function(){function s(){try{var d=document.documentElement,b=document.body,'
               b'h=d.getBoundingClientRect().height;if(b){var r=b.getBoundingClientRect(),m=parseFloat(getComputedStyle(b).marginBottom)||0;'
               b'h=Math.max(h,r.bottom+m)}if(d.scrollHeight>innerHeight+1)h=Math.max(h,d.scrollHeight);'
               b'parent.postMessage({agH:Math.ceil(h)},"*")}catch(e){}}'
               b'addEventListener("load",s);if(window.ResizeObserver)new ResizeObserver(s).observe(document.documentElement);'
               b'setTimeout(s,60);setTimeout(s,900)})()</script>\n')
for _f in PREV.glob("*.html"):
    try:
        if time.time() - _f.stat().st_mtime > 60 * 86400:
            _f.unlink()
    except OSError:
        pass


def assina(alvo):
    return hmac.new(SEGREDO, str(alvo).encode(), hashlib.sha256).hexdigest()[:32]


def info_local(caminho):
    c = os.path.expanduser(str(caminho or "").strip())
    if c.startswith("file://"):
        c = urllib.parse.unquote(c[7:])
    if not os.path.isabs(c):
        return None
    real = Path(os.path.realpath(c))
    tipo = EXT_MIDIA.get(real.suffix.lower())
    if not tipo or not real.is_file():
        return None
    pasta = real.parent
    oculta = any(x.startswith(".") for x in pasta.parts)
    raiz = pasta if len(pasta.parts) >= 4 and pasta != HOME_REAL and not oculta else real
    return {"url": f"/f/{assina(raiz)}{urllib.parse.quote(str(real))}", "tipo": tipo,
            "tam": real.stat().st_size, "nome": real.name}


def acha_local(resto):
    sig, _, cam = resto.partition("/")
    if not re.fullmatch(r"[0-9a-f]{32}", sig):
        return None
    p = Path(os.path.realpath(os.path.normpath("/" + urllib.parse.unquote(cam))))
    if not p.is_file():
        return None
    for anc in [p, *p.parents]:
        if hmac.compare_digest(assina(anc), sig):
            if anc != p and any(x.startswith(".") for x in p.relative_to(anc).parts):
                return None
            return p
    return None


def salva_preview(codigo, tipo):
    codigo = str(codigo or "")
    if not codigo.strip() or len(codigo) > 3_000_000:
        raise ValueError("nothing to preview" if not codigo.strip() else "preview too large")
    if tipo == "svg" and "<html" not in codigo[:4000].lower():
        codigo = ('<!doctype html><meta charset="utf-8"><style>html,body{margin:0;background:#fff}'
                  'body{display:grid;place-items:center;padding:14px}svg{max-width:100%;height:auto}</style>' + codigo)
    nome = hmac.new(SEGREDO, codigo.encode(), hashlib.sha256).hexdigest()[:24] + ".html"
    arq = PREV / nome
    if not arq.exists():
        arq.write_text(codigo)
    else:
        os.utime(arq)
    return f"/f/{assina(PREV_REAL)}{urllib.parse.quote(str(PREV_REAL / nome))}"


SW_JS = """self.addEventListener("install",()=>self.skipWaiting());
self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));
self.addEventListener("notificationclick",e=>{e.notification.close();const id=(e.notification.data||{}).id||"";
e.waitUntil(self.clients.matchAll({type:"window",includeUncontrolled:true}).then(cs=>{for(const c of cs){c.postMessage({abrir:id});if("focus"in c)return c.focus()}return self.clients.openWindow("/"+(id?"#c="+id:""))}))});
"""


def origem_ok(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback:
        return True
    return any(a in r for r in info_rede()["redes"])


def host_ok(host):
    h = (host or "").rsplit(":", 1)[0].strip("[]").lower()
    return h in ("127.0.0.1", "localhost") or h in info_rede()["ips"]


# ---------------------------------------------------------------- chats
def caminho_chat(cid):
    if not re.fullmatch(r"[a-f0-9]{12}", cid or ""):
        raise ValueError("invalid id")
    return CHATS / (cid + ".json")


def apagar_chats(ids, arquivos=False, sessoes=False):
    feitos, pulados, livres, erros = 0, [], 0, []
    for cid in ids or []:
        caminho_chat(cid)  # validate every id before deleting any
    for cid in ids or []:
        ex = EXECS.get(cid)
        if ex and not ex.acabou:
            pulados.append(cid)
            continue
        arq = caminho_chat(cid)
        if not arq.exists():
            continue
        try:
            c = json.loads(arq.read_text())
        except Exception:
            c = {}
        if sessoes and conta_por_id(c.get("conta", "")):
            # a fork shares its parent's session until its first reply: only delete what no other chat uses
            outros = set()
            for r in listar_chats():
                if r["id"] != cid and r["id"] not in ids:
                    outros |= {r.get("sessao")} | set(r.get("antigas") or [])
            meus = ([] if c.get("fork") else [c.get("sessao")]) + list(c.get("antigas") or [])
            pares = [(c["conta"], x) for x in meus if x and x not in outros]
            # the sessions of the AIs called into this chat (Call another AI), on their own accounts
            cc = c.get("contas_conv") or {}
            pares += [(cc[x], x) for x in c.get("sids_conv") or [] if x and cc.get(x)]
            for conta_id, sid in dict.fromkeys(pares):
                if not conta_por_id(conta_id):
                    continue
                try:
                    livres += apagar_sessao(conta_por_id(conta_id), sid)
                except ValueError as e:
                    erros.append(f"{c.get('titulo', cid)}: {e}")
        if arquivos:
            for m in c.get("mensagens", []):
                for a in m.get("anexos") or []:
                    meta = ler_anexo(a.get("id"))
                    if not meta:
                        continue
                    alvo = Path(meta["caminho"]).resolve()
                    if UPLOADS.resolve() in alvo.parents and alvo.is_file():
                        livres += alvo.stat().st_size
                        alvo.unlink()
                        try:
                            alvo.parent.rmdir()
                        except OSError:
                            pass
                    (UPLOADS / (a["id"] + ".json")).unlink(missing_ok=True)
        arq.unlink(missing_ok=True)
        EXECS.pop(cid, None)
        troca_fixado(cid, "cli:" + c["sessao"] if c.get("sessao") and not sessoes and SID_RE.fullmatch(c["sessao"]) else None)
        feitos += 1
    return {"apagados": feitos, "pulados": pulados, "liberado": livres, "erros": erros}


def ler_chat(cid):
    return json.loads(caminho_chat(cid).read_text())


def gravar_chat(chat):
    chat["atualizado"] = time.time()
    salvar_json(caminho_chat(chat["id"]), chat)


def novo_chat(dados):
    agente = dados.get("agente") if dados.get("agente") in ("claude", "agy") else "claude"
    conta = conta_por_id(dados.get("conta") or "") or next(c for c in CFG["contas"] if c["agente"] == agente)
    if conta["agente"] != agente:
        raise ValueError("this account does not belong to the selected agent")
    chat = {
        "id": secrets.token_hex(6), "titulo": dados.get("titulo") or "New chat",
        "agente": agente, "conta": conta["id"], "modelo": dados.get("modelo", ""),
        "perm": dados.get("perm") or (CFG["padrao"]["perm_claude"] if agente == "claude" else CFG["padrao"]["perm_agy"]),
        "effort": dados.get("effort", ""), "cwd": dados.get("cwd") or CFG["padrao"]["cwd"],
        "sessao": dados.get("sessao", ""), "criado": time.time(), "mensagens": [],
    }
    if dados.get("ajustado"):
        chat["ajustado"] = True
    gravar_chat(chat)
    return chat


def resumo_chat(c):
    tot = totais(c)
    return {"id": c["id"], "titulo": c["titulo"], "agente": c["agente"], "conta": c["conta"],
            "modelo": c.get("modelo", ""), "atualizado": c.get("atualizado", 0), "n": len(c["mensagens"]),
            "ajustado": bool(c.get("ajustado")),
            "tokens": tot["total"], "fila": len(c.get("fila") or []), "sessao": c.get("sessao", ""),
            "cwd": c.get("cwd", ""), "origem": c.get("origem", ""), "antigas": c.get("antigas") or [],
            "sids_conv": c.get("sids_conv") or [],
            "duo": (c.get("convidado") or {}).get("agente", "") if c.get("duo") else "",
            "rodando": c["id"] in EXECS and not EXECS[c["id"]].acabou}


def totais(c):
    t = {"entrada": 0, "saida": 0, "cache_l": 0, "cache_e": 0, "pensamento": 0, "custo": 0.0, "total": 0}
    for m in c["mensagens"]:
        u = m.get("uso") or {}
        for k in ("entrada", "saida", "cache_l", "cache_e", "pensamento"):
            t[k] += int(u.get(k) or 0)
        t["custo"] += float(m.get("custo") or 0)
    t["total"] = t["entrada"] + t["saida"] + t["cache_l"] + t["cache_e"]
    return t


_resumos = {}


def listar_chats():
    out, vistos = [], set()
    for p in CHATS.glob("*.json"):
        vistos.add(p.name)
        try:
            st = p.stat()
            chave = (st.st_mtime_ns, st.st_size)
            r = _resumos.get(p.name)
            if not r or r[0] != chave:
                r = (chave, resumo_chat(json.loads(p.read_text())))
                _resumos[p.name] = r
            ex = EXECS.get(r[1]["id"])
            out.append({**r[1], "rodando": bool(ex and not ex.acabou)})
        except Exception:
            pass
    for k in list(_resumos):
        if k not in vistos:
            _resumos.pop(k, None)
    out.sort(key=lambda x: -x["atualizado"])
    return out


_NM, _textos = {}, {}


def _norm(s):
    # lowercase without accents, one character per character (positions match the original)
    out = []
    for ch in s:
        r = _NM.get(ch)
        if r is None:
            r = (unicodedata.normalize("NFKD", ch)[:1] or ch).lower()[:1] or ch
            _NM[ch] = r
        out.append(r)
    return "".join(out)


def _textos_chat(c):
    ts = [(-1, c.get("titulo", ""))]
    for i, m in enumerate(c.get("mensagens", [])):
        t = (m.get("texto") or "")[:200_000]
        nomes = " ".join(a.get("nome", "") for a in (m.get("anexos") or []))
        t = (t + "\n" + nomes) if t and nomes else (t or nomes)
        if t:
            ts.append((i, t))
    return [(i, t, _norm(t)) for i, t in ts]


def buscar(q):
    nq = _norm(re.sub(r"\s+", " ", (q or "").strip()))
    if len(nq) < 2:
        return []
    out, lim = [], (lambda x: re.sub(r"\s+", " ", x))
    for r in listar_chats():
        p = CHATS / (r["id"] + ".json")
        try:
            st = p.stat()
            chave = (st.st_mtime_ns, st.st_size)
            t = _textos.get(r["id"])
            if not t or t[0] != chave:
                t = (chave, _textos_chat(json.loads(p.read_text())))
                _textos[r["id"]] = t
        except Exception:
            continue
        achou, n = None, 0
        for i, orig, nt in t[1]:
            k = nt.find(nq)
            if k < 0:
                continue
            n += nt.count(nq)
            if achou is None:
                achou = (i, orig, k)
        if achou is None:
            continue
        i, orig, k = achou
        a, b = max(0, k - 60), min(len(orig), k + len(nq) + 100)
        out.append({**r, "busca": {"msg": i, "n": n, "achado": orig[k:k + len(nq)],
                                   "antes": ("…" if a else "") + lim(orig[a:k]).lstrip(),
                                   "depois": lim(orig[k + len(nq):b]) + ("…" if b < len(orig) else "")}})
    for k in list(_textos):
        if not (CHATS / (k + ".json")).exists():
            _textos.pop(k, None)
    return out[:100]


# ---------------------------------------------------------------- agent runs
EXECS = {}
DICA = ("You are replying in the user's own web chat interface for this PC, not a terminal (it may be open on their "
        "phone). Markdown is fully rendered, tables included. When a reply has data worth seeing (comparisons, specs, "
        "numbers, rankings, timelines, progress, flows or architectures), add a visual: one self-contained ```html code "
        "block, which the interface renders as a live preview in a sandboxed frame (scripts and CDN libraries work; no "
        "localStorage, cookies or access to this PC). Skip it for short or purely conversational replies, and keep the "
        "text answer complete on its own. The frame already carries the interface's theme (dark or light) and font: leave the page "
        "background transparent and style with the CSS variables --ag-text, --ag-muted, --ag-surface, --ag-surface-2, "
        "--ag-border, --ag-accent, --ag-ok, --ag-warn, --ag-err, --ag-radius and --ag-c1 to --ag-c4 (chart series), so it "
        "blends in. To show a file from this PC inline (image, video, audio, PDF or an .html page), write its absolute "
        "path in backticks, for example `/tmp/result.png`.")
DICA_PK = ("Commands that need root: run them with plain `pkexec` (never sudo) and give that shell call a 10 minute "
           "timeout, because the user approves each one from this interface, maybe on the phone.")

# ---------------------------------------------------------------- pkexec through the browser
# bin/pkexec (first in the agents' PATH) registers a pkttyagent for itself only and asks for the
# password here through a unix socket
PK_BIN = BASE / "bin"
PK_SOCK = (Path(os.environ.get("XDG_RUNTIME_DIR") or ("" if WIN else f"/run/user/{os.getuid()}"))
           / ("agents-web-pk.sock" if PORTA == 47830 else f"agents-web-pk-{PORTA}.sock"))
PK = {"ok": False}
PK_TOKENS = {}
PEDIDOS = {}
PK_TRAVA = threading.Lock()
PK_ESPERA = 600


def pk_ativo():
    return PK["ok"] and CFG["padrao"].get("root", True)


def pk_muda(p, estado, **kw):
    p["pub"].update(estado=estado, **kw)
    p["ex"].emite({"t": "pedido", **p["pub"]})


def pk_novo(ex, d):
    cmd = " ".join(shlex.quote(str(x)) for x in (d.get("cmd") or [])[:300])[:4000]
    pub = {"id": secrets.token_hex(8), "tipo": "root", "cmd": cmd, "texto": str(d.get("texto") or "")[:600],
           "cwd": str(d.get("cwd") or "")[:600], "ts": time.time(), "estado": "pendente"}
    p = {"pub": pub, "ex": ex, "ev": threading.Event(), "resp": None}
    with PK_TRAVA:
        PEDIDOS[pub["id"]] = p
    ex.msg.setdefault("pedidos", []).append(pub)
    ex.emite({"t": "pedido", **pub})
    return p


def pk_espera(p, c):
    fim = time.time() + PK_ESPERA
    while not p["ev"].wait(1):
        motivo = ""
        if time.time() > fim:
            motivo, estado = "nobody answered in the web interface within 10 minutes", "expirou"
        else:
            try:
                if c.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                    motivo, estado = "cancelled", "cancelado"
            except BlockingIOError:
                pass
        if motivo:
            with PK_TRAVA:
                if p["pub"]["estado"] == "pendente":
                    p["resp"] = {"r": "negar", "msg": motivo}
                    pk_muda(p, estado)
                    p["ev"].set()
    resp, p["resp"] = p["resp"], None
    return resp or {"r": "negar"}


def pk_conexao(c):
    atual = None
    try:
        uid = struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
        if uid != os.getuid():
            return
        rf = c.makefile("r", encoding="utf-8")
        for linha in rf:
            d = json.loads(linha)
            est = atual["pub"]["estado"] if atual else ""
            if "token" in d:
                if atual:
                    if est == "verificando":
                        pk_muda(atual, "falhou")
                    with PK_TRAVA:
                        PEDIDOS.pop(atual["pub"]["id"], None)
                ex = PK_TOKENS.get(str(d.get("token")))
                if not ex or ex.acabou or not pk_ativo():
                    atual = None
                    c.sendall(b'{"r": "negar", "msg": "this request did not come from a running web chat"}\n')
                    continue
                atual = pk_novo(ex, d)
                c.sendall((json.dumps(pk_espera(atual, c)) + "\n").encode())
            elif d.get("ok") and est == "verificando":
                pk_muda(atual, "ok")
            elif d.get("falhou") and est == "verificando":
                pk_muda(atual, "falhou")
            elif "fim" in d and atual:
                rc = d.get("rc")
                if est == "verificando":
                    pk_muda(atual, "ok" if d["fim"] == "ok" else "falhou" if d["fim"] == "falhou" else "erro", rc=rc)
                elif est == "ok":
                    pk_muda(atual, "ok", rc=rc)
                break
    except Exception:
        pass
    finally:
        if atual:
            with PK_TRAVA:
                if atual["pub"]["estado"] in ("pendente", "verificando"):
                    pk_muda(atual, "cancelado")
                PEDIDOS.pop(atual["pub"]["id"], None)
        try:
            c.close()
        except OSError:
            pass


def pk_servidor():
    try:
        PK_SOCK.unlink()
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(PK_SOCK))
    except OSError as e:
        print(f"root approvals from the browser are off: cannot create {PK_SOCK} ({e.strerror})", flush=True)
        return
    os.chmod(PK_SOCK, 0o600)
    s.listen(16)
    PK["ok"] = True
    while True:
        try:
            c, _ = s.accept()
        except OSError:
            continue
        threading.Thread(target=pk_conexao, args=(c,), daemon=True).start()


def pk_responde(pid, acao, senha, seguro):
    with PK_TRAVA:
        p = PEDIDOS.get(str(pid))
        if not p or p["pub"]["estado"] != "pendente":
            raise ValueError("this request is no longer waiting")
        if acao == "aprovar":
            if not seguro:
                raise ValueError("open the secure (https) address to send a password")
            senha = str(senha or "")
            if not senha or len(senha) > 1024 or "\n" in senha or "\r" in senha:
                raise ValueError("invalid password")
            p["resp"] = {"r": "senha", "senha": senha}
            pk_muda(p, "verificando")
        else:
            p["resp"] = {"r": "negar", "msg": "the user denied this request in the web interface"}
            pk_muda(p, "negado")
        p["ev"].set()


def pk_pendentes():
    with PK_TRAVA:
        return [{**p["pub"], "chat": p["ex"].chat_id, "titulo": p["ex"].chat.get("titulo", "")}
                for p in PEDIDOS.values() if p["pub"]["estado"] == "pendente"]


LIMITES_ARQ = DATA / "limites.json"
try:
    LIMITES = json.loads(LIMITES_ARQ.read_text())
except Exception:
    LIMITES = {}


def corta(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"\n... (+{len(s) - n} caracteres)"


# ---------------------------------------------------------------- diffs of file edits
PATCH_LINHAS = 500


def _hunks_limpos(hunks):
    # the CLI's structuredPatch format, capped so a huge rewrite cannot bloat a chat file
    out, total, cortado = [], 0, False
    for h in hunks or []:
        if not isinstance(h, dict):
            continue
        linhas = [str(x)[:400] for x in (h.get("lines") or []) if isinstance(x, str)]
        if total + len(linhas) > PATCH_LINHAS:
            linhas = linhas[:max(0, PATCH_LINHAS - total)]
            cortado = True
        total += len(linhas)
        out.append({"a": int(h.get("oldStart") or 0), "b": int(h.get("newStart") or 0), "linhas": linhas})
        if cortado:
            break
    return out, cortado


def _diff(antes, depois):
    import difflib
    hunks, atual = [], None
    for l in difflib.unified_diff(antes.splitlines(), depois.splitlines(), lineterm="", n=3):
        if l.startswith(("---", "+++")) and atual is None:
            continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", l)
        if m:
            atual = {"oldStart": int(m.group(1)), "newStart": int(m.group(2)), "lines": []}
            hunks.append(atual)
        elif atual is not None:
            atual["lines"].append(l)
    return hunks


def _le_texto(caminho, limite=2_000_000):
    try:
        p = Path(caminho)
        if p.is_file() and p.stat().st_size <= limite:
            return p.read_text(errors="replace")
    except OSError:
        pass
    return None


def patch_pedido(nome, e, cwd):
    """Diff of an Edit or Write the agent asks to make, computed before it runs."""
    if not isinstance(e, dict) or not isinstance(e.get("file_path"), str):
        return None
    arq = e["file_path"] if os.path.isabs(e["file_path"]) else os.path.join(cwd or str(HOME), e["file_path"])
    atual = _le_texto(arq)
    if nome == "Write" and isinstance(e.get("content"), str):
        hunks = _diff(atual or "", e["content"])
        novo = atual is None
    elif nome == "Edit" and isinstance(e.get("old_string"), str) and isinstance(e.get("new_string"), str):
        novo = False
        if atual is not None and e["old_string"] and e["old_string"] in atual:
            depois = (atual.replace(e["old_string"], e["new_string"]) if e.get("replace_all")
                      else atual.replace(e["old_string"], e["new_string"], 1))
            hunks = _diff(atual, depois)
        else:
            hunks = _diff(e["old_string"], e["new_string"])
    else:
        return None
    h, cortado = _hunks_limpos(hunks)
    return {"arquivo": arq, "novo": novo, "hunks": h, "cortado": cortado}


def patch_resultado(nome, r):
    """Diff the CLI reports after an Edit or Write ran (tool_use_result.structuredPatch)."""
    if nome not in ("Edit", "Write") or not isinstance(r, dict) or not isinstance(r.get("filePath"), str):
        return None
    hunks = r.get("structuredPatch") or []
    novo = r.get("type") == "create"
    if not hunks and isinstance(r.get("content"), str):
        hunks = _diff("", r["content"])
    if not hunks:
        return None
    h, cortado = _hunks_limpos(hunks)
    return {"arquivo": r["filePath"], "novo": novo, "hunks": h, "cortado": cortado}


# ---------------------------------------------------------------- approvals asked by Claude Code
# Every Claude run uses --permission-prompt-tool stdio: when the CLI needs a yes or no (a tool the
# permission mode does not allow by itself, an AskUserQuestion, a plan to approve) it writes a
# control_request on stdout and waits for our control_response on stdin. The request becomes a
# card in the chat, like the root requests above, and shares their list (PEDIDOS).
LIMPA_ENTRADA = 20_000


def _entrada_pub(e):
    # what the page shows: the same input, long strings shortened
    if isinstance(e, dict):
        return {k: _entrada_pub(v) for k, v in list(e.items())[:60]}
    if isinstance(e, list):
        return [_entrada_pub(v) for v in e[:60]]
    if isinstance(e, str) and len(e) > LIMPA_ENTRADA:
        return e[:LIMPA_ENTRADA] + f"\n... (+{len(e) - LIMPA_ENTRADA} characters)"
    return e


def ctl_novo(ex, rid, r):
    nome = str(r.get("tool_name") or "?")
    entrada = r.get("input") if isinstance(r.get("input"), dict) else {}
    tipo = {"AskUserQuestion": "pergunta", "ExitPlanMode": "plano"}.get(nome, "ferramenta")
    pub = {"id": secrets.token_hex(8), "tipo": tipo, "ferramenta": nome, "entrada": _entrada_pub(entrada),
           "descricao": str(r.get("description") or "")[:600], "caminho": str(r.get("blocked_path") or "")[:600],
           "ts": time.time(), "estado": "pendente"}
    if tipo == "ferramenta":
        pt = patch_pedido(nome, entrada, ex.chat.get("cwd"))
        if pt:
            pub["patch"] = pt
    p = {"pub": pub, "ex": ex, "rid": rid, "entrada": entrada, "ctl": True}
    with PK_TRAVA:
        PEDIDOS[pub["id"]] = p
    ex.msg.setdefault("pedidos", []).append(pub)
    ex.emite({"t": "pedido", **pub})
    return p


def _chat_muda(ex, **campos):
    # settings changed from an approval card, saved now and kept by the running reply too
    ex.chat.update(campos)
    with trava:
        try:
            c = ler_chat(ex.chat_id)
        except (FileNotFoundError, ValueError):
            return
        if ex.conv:
            # the other AI's approvals stay with it, the chat's own AI keeps its settings
            if mesmo_convidado(c.get("convidado"), ex.conv):
                c["convidado"].update({k: v for k, v in campos.items() if k in ("perm", "permitidos")})
        else:
            c.update(campos)
        gravar_chat(c)


def ctl_responde(p, d):
    pub, ex, acao = p["pub"], p["ex"], d.get("acao")
    motivo = re.sub(r"\s+", " ", str(d.get("motivo") or "")).strip()[:2000]
    muda = {}  # chat settings this answer changes, saved only once the agent got the answer
    if pub["tipo"] == "ferramenta":
        if acao in ("aprovar", "sempre"):
            resp, estado = {"behavior": "allow", "updatedInput": p["entrada"]}, "permitido"
            if acao == "sempre":
                lista = [x for x in (ex.chat.get("permitidos") or []) if x != pub["ferramenta"]]
                muda["permitidos"] = (lista + [pub["ferramenta"]])[-50:]
                pub["sempre"] = True
        elif acao == "negar":
            resp = {"behavior": "deny", "message": "The user denied this in the web interface"
                    + (f" and said: {motivo}" if motivo else ".")}
            estado = "negado"
        else:
            raise ValueError("unknown answer")
    elif pub["tipo"] == "pergunta":
        if acao == "responder":
            perguntas = [q.get("question") for q in (p["entrada"].get("questions") or []) if isinstance(q, dict)]
            dadas = d.get("respostas") if isinstance(d.get("respostas"), dict) else {}
            respostas = {q: re.sub(r"[\r\n]+", " ", str(dadas.get(q) or "")).strip()[:2000] for q in perguntas}
            if not any(respostas.values()):
                raise ValueError("pick or type an answer first")
            resp = {"behavior": "allow", "updatedInput": {**p["entrada"], "answers": respostas}}
            pub["respostas"], estado = respostas, "respondido"
        elif acao == "negar":
            resp = {"behavior": "deny", "message": "The user chose not to answer"
                    + (f" and said: {motivo}" if motivo else ". Continue with your best judgement.")}
            estado = "negado"
        else:
            raise ValueError("unknown answer")
    else:  # plano
        if acao == "aprovar":
            modo = d.get("modo") if d.get("modo") in ("acceptEdits", "manual") else "manual"
            resp = {"behavior": "allow", "updatedInput": p["entrada"]}
            if modo == "acceptEdits":
                resp["updatedPermissions"] = [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
            # like the terminal: once the plan is approved, the chat leaves plan mode
            muda["perm"] = modo
            pub["modo"], estado = modo, "aprovado"
        elif acao == "negar":
            resp = {"behavior": "deny", "message": "The user wants changes to the plan"
                    + (f": {motivo}" if motivo else ". Ask what to change.")}
            estado = "negado"
        else:
            raise ValueError("unknown answer")
    if motivo and estado == "negado":
        pub["motivo"] = motivo
    if not ex.responde_ctl(p["rid"], resp):
        raise ValueError("the agent is no longer waiting for this")
    if muda:
        _chat_muda(ex, **muda)
    pk_muda(p, estado)


class Execucao:
    def __init__(self, chat, texto, anexos, convidado=None, oculta=False, debate=False, duo=False):
        self.chat_id = chat["id"]
        # a guest run (Call another AI) sees the chat through its own agent, account, model and session
        self.conv = convidado
        self.oculta = oculta      # a debate turn of the chat's own AI: no message of yours before it
        self.debate = debate
        # a reply of the chat's own AI to a message of yours: in a duo chat the other AI answers right after
        self.seguir_duo = not convidado and not oculta
        if convidado:
            chat = {**chat, "agente": convidado["agente"], "conta": convidado["conta"],
                    "modelo": convidado.get("modelo", ""), "perm": convidado.get("perm", ""),
                    "effort": convidado.get("effort", "") if convidado["agente"] == "claude" else "",
                    "sessao": convidado.get("sessao", ""), "permitidos": list(convidado.get("permitidos") or [])}
            chat.pop("fork", None)
        self.chat = chat
        self.eventos = []
        self.cond = threading.Condition()
        self.acabou = False
        self.proc = None
        self.parado = False
        self.texto = texto
        self.anexos = anexos
        self.msg = {"papel": "assistente", "texto": "", "pensamento": "", "ferramentas": [], "uso": {},
                    "custo": 0, "duracao": 0, "contexto": {}, "modelo": chat.get("modelo", ""), "erro": "",
                    "ts": time.time()}
        if convidado:
            self.msg["convidado"] = {"agente": convidado["agente"], "conta": convidado["conta"],
                                     "nome": convidado.get("nome", "")}
        if debate:
            self.msg["debate"] = True
        if duo:
            self.msg["duo"] = True
        self.inicio = time.time()
        ATIVIDADE["t"] = time.time()
        self.pk = secrets.token_hex(16)
        self.trava_in = threading.Lock()
        self.iniciou = False

    def emite(self, ev):
        with self.cond:
            self.eventos.append(ev)
            self.cond.notify_all()

    def comando(self):
        c = self.chat
        if c["agente"] == "claude":
            # stream-json on stdin too: the prompt goes in as a message and stdin stays open for the
            # answers to permission requests, questions and plan approvals, and for a clean interrupt
            cmd = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                   "--include-partial-messages", "--permission-prompt-tool", "stdio"]
            if c.get("modelo"):
                cmd += ["--model", c["modelo"]]
            if c.get("perm"):
                cmd += ["--permission-mode", c["perm"]]
            if c.get("effort"):
                cmd += ["--effort", c["effort"]]
            if c.get("sessao"):
                cmd += ["--resume", c["sessao"]]
                if isinstance(c.get("fork"), dict):
                    # edit and resend, retry, fork: branch the CLI session at an earlier reply (the
                    # original session stays as it was)
                    if c["fork"].get("em"):
                        cmd += ["--resume-session-at", c["fork"]["em"]]
                    cmd += ["--fork-session"]
            mem = memoria_comum(conta_por_id(c.get("conta", "")), c.get("cwd") or str(HOME))
            if mem:
                cmd += ["--settings", json.dumps({"autoMemoryDirectory": str(mem)})]
            extra = [DICA] if CFG["padrao"].get("dica", True) else []
            if pk_ativo():
                extra.append(DICA_PK)
            if extra:
                cmd += ["--append-system-prompt", " ".join(extra)]
            return cmd, "json"
        cmd = [AGY, "--output-format", "stream-json"]
        if c.get("modelo"):
            cmd += ["--model", c["modelo"]]
        if c.get("perm") == "skip":
            cmd += ["--dangerously-skip-permissions"]
        elif c.get("perm") in ("accept-edits", "auto", "plan"):
            cmd += ["--mode", c["perm"]]
        if c.get("sessao"):
            cmd += ["--conversation", c["sessao"]]
        return cmd, "arg"

    def prompt(self):
        t = self.texto
        if not self.conv:
            nota = nota_anfitriao(self.chat, self.oculta)
            if nota:
                t = nota + "\n\n" + (t if self.oculta else "The user's new message:\n" + t)
        if self.chat["agente"] == "agy" and not self.chat.get("sessao"):
            nota = " ".join(([DICA] if CFG["padrao"].get("dica", True) else []) + ([DICA_PK] if pk_ativo() else []))
            if nota:
                t = f"[Interface note: {nota}]\n\n" + t
        if self.anexos:
            t += "\n\n[Files sent through the web interface, already saved on this PC]\n"
            for a in self.anexos:
                t += f"- {a['caminho']} ({a['tipo']}, {a['tam']} bytes)\n"
        return t

    def rodar(self):
        try:
            self._rodar()
        except Exception as e:
            self.msg["erro"] = f"internal error: {e!r}"
            self.emite({"t": "erro", "s": self.msg["erro"]})
        finally:
            self.finalizar()

    def _rodar(self):
        c = self.chat
        conta = conta_por_id(c["conta"])
        if c["agente"] == "claude":
            try:
                unir_memorias(c.get("cwd") or str(HOME))
            except Exception as e:
                print(f"merging memories failed: {e!r}", flush=True)
        cmd, modo = self.comando()
        texto = self.prompt()
        if modo == "arg":
            if len(texto.encode()) > 100_000:
                pasta = UPLOADS / "prompts"
                pasta.mkdir(exist_ok=True)
                arq = pasta / f"{self.chat_id}-{int(time.time())}.md"
                arq.write_text(texto)
                texto = (f"The user's message is too long for the command line, so it was saved in full to {arq}. "
                         "Read that file first and treat its content as the user's message.")
            cmd += ["-p", texto]
        cwd = c.get("cwd") or str(HOME)
        if not os.path.isdir(cwd):
            raise RuntimeError(f"working folder does not exist: {cwd}")
        # setpriv --pdeathsig: if this server dies, the agent dies with it (on Windows the tray's job object does that)
        if SETPRIV:
            cmd = [SETPRIV, "--pdeathsig", "KILL", "--"] + cmd
        env = env_conta(conta)
        if pk_ativo():
            PK_TOKENS[self.pk] = self
            env.update(PATH=f"{PK_BIN}:{env['PATH']}", AGW_PK=self.pk, AGW_PK_SOCK=str(PK_SOCK))
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True,
                                     stdin=subprocess.PIPE if modo == "json" else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.erros = b""

        def le_erro():
            for linha in self.proc.stderr:
                self.erros = (self.erros + linha)[-6000:]
        threading.Thread(target=le_erro, daemon=True).start()
        if modo == "json":
            self.escreve({"type": "user", "message": {"role": "user", "content": texto},
                          "parent_tool_use_id": None, "session_id": ""})
        parse = self.parse_claude if c["agente"] == "claude" else self.parse_agy
        self.vistos = set()
        self.ferr = {}
        for linha in self.proc.stdout:
            linha = linha.strip()
            if not linha:
                continue
            try:
                d = json.loads(linha)
            except Exception:
                continue
            try:
                parse(d)
            except Exception as e:
                self.emite({"t": "aviso", "s": f"unrecognized event: {e!r}"})
        self.fecha_entrada()
        rc = self.proc.wait()
        if rc != 0 and not self.msg["texto"] and not self.msg["erro"] and not self.parado:
            self.msg["erro"] = f"exited with code {rc}: " + self.erros.decode(errors="replace")[-1500:]
            self.emite({"t": "erro", "s": self.msg["erro"]})

    # ---- Claude Code stream-json
    def parse_claude(self, d):
        t = d.get("type")
        if t == "control_request":
            r = d.get("request") or {}
            if r.get("subtype") != "can_use_tool":
                self.escreve({"type": "control_response", "response": {
                    "subtype": "error", "request_id": d.get("request_id"), "error": "not supported by Agents Web"}})
            elif (r.get("tool_name") not in ("AskUserQuestion", "ExitPlanMode")
                  and r.get("tool_name") in (self.chat.get("permitidos") or [])):
                self.responde_ctl(d.get("request_id"), {"behavior": "allow", "updatedInput": r.get("input") or {}})
            else:
                ctl_novo(self, d.get("request_id"), r)
        elif t == "control_cancel_request":
            with PK_TRAVA:
                for p in PEDIDOS.values():
                    if p["ex"] is self and p.get("rid") == d.get("request_id") and p["pub"]["estado"] == "pendente":
                        pk_muda(p, "cancelado")
        elif t == "control_response":
            pass
        elif t == "system" and d.get("subtype") == "init":
            self.iniciou = True
            self.chat["sessao"] = d.get("session_id", self.chat.get("sessao", ""))
            self.msg["modelo"] = d.get("model", "")
            self.emite({"t": "sistema", "modelo": self.msg["modelo"], "sessao": self.chat["sessao"],
                        "perm": d.get("permissionMode")})
        elif t == "stream_event":
            if d.get("parent_tool_use_id"):
                return
            e = d.get("event", {})
            if e.get("type") == "content_block_delta":
                dl = e.get("delta", {})
                if dl.get("type") == "text_delta":
                    self.emite({"t": "texto", "s": dl.get("text", "")})
                elif dl.get("type") == "thinking_delta":
                    self.msg["pensamento"] += dl.get("thinking", "")
                    self.emite({"t": "pensando", "s": dl.get("thinking", "")})
        elif t == "assistant":
            m = d.get("message", {})
            if d.get("parent_tool_use_id"):
                for b in m.get("content", []):
                    if b.get("type") == "tool_use":
                        self.emite({"t": "sub", "s": b.get("name", "")})
                return
            blocos = m.get("content") or []
            if (d.get("uuid") and any(b.get("type") == "text" for b in blocos)
                    and not any(b.get("type") == "tool_use" for b in blocos)):
                # where a later edit, retry or fork branches off: a text block with no pending tool call
                self.msg["uuid"] = d["uuid"]
            u = m.get("usage") or {}
            ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                     "cache_read_input_tokens", "output_tokens"))
            if ctx:
                self.msg["contexto"]["usado"] = ctx
            for i, b in enumerate(m.get("content", [])):
                chave = (m.get("id"), json.dumps(b, sort_keys=True)[:4000])
                if chave in self.vistos:
                    continue
                self.vistos.add(chave)
                if b.get("type") == "text":
                    if self.msg["texto"] and not self.msg["texto"].endswith("\n"):
                        self.msg["texto"] += "\n\n"
                    self.msg["texto"] += b.get("text", "")
                elif b.get("type") == "tool_use":
                    f = {"id": b.get("id"), "nome": b.get("name"), "entrada": corta(b.get("input"), 3000),
                         "saida": "", "erro": False, "fim": False}
                    self.ferr[f["id"]] = f
                    self.msg["ferramentas"].append(f)
                    self.emite({"t": "ferramenta", **f})
            self.emite({"t": "ctx", "usado": self.msg["contexto"].get("usado", 0)})
        elif t == "user":
            if d.get("parent_tool_use_id"):
                return
            blocos = [b for b in (d.get("message", {}).get("content") or [])
                      if isinstance(b, dict) and b.get("type") == "tool_result"]
            for b in blocos:
                cont = b.get("content")
                if isinstance(cont, list):
                    cont = "\n".join(x.get("text", "[" + x.get("type", "?") + "]") for x in cont if isinstance(x, dict))
                f = self.ferr.get(b.get("tool_use_id"))
                if f:
                    f.update(saida=corta(cont or "", 6000), erro=bool(b.get("is_error")), fim=True)
                    ev = {"t": "resultado", "id": f["id"], "saida": f["saida"], "erro": f["erro"]}
                    if len(blocos) == 1 and not f["erro"]:
                        pt = patch_resultado(f["nome"], d.get("tool_use_result"))
                        if pt:
                            f["patch"] = ev["patch"] = pt
                    self.emite(ev)
        elif t == "rate_limit_event":
            LIMITES[self.chat["conta"]] = {"info": d.get("rate_limit_info", {}), "quando": time.time()}
            try:
                salvar_json(LIMITES_ARQ, LIMITES)
            except Exception:
                pass
            self.emite({"t": "limite", "conta": self.chat["conta"], **LIMITES[self.chat["conta"]]})
        elif t == "result":
            u = d.get("usage") or {}
            self.msg["uso"] = {"entrada": u.get("input_tokens", 0), "saida": u.get("output_tokens", 0),
                               "cache_l": u.get("cache_read_input_tokens", 0),
                               "cache_e": u.get("cache_creation_input_tokens", 0),
                               "pensamento": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)}
            self.msg["custo"] = d.get("total_cost_usd", 0) or 0
            self.msg["turnos"] = d.get("num_turns", 0)
            mu = d.get("modelUsage") or {}
            if mu:
                principal = mu.get(self.msg["modelo"]) or max(mu.values(), key=lambda x: x.get("outputTokens", 0))
                self.msg["contexto"]["janela"] = principal.get("contextWindow")
                self.msg["modelos"] = {k: {"custo": v.get("costUSD"), "saida": v.get("outputTokens")} for k, v in mu.items()}
            if d.get("is_error"):
                if not self.parado:
                    self.msg["erro"] = str(d.get("result") or d.get("subtype") or "erro")
            elif not self.msg["texto"] and d.get("result"):
                self.msg["texto"] = d["result"]
            if d.get("permission_denials"):
                self.msg["negadas"] = [x.get("tool_name") for x in d["permission_denials"]]
            if d.get("session_id"):
                self.chat["sessao"] = d["session_id"]
            self.fecha_entrada()  # the turn is over: closing stdin lets the CLI exit

    # ---- stdin of a Claude run
    def escreve(self, d):
        with self.trava_in:
            ent = self.proc.stdin if self.proc else None
            if not ent or ent.closed:
                return False
            try:
                ent.write((json.dumps(d, ensure_ascii=False) + "\n").encode())
                ent.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def responde_ctl(self, rid, resposta):
        return self.escreve({"type": "control_response",
                             "response": {"subtype": "success", "request_id": rid, "response": resposta}})

    def fecha_entrada(self):
        with self.trava_in:
            ent = self.proc.stdin if self.proc else None
            if ent and not ent.closed:
                try:
                    ent.close()
                except OSError:
                    pass

    # ---- Antigravity CLI stream-json
    def parse_agy(self, d):
        ev = d.get("event")
        if ev == "init":
            self.chat["sessao"] = d.get("conversation_id", self.chat.get("sessao", ""))
            ini = d.get("init", {})
            self.msg["modelo"] = ini.get("model", "")
            self.emite({"t": "sistema", "modelo": self.msg["modelo"], "sessao": self.chat["sessao"],
                        "perm": ini.get("permission_mode")})
        elif ev == "step_update":
            s = d.get("step_update", {})
            tipo = s.get("step_type")
            if tipo == "agent_response":
                if s.get("thinking_delta"):
                    self.msg["pensamento"] += s["thinking_delta"]
                    self.emite({"t": "pensando", "s": s["thinking_delta"]})
                if s.get("text_delta"):
                    self.msg["texto"] += s["text_delta"]
                    self.emite({"t": "texto", "s": s["text_delta"]})
                u = s.get("usage")
                if u and s.get("state") == "DONE":
                    self.msg["contexto"]["usado"] = int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
                    self.emite({"t": "ctx", "usado": self.msg["contexto"]["usado"]})
                    if self.msg["texto"] and not self.msg["texto"].endswith("\n"):
                        self.msg["texto"] += "\n"
            elif tipo == "tool":
                fid = f"s{s.get('step_index')}"
                info = s.get("tool_info") or {}
                if fid not in self.ferr:
                    f = {"id": fid, "nome": s.get("tool_name") or info.get("name"),
                         "entrada": corta(info.get("parameters"), 3000), "saida": "", "erro": False, "fim": False}
                    self.ferr[fid] = f
                    self.msg["ferramentas"].append(f)
                    self.emite({"t": "ferramenta", **f})
                if s.get("state") in ("DONE", "ERROR", "FAILED", "CANCELED"):
                    f = self.ferr[fid]
                    f.update(saida=corta(info.get("output", ""), 6000), erro=s.get("state") != "DONE", fim=True)
                    self.emite({"t": "resultado", "id": fid, "saida": f["saida"], "erro": f["erro"]})
        elif ev == "result":
            r = d.get("result", {})
            u = r.get("usage") or {}
            self.msg["uso"] = {"entrada": u.get("input_tokens", 0), "saida": u.get("output_tokens", 0),
                               "cache_l": u.get("cache_read_tokens", 0), "cache_e": 0,
                               "pensamento": u.get("thinking_tokens", 0)}
            self.msg["turnos"] = r.get("num_turns", 0)
            if r.get("response"):
                self.msg["texto"] = r["response"]
            if r.get("status") not in (None, "SUCCESS"):
                self.msg["erro"] = str(r.get("error") or r.get("status"))
            if r.get("conversation_id"):
                self.chat["sessao"] = r["conversation_id"]

    def troca_automatica(self):
        """The reply stopped at the account's usage limit: move the chat to the Claude account with the most
        room left and continue there. Each account is tried once until you send a message yourself."""
        try:
            with trava:
                c = ler_chat(self.chat_id)
                de = c.get("conta", "")
                c["tentadas"] = list(dict.fromkeys((c.get("tentadas") or []) + [de]))
                gravar_chat(c)
            alvo = conta_livre(c)
            if not alvo:
                return False
            mover_conta(self.chat_id, alvo["id"], atual=self)
            with trava:
                c = ler_chat(self.chat_id)
                if c["mensagens"] and c["mensagens"][-1].get("papel") == "assistente":
                    c["mensagens"][-1]["troca"] = {"de": de, "para": alvo["id"]}
                gravar_chat(c)
            self.msg["troca"] = {"de": de, "para": alvo["id"]}
            self.emite({"t": "troca", "de": de, "para": alvo["id"]})
            print(f"chat {self.chat_id} hit the limit on {de}, continuing on {alvo['id']}", flush=True)
            enviar(self.chat_id, "Continue where you left off.", [], self, auto=True)
            return True
        except Exception as e:
            print(f"automatic account switch failed: {e!r}", flush=True)
            return False

    def parar(self):
        self.parado = True
        if self.proc and self.proc.poll() is None and self.escreve(
                {"type": "control_request", "request_id": "stop-" + secrets.token_hex(4), "request": {"subtype": "interrupt"}}):
            # a clean interrupt keeps the CLI session consistent, like Esc in the terminal
            fim = time.time() + 4
            while time.time() < fim and self.proc.poll() is None:
                time.sleep(0.1)
        if self.proc and self.proc.poll() is None:
            mata_arvore(self.proc)
            time.sleep(1.5)
            mata_arvore(self.proc, forte=True)

    def finalizar(self):
        PK_TOKENS.pop(self.pk, None)
        with PK_TRAVA:
            for k, p in list(PEDIDOS.items()):
                if p["ex"] is not self:
                    continue
                if p["pub"]["estado"] == "pendente":
                    if p.get("ctl"):
                        pk_muda(p, "cancelado")
                    else:
                        p["resp"] = {"r": "negar", "msg": "the reply ended before an answer"}
                        pk_muda(p, "cancelado")
                        p["ev"].set()
                if p.get("ctl"):
                    PEDIDOS.pop(k, None)
        self.msg["duracao"] = round(time.time() - self.inicio, 1)
        self.msg["texto"] = self.msg["texto"].strip()
        self.msg["effort"] = esforco_resposta(self.chat, self.msg.get("modelo") or self.chat.get("modelo", ""))
        if self.parado:
            self.msg["erro"] = (self.msg["erro"] + " " if self.msg["erro"] else "") + "(stopped)"
        with trava:
            try:
                atual = ler_chat(self.chat_id)
                if self.conv:
                    cv, sid = atual.get("convidado"), self.chat.get("sessao")
                    if sid and mesmo_convidado(cv, self.conv):
                        cv["sessao"] = sid
                    if sid:
                        atual["sids_conv"] = list(dict.fromkeys((atual.get("sids_conv") or []) + [sid]))[-50:]
                        cc = atual.get("contas_conv") or {}
                        cc[sid] = self.conv["conta"]
                        atual["contas_conv"] = {k: v for k, v in cc.items() if k in atual["sids_conv"]}
                else:
                    atual["sessao"] = self.chat.get("sessao", atual.get("sessao", ""))
                    if self.iniciou:
                        atual.pop("fork", None)  # the branch exists now, under its own session id
                atual["mensagens"].append(self.msg)
                db = atual.get("debate")
                if db and (self.parado or self.msg["erro"]):
                    atual.pop("debate", None)
                elif db and self.debate:
                    db["feitas"] = int(db.get("feitas") or 0) + 1
                    if db["feitas"] >= int(db.get("total") or 0):
                        atual.pop("debate", None)
                renomear = not self.conv and atual.pop("renomear_cli", False)
                nomear = atual.pop("nomear_depois", False) and not self.msg["erro"] and not self.parado
                gravar_chat(atual)
                existe = True
            except FileNotFoundError:
                renomear = nomear = existe = False
            self.gravado = True
        if renomear and atual.get("sessao") and not atual.get("fork") and conta_por_id(atual.get("conta", "")):
            try:
                renomear_sessao(conta_por_id(atual["conta"]), atual["sessao"], atual["titulo"])
            except Exception:
                pass
        if nomear:
            threading.Thread(target=nomear_chat, args=(self.chat_id,), daemon=True).start()
        if not self.parado and not self.msg["erro"]:
            try:
                # a message you queued goes first; the debate picks up after its reply
                self.segue = (self.seguir_duo and seguir_duo(self.chat_id, self)) or proxima_fila(self.chat_id, self) \
                    or seguir_debate(self.chat_id, self)
            except Exception as e:
                print(f"chat {self.chat_id}: next turn failed: {e!r}", flush=True)
        elif (existe and not self.parado and not self.conv and self.chat["agente"] == "claude" and CFG["padrao"].get("troca_auto")
              and LIMITE_RE.search(self.msg["erro"])):
            self.troca_automatica()
        with self.cond:
            self.acabou = True
            ATIVIDADE["t"] = time.time()
            self.eventos.append({"t": "fim", "msg": self.msg, "segue": bool(getattr(self, "segue", False))})
            self.cond.notify_all()

        def solta():
            if EXECS.get(self.chat_id) is self:
                EXECS.pop(self.chat_id, None)
        t = threading.Timer(180, solta)
        t.daemon = True
        t.start()


# ---------------------------------------------------------------- chat titles
URL_RE = re.compile(r"https?://\S+|www\.\S+")
TITULO_SP = ("You name chats. Reply with only a short title (2 to 6 words) for the conversation, in the language "
             "the user wrote in, with normal capitalization, no quotes, no emoji and no final period.")


def titulo_simples(texto):
    """The first message as a tidy title: links become their last path part, spaces collapse, the first letter
    is a capital and the cut falls between words."""
    t = str(texto or "")
    t = URL_RE.sub(lambda m: (re.sub(r"[?#].*", "", m.group(0)).rstrip("/").split("/")[-1] or m.group(0)), t)
    t = re.sub(r"\s+", " ", t).strip(" ,;").rstrip(" .:-")
    if not t:
        return ""
    if len(t) > 60:
        t = t[:60].rsplit(" ", 1)[0] if " " in t[:60] else t[:60]
        t = t.rstrip(" ,.;:-") + "…"
    primeira = t.split(" ", 1)[0]
    if primeira[:1].islower() and primeira.islower():   # "iPhone", ".env" and "x86_64" stay as typed
        t = t[0].upper() + t[1:]
    return t


def _limpa_titulo(t):
    t = (t or "").strip().splitlines()[0] if (t or "").strip() else ""
    t = re.sub(r"^(title|título|titulo)\s*:\s*", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t.strip().strip("\"'`*#").strip()).rstrip(".")
    return t[:80]


def _contas_titulo(c):
    # the chat's own Claude login first; Antigravity chats borrow a Claude login. Accounts whose limit
    # was reached are skipped.
    agora = time.time()
    def livre(x):
        inf = (LIMITES.get(x["id"]) or {}).get("info") or {}
        return not (inf.get("status") == "rejected" and (inf.get("resetsAt") or agora + 1) > agora)
    todas = [x for x in CFG["contas"] if x["agente"] == "claude"]
    primeiro = [x for x in todas if x["id"] == c.get("conta")]
    return [x for x in primeiro + [x for x in todas if x not in primeiro] if livre(x)]


def gerar_titulo(c):
    """A short title from the first exchange, written by Claude Haiku with no tools, no settings, no MCP and
    no saved session (about 800 tokens). None when no Claude login can do it."""
    msgs = c.get("mensagens") or []
    u = next((m for m in msgs if m.get("papel") == "usuario"), None)
    if not u:
        return None
    texto = (u.get("texto") or "").strip()
    if not texto and u.get("anexos"):
        texto = "(sent files: " + ", ".join(a.get("nome", "?") for a in u["anexos"][:5]) + ")"
    r = next((m for m in msgs if m.get("papel") == "assistente" and m.get("texto")), None)
    prompt = "Conversation start:\nUser: " + corta(texto, 1500)
    if r:
        prompt += "\nAssistant: " + corta(r["texto"], 800)
    for conta in _contas_titulo(c)[:2]:
        try:
            p = subprocess.run([CLAUDE, "-p", "--model", "haiku", "--no-session-persistence", "--tools", "",
                                "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
                                "--system-prompt", TITULO_SP, "--output-format", "json"],
                               input=prompt, capture_output=True, text=True, timeout=45, cwd=str(DATA),
                               env=env_conta(conta))
            d = json.loads(p.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
        t = _limpa_titulo(d.get("result")) if not d.get("is_error") else ""
        if t:
            return t
    return None


def nomear_chat(cid, condicao=lambda c: c.get("titulo_auto")):
    """Give the chat a title from Claude Haiku, if `condicao` still holds for the chat when the title arrives
    (by default: nobody renamed it by hand in the meantime)."""
    try:
        t = gerar_titulo(ler_chat(cid))
    except FileNotFoundError:
        return None
    if not t:
        return None
    with trava:
        try:
            c = ler_chat(cid)
        except FileNotFoundError:
            return None
        if not condicao(c):
            return None
        c["titulo"], c["titulo_auto"] = t, False
        ex = EXECS.get(cid)
        # finalizar() saves the reply under the same lock and sets ex.gravado there, so either it still
        # has to run (and will rename the CLI session) or it already ran and the rename happens here
        espera = bool(ex and not getattr(ex, "gravado", False))
        if espera:
            c["renomear_cli"] = True     # the CLI is writing its transcript: name it when the reply ends
        gravar_chat(c)
        # an unreplied fork still points at its parent's session, which keeps its own name
        sid = "" if c.get("fork") else c.get("sessao")
        conta = conta_por_id(c.get("conta", ""))
    if ex and not ex.acabou:
        ex.chat["titulo"] = t
        ex.emite({"t": "titulo", "titulo": t})
    if not espera and sid and conta:
        try:
            renomear_sessao(conta, sid, t)
        except Exception:
            pass
    return t


def titulo_cru(c):
    # chats named by the old rule: the first message cut at 60 characters, never renamed
    if c.get("titulo_auto") is False or c.get("origem") == "cli":
        return False
    u = next((m for m in c.get("mensagens") or [] if m.get("papel") == "usuario"), None)
    return bool(u) and c.get("titulo") == re.sub(r"\s+", " ", (u.get("texto") or "").strip())[:60]


NOMEANDO = {"rodando": False, "feitos": 0, "tentados": 0, "total": 0}


def _sem_nome(c):
    return bool(c.get("titulo_auto") or titulo_cru(c))


def nomear_antigos():
    try:
        alvos = []
        for q in sorted(CHATS.glob("*.json"), key=lambda q: -q.stat().st_mtime):
            try:
                c = json.loads(q.read_text())
            except Exception:
                continue
            if c.get("mensagens") and _sem_nome(c):
                alvos.append(c["id"])
        alvos = alvos[:100]
        NOMEANDO.update(feitos=0, tentados=0, total=len(alvos))
        for cid in alvos:
            if nomear_chat(cid, condicao=_sem_nome):
                NOMEANDO["feitos"] += 1
            NOMEANDO["tentados"] += 1
    finally:
        NOMEANDO["rodando"] = False


def limpar_vazios(idade=7 * 86400):
    """Chats opened with "New chat" and left without a message, without changing any setting, are removed
    after a week."""
    fixos = set(CFG.get("fixados") or [])
    for q in CHATS.glob("*.json"):
        try:
            c = json.loads(q.read_text())
            quando = max(c.get("atualizado") or 0, c.get("criado") or 0)
            if (not c.get("mensagens") and not c.get("sessao") and not c.get("fila") and not c.get("ajustado")
                    and c["id"] not in fixos and c["id"] not in EXECS and quando
                    and time.time() - quando > idade):
                q.unlink()
        except Exception:
            pass


def enviar(cid, texto, anexos_ids, substitui=None, auto=False, oculta=False, debate=False):
    with trava:
        if cid in EXECS and not EXECS[cid].acabou and EXECS[cid] is not substitui:
            raise ValueError("this chat is already running")
        chat = ler_chat(cid)
        if not conta_por_id(chat.get("conta", "")):
            raise ValueError("the account of this chat was removed. Pick another account in the header"
                             if not chat.get("sessao") else
                             "the account of this chat was removed. Add it again in Accounts, or start a new chat")
        if chat["agente"] == "claude" and chat.get("sessao") and conta_por_id(chat["conta"]):
            if chat["sessao"] in ativas_claude(conta_por_id(chat["conta"])):
                raise ValueError("this conversation is open in a terminal right now. Close it there first (or start a new chat)")
        anexos = []
        for aid in anexos_ids or []:
            a = ler_anexo(aid)
            if a:
                anexos.append(a)
        msg = {"papel": "usuario", "texto": texto, "anexos": anexos, "ts": time.time()}
        if auto:
            msg["auto"] = True
        elif not oculta:
            chat.pop("tentadas", None)   # a message of yours: accounts at their limit may be tried again
        if not oculta:
            chat["mensagens"].append(msg)
        nomear = False
        if not oculta and chat["titulo"] in ("New chat", "Novo chat") and (texto.strip() or anexos):
            chat["titulo"] = titulo_simples(texto) or (anexos[0].get("nome") or "New chat")[:60]
            chat["titulo_auto"] = True
            if CFG["padrao"].get("titulos", True):
                # "hi" or a bare attachment says little: wait for the reply to know the topic
                nomear = len(texto.strip()) >= 15
                chat["nomear_depois"] = not nomear
        gravar_chat(chat)
        ex = Execucao(chat, texto, anexos, oculta=oculta, debate=debate)
        EXECS[cid] = ex
    threading.Thread(target=ex.rodar, daemon=True).start()
    if nomear:
        threading.Thread(target=nomear_chat, args=(cid,), daemon=True).start()
    return ex


def _ponto_de_volta(c, i):
    """The reply a branch continues from: the assistant message right before message i."""
    if c["agente"] != "claude":
        raise ValueError("editing and forking work with Claude chats only")
    # replies of another AI (and your messages to it) live in its own session: branch at the last
    # reply of the chat's own AI; the guest's part is passed along again in the next message
    i -= 1
    while i >= 0 and (c["mensagens"][i].get("convidado") or c["mensagens"][i].get("para")):
        i -= 1
    if i < 0:
        return None
    ant = c["mensagens"][i]
    if ant.get("papel") != "assistente" or not ant.get("uuid") or not c.get("sessao"):
        raise ValueError("this part of the conversation is from before Agents Web 1.4, so it cannot be branched")
    return ant["uuid"]


def reenviar(cid, i, texto):
    """Edit a message of yours (or send it again) and continue from there. The CLI session forks, so
    the original stays intact in Claude Code; the web chat drops what came after."""
    with trava:
        ex = EXECS.get(cid)
        if ex and not ex.acabou:
            raise ValueError("stop the current reply first")
        c = ler_chat(cid)
        try:
            i = int(i)
        except (TypeError, ValueError):
            raise ValueError("message not found")
        if not 0 <= i < len(c["mensagens"]) or c["mensagens"][i].get("papel") != "usuario":
            raise ValueError("message not found")
        if c["mensagens"][i].get("para"):
            raise ValueError("call the other AI again to ask it something else")
        conta = conta_por_id(c.get("conta", ""))
        if not conta:
            raise ValueError("the account of this chat was removed. Add it again in Accounts, or start a new chat")
        if c.get("sessao") and c["sessao"] in ativas_claude(conta):
            raise ValueError("this conversation is open in a terminal right now. Close it there first")
        texto = c["mensagens"][i]["texto"] if texto is None else str(texto)
        anexos = [a.get("id") for a in (c["mensagens"][i].get("anexos") or []) if a.get("id")]
        if not texto.strip() and not anexos:
            raise ValueError("empty message")
        em = _ponto_de_volta(c, i)
        if c.get("sessao"):
            c["antigas"] = list(dict.fromkeys((c.get("antigas") or []) + [c["sessao"]]))[-100:]
        if em:
            c["fork"] = {"em": em}
        else:
            c.pop("fork", None)
            c["sessao"] = ""
        if c.get("convidado") and any(m.get("convidado") or m.get("para") for m in c["mensagens"][i:]):
            c["convidado"]["sessao"] = ""
        c["mensagens"] = c["mensagens"][:i]
        c["fila"] = []
        c.pop("debate", None)
        gravar_chat(c)
        enviar(cid, texto, anexos)


def bifurcar(cid, i):
    """A new chat with the conversation up to reply i; its first message branches the CLI session there."""
    c = ler_chat(cid)
    try:
        i = int(i)
    except (TypeError, ValueError):
        raise ValueError("message not found")
    if not 0 <= i < len(c["mensagens"]) or c["mensagens"][i].get("papel") != "assistente":
        raise ValueError("pick a reply to fork from")
    em = _ponto_de_volta(c, i + 1)
    n = novo_chat({**{k: c[k] for k in ("agente", "conta", "modelo", "perm", "effort", "cwd")},
                   "titulo": ("Fork of " + c["titulo"])[:120], "sessao": c.get("sessao", "")})
    with trava:
        n = ler_chat(n["id"])
        n["mensagens"] = json.loads(json.dumps(c["mensagens"][:i + 1]))
        n["fork"] = {"em": em}
        gravar_chat(n)
    return n


def _contem(longo, curto):
    """True when file `longo` starts with the bytes of `curto` (or both are the same link). Transcripts only
    grow, so this tells which of two copies of a session is the more recent one."""
    if longo.is_symlink() or curto.is_symlink():
        return longo.is_symlink() and curto.is_symlink() and os.readlink(longo) == os.readlink(curto)
    if curto.stat().st_size > longo.stat().st_size:
        return False
    with open(longo, "rb") as a, open(curto, "rb") as b:
        while True:
            x = b.read(1 << 20)
            if not x:
                return True
            if a.read(len(x)) != x:
                return False


def _arquivos_de(org, dst):
    """(source, destination) for a file, or for every file under a folder (links are kept as links, and empty
    folders are listed too, so they get made on the other side)."""
    if org.is_symlink() or org.is_file():
        return [(org, dst)]
    out = []
    for raiz, pastas, arqs in os.walk(org):
        r = Path(raiz)
        for n in arqs + [p for p in pastas if (r / p).is_symlink()]:
            out.append((r / n, dst / (r / n).relative_to(org)))
        if not arqs and not pastas:
            out.append((r, dst / r.relative_to(org)))
    return out


def _cria_pasta(p, desfaz):
    """mkdir -p that registers how to remove the folders it made, deepest first."""
    novas = []
    while not p.exists():
        novas.append(p)
        p = p.parent
    if novas:
        novas[0].mkdir(parents=True)
    for n in reversed(novas):
        desfaz.append(lambda n=n: n.rmdir() if n.is_dir() and not any(n.iterdir()) else None)


def _transfere(itens):
    """Move or copy files as one step: either all of them reach their destination or, on any error, every
    file is put back the way it was. Returns (undo, cleanup): undo puts everything back (for a failure right
    after), cleanup lists files to delete once the move is committed. Uses rename on the same disk, so even
    big sessions move instantly. A destination that already holds a longer copy of the same transcript is
    kept; a shorter one is replaced; a different one is replaced and kept next to it as .bak."""
    desfaz, depois = [], []

    def volta():
        for f in reversed(desfaz):
            try:
                f()
            except OSError as e:
                print(f"undo step failed: {e!r}", flush=True)

    try:
        for org, dst, copiar in itens:
            if org.is_dir() and not org.is_symlink():   # an empty folder
                _cria_pasta(dst, desfaz)
                continue
            if dst.exists() or dst.is_symlink():
                if _contem(dst, org):
                    if not copiar:
                        depois.append(org)
                    continue
                diverge = not _contem(org, dst)
                guarda = dst.with_name(f"{dst.name}.aw-{int(time.time())}.bak" if diverge
                                       else f".{dst.name}.aw-{secrets.token_hex(4)}")
                os.rename(dst, guarda)

                def repoe(d=dst, g=guarda):
                    if d.exists() or d.is_symlink():
                        d.unlink()
                    os.rename(g, d)
                desfaz.append(repoe)
                if diverge:
                    print(f"kept the other copy of {dst.name} as {guarda.name}", flush=True)
                else:
                    depois.append(guarda)
            _cria_pasta(dst.parent, desfaz)
            if not copiar:
                try:
                    os.rename(org, dst)
                    desfaz.append(lambda o=org, d=dst: os.rename(d, o))
                    continue
                except OSError as e:
                    if e.errno != errno.EXDEV:
                        raise
                depois.append(org)
            desfaz.append(lambda d=dst: (d.exists() or d.is_symlink()) and d.unlink())
            shutil.copy2(org, dst, follow_symlinks=False)
    except BaseException:
        volta()
        raise
    return volta, depois


def _esforco_de(conta, modelo):
    """The effort level an account's own settings.json uses for a model, if any."""
    try:
        d = json.loads((dir_claude(conta) / "settings.json").read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(d, dict):
        return ""
    ms = d.get("modelSettings") if isinstance(d.get("modelSettings"), dict) else {}
    m = ms.get(modelo) or ms.get(re.sub(r"\[.*\]$", "", modelo or ""))
    e = (m.get("effortLevel") if isinstance(m, dict) else None) or d.get("effortLevel")
    return e if isinstance(e, str) and e in ESFORCOS else ""


def mover_conta(cid, destino, atual=None):
    """Continue a Claude chat on another Claude account. The CLI keeps a session in the config folder of the
    account that ran it, so its transcript (and the ones of earlier edits) moves along to the new folder,
    where `--resume` finds it. A session another web chat still uses there is copied instead of moved.
    All or nothing: if any file cannot move, everything stays on the old account. A chat on the account's
    default model and effort keeps the ones it was really using, since the other account may default to others."""
    with trava:
        ex = EXECS.get(cid)
        if ex and not ex.acabou and ex is not atual:
            raise ValueError("stop the current reply first")
        c = ler_chat(cid)
        if c["agente"] != "claude":
            raise ValueError("only Claude chats can change account")
        de, para = conta_por_id(c.get("conta", "")), conta_por_id(destino or "")
        if not para or para["agente"] != "claude":
            raise ValueError("account not found")
        if de and de["id"] == para["id"]:
            return c
        sids = [x for x in dict.fromkeys([c.get("sessao") or ""] + list(c.get("antigas") or []))
                if SID_RE.fullmatch(x or "")]
        if de and sids:
            if dir_claude(de).resolve() == dir_claude(para).resolve():
                sids = []          # two names for the same login folder: nothing to move
            elif c.get("sessao") in ativas_claude(de) or c.get("sessao") in ativas_claude(para):
                raise ValueError("this conversation is open in a terminal right now. Close it there first")
        outros = set()
        if de and sids:
            for q in CHATS.glob("*.json"):
                if q.stem == cid:
                    continue
                try:
                    o = json.loads(q.read_text())
                except Exception:
                    continue
                if o.get("conta") == de["id"]:
                    outros.update([o.get("sessao") or ""] + list(o.get("antigas") or []))
        itens, pastas = [], []
        if de and sids:
            a, b = dir_claude(de), dir_claude(para)
            for sid in sids:
                raizes = [(x, b / "projects" / x.parent.name / x.name)
                          for x in list((a / "projects").glob(f"*/{sid}.jsonl")) + list((a / "projects").glob(f"*/{sid}"))]
                raizes += [(a / d / sid, b / d / sid) for d in ("file-history", "session-env")]
                for org, dst in raizes:
                    if not org.exists() or org.is_symlink():
                        continue
                    if org.is_dir() and sid not in outros:
                        pastas.append(org)
                    itens += [(x, y, sid in outros) for x, y in _arquivos_de(org, dst)]
        try:
            volta, depois = _transfere(itens)
        except OSError as e:
            raise ValueError(f"could not move the conversation files ({e.strerror or e}). Nothing changed, "
                             f"the chat is still on {(de or {}).get('nome') or 'its account'}")
        fixou = {}
        try:
            if not c.get("modelo"):
                m = next((x.get("modelo") for x in reversed(c["mensagens"])
                          if x.get("papel") == "assistente" and x.get("modelo")), "")
                if m:
                    c["modelo"] = fixou["modelo"] = m
            if not c.get("effort") and de and c.get("modelo"):
                e = _esforco_de(de, c["modelo"])
                if e:
                    c["effort"] = fixou["effort"] = e
            c["conta"] = para["id"]
            gravar_chat(c)
        except BaseException:
            volta()
            raise
        for p in depois:
            try:
                _remove(p)
            except OSError as e:
                print(f"could not remove {p}: {e!r}", flush=True)
        for p in pastas:   # folders emptied by the move
            for raiz, _, _ in sorted(os.walk(p, topdown=False), key=lambda t: -len(t[0])):
                try:
                    os.rmdir(raiz)
                except OSError:
                    pass
        print(f"chat {cid} moved to account {para['id']} ({len(itens)} session files)", flush=True)
        return {**c, "fixado": fixou} if fixou else c


LIMITE_RE = re.compile(r"hit your .*limit|usage limit|out of (extra )?usage", re.I)


def _bloqueada(cid):
    """The last reply on this account was refused for its limit, and that limit has not reset yet."""
    l = LIMITES.get(cid) or {}
    inf = l.get("info") or {}
    if inf.get("status") != "rejected":
        return False
    return (inf["resetsAt"] > time.time()) if inf.get("resetsAt") else (l.get("quando") or 0) > time.time() - 5 * 3600


def conta_livre(chat):
    """For a chat that hit its limit: the other Claude account with the most room left, skipping the ones this
    chat already hit the limit on since your last message. None when there is no such account."""
    atual = conta_por_id(chat.get("conta", ""))
    tentadas = set(chat.get("tentadas") or [])
    contas = []
    for c in CFG["contas"]:
        if c["agente"] != "claude" or c["id"] == chat.get("conta") or c["id"] in tentadas or _bloqueada(c["id"]):
            continue
        if atual and dir_claude(c).resolve() == dir_claude(atual).resolve():
            continue
        contas.append(c)
    if not contas:
        return None
    # one usage lookup per account, side by side, so a slow endpoint costs one timeout and not one per account
    res = {}

    def busca(c):
        try:
            res[c["id"]] = _em_cache("lim:" + c["id"], 90, False, lambda: limites_claude(c)) or {}
        except Exception:
            res[c["id"]] = {}
    fios = [threading.Thread(target=busca, args=(c,), daemon=True) for c in contas]
    for f in fios:
        f.start()
    fim = time.time() + 20
    for f in fios:
        f.join(max(0, fim - time.time()))
    cands = []
    for c in contas:
        lim = res.get(c["id"]) or {}
        if lim.get("erro") == "Not signed in" and not lim.get("linhas"):
            continue
        usos = [0 if (l.get("reinicia") or 0) and l["reinicia"] < time.time() else (l.get("usado") or 0)
                for l in lim.get("linhas") or [] if l.get("janela") == "5h" or "All models" in (l.get("nome") or "")]
        if usos and max(usos) >= 100:
            continue
        cands.append((max(usos) if usos else 50, len(cands), c))
    return min(cands)[2] if cands else None


def proxima_fila(cid, atual):
    # called by the run that ends, before the "fim" event, so whoever is watching reopens the
    # chat with the next queued message already running
    with trava:
        try:
            c = ler_chat(cid)
        except (FileNotFoundError, ValueError):
            return False
        f = c.get("fila") or []
        if not f:
            return False
        item = f.pop(0)
        c["fila"] = f
        gravar_chat(c)
        try:
            enviar(cid, item.get("texto", ""), item.get("anexos"), atual)
            return True
        except Exception:
            c = ler_chat(cid)
            c.setdefault("fila", []).insert(0, item)
            gravar_chat(c)
            return False



# ---------------------------------------------------------------- Call another AI
# A second AI joins the chat: it reads the conversation so far (or only what it has not seen yet, in
# its own CLI session), replies in the same chat, and the chat's own AI gets that reply with your next
# message. A debate goes back and forth by itself for a set number of replies.
NOME_AG = {"claude": "Claude", "agy": "Antigravity"}
CONV_MAX = 60_000     # characters of conversation passed along in one go (the newest part wins)
CONV_MSG = 8_000      # characters of one message
DEBATE_MAX = 10       # rounds (one reply from each AI)
ESFORCOS = ("low", "medium", "high", "xhigh", "max")


def mesmo_convidado(a, b):
    return bool(a and b) and a.get("agente") == b.get("agente") and a.get("conta") == b.get("conta")


def _ultimo(msgs, cond):
    for i in range(len(msgs) - 1, -1, -1):
        if cond(msgs[i]):
            return i
    return -1


def nomes_ias(c, conv):
    """How the two AIs call each other in the prompts (and the guest's name on its replies)."""
    h, g = NOME_AG[c["agente"]], NOME_AG[conv["agente"]]
    if h == g:
        ch, cg = conta_por_id(c.get("conta", "")), conta_por_id(conv["conta"])
        nh, ng = (ch or {}).get("nome") or c.get("conta"), (cg or {}).get("nome") or conv["conta"]
        if c.get("conta") != conv.get("conta") and nh != ng:
            h, g = f"{h} ({nh})", f"{g} ({ng})"
        else:
            g = f"{g} ({conv['modelo']})" if conv.get("modelo") and conv["modelo"] != c.get("modelo") else f"{g} (guest)"
    return h, g


def transcricao(msgs, anfitriao):
    partes = []
    for m in msgs:
        t = (m.get("texto") or "").strip()
        if m.get("papel") == "usuario":
            autor = f"User (to {m['para']})" if m.get("para") else "User"
        else:
            autor = (m.get("convidado") or {}).get("nome") or anfitriao
            if not t and m.get("erro"):
                continue   # a reply that failed says nothing to the other AI
            if not t:
                t = "(no text in this reply)"
        if len(t) > CONV_MSG:
            t = t[:CONV_MSG] + "\n[... the rest of this message was left out]"
        ax = [a.get("caminho", "") for a in (m.get("anexos") or [])]
        if ax:
            t += "\n[Files attached, saved on this PC: " + ", ".join(ax) + "]"
        partes.append(f"### {autor}\n{t}")
    out, total = [], 0
    for x in reversed(partes):
        if out and total + len(x) > CONV_MAX:
            out.append("[earlier messages left out]")
            break
        out.append(x)
        total += len(x)
    return "\n\n".join(reversed(out))


def nota_anfitriao(c, oculta):
    """What the chat's own AI missed: the other AI's replies (and your messages to it) since its last reply
    that went through (a reply that failed or was stopped may never have reached its session)."""
    msgs = c["mensagens"] if oculta else c["mensagens"][:-1]
    i = _ultimo(msgs, lambda m: m.get("papel") == "assistente" and not m.get("convidado") and not m.get("erro"))
    novas = [m for m in msgs[i + 1:] if m.get("convidado") or m.get("para")]
    cv = c.get("convidado") if (c.get("convidado") or {}).get("agente") in NOME_AG else None
    eu, g = nomes_ias(c, cv) if cv else (NOME_AG[c["agente"]], "")
    equipe = ""
    if c.get("duo") and cv and not oculta:
        equipe = (f"[Agents Web: {g} is in this chat with you as a team. It answers each message of the user right "
                  f"after you, with your reply in front of it, so cover the main answer and leave room for it.]")
    if not novas:
        return equipe
    nomes = list(dict.fromkeys(m["convidado"].get("nome") or "another AI" for m in novas if m.get("convidado")))
    quem = " and ".join(nomes) or "another AI"
    return ((equipe + "\n\n" if equipe else "") +
            f"[Agents Web: the user called {quem} into this chat as a second AI. What was said there since "
            f"your last reply:]\n\n<conversation>\n{transcricao(novas, eu)}\n</conversation>")


EQUIPE = ("[You and {h} are a team in this chat. Each message of the user goes to both of you: {h} answers first "
          "and you answer right after, with its reply in front of you. Work together: build on {h}'s reply, fix what "
          "is wrong, add what is missing or do the part it left, and do not repeat what it already said. If it "
          "already covered everything, say so in a line or two. Reply in the user's language.]")


def texto_convidado(c, conv, pedido, db, duo=False):
    h, g = nomes_ias(c, conv)
    msgs = c["mensagens"][:-1] if pedido else c["mensagens"]
    novo = not conv.get("sessao")
    i = -1 if novo else _ultimo(msgs, lambda m: mesmo_convidado(m.get("convidado"), conv))
    conversa = transcricao(msgs[i + 1:], h)
    if novo:
        t = (f"[Agents Web: the user is chatting with {h} in a web interface on their PC and called you, {g}, "
             f"into the conversation as a second AI. {h} reads what you write. Your tools work on this PC as "
             f"usual. The conversation so far:]\n\n<conversation>\n{conversa or '(nothing yet)'}\n</conversation>")
    else:
        t = (f"[Agents Web: new messages in the chat since your last reply:]\n\n<conversation>\n{conversa}\n"
             "</conversation>") if conversa else ""
    if db:
        t += (f"\n\n[A back and forth between you and {h} with the user watching, reply "
              f"{int(db.get('feitas') or 0) + 1} of {db.get('total')}. Talk to {h} directly, answer its points, "
              "say where you disagree instead of agreeing to be polite, and keep it short.]")
    equipe = duo and not db
    if equipe:
        t += "\n\n" + EQUIPE.format(h=h)
    if pedido:
        t += f"\n\nThe user asks you:\n{pedido}"
    elif not equipe and (not db or novo):
        t += (f"\n\nGive your own view on this conversation, mainly on {h}'s last reply: what you agree with, "
              "what you would do differently and what is missing. Reply in the user's language.")
    return t.strip()


def checa_convidado(conv):
    if not conv or not conta_por_id(conv.get("conta", "")):
        raise ValueError("the account of the other AI was removed. Call it again")
    if conv["agente"] == "claude" and conv.get("sessao") and conv["sessao"] in ativas_claude(conta_por_id(conv["conta"])):
        raise ValueError("the other AI's conversation is open in a terminal right now. Close it there first")


def rodar_convidado(cid, pedido="", substitui=None, debate=False, duo=False):
    with trava:
        ex = EXECS.get(cid)
        if ex and not ex.acabou and ex is not substitui:
            raise ValueError("wait for the current reply to finish")
        c = ler_chat(cid)
        conv = c.get("convidado")
        if not conv or not conta_por_id(conv.get("conta", "")):
            raise ValueError("the account of the other AI was removed. Call it again")
        if conv.get("sessao") and _ultimo(c["mensagens"], lambda m: mesmo_convidado(m.get("convidado"), conv)) < 0:
            conv["sessao"] = ""   # its replies were edited away: it starts again with the whole chat
        checa_convidado(conv)
        h, g = nomes_ias(c, conv)
        conv["nome"] = g
        if pedido:
            c["mensagens"].append({"papel": "usuario", "texto": pedido, "anexos": [], "ts": time.time(), "para": g})
            if c["titulo"] in ("New chat", "Novo chat"):
                c["titulo"] = titulo_simples(pedido) or "New chat"
                c["titulo_auto"] = True
                if CFG["padrao"].get("titulos", True):
                    c["nomear_depois"] = True
        gravar_chat(c)
        texto = texto_convidado(c, conv, pedido, c.get("debate") if debate else None, duo)
        ex = Execucao(c, texto, [], convidado={**conv, "nome": g}, debate=debate, duo=duo and not debate)
        EXECS[cid] = ex
    threading.Thread(target=ex.rodar, daemon=True).start()
    return ex


PERM_PAR = {   # the chat's permission mode -> the same strictness on the other CLI
    ("claude", "agy"): {"bypassPermissions": "skip", "acceptEdits": "accept-edits", "auto": "auto", "plan": "plan"},
    ("agy", "claude"): {"skip": "bypassPermissions", "accept-edits": "acceptEdits", "auto": "auto", "plan": "plan",
                        "": "manual"},
}


def perm_convidado(c, agente):
    if agente == c["agente"]:
        return c.get("perm", "")
    # modes without a match on the other CLI (ask before each action, don't ask) become plan only
    return PERM_PAR[(c["agente"], agente)].get(c.get("perm", ""), "plan")


def convidar(cid, d):
    agente = d.get("agente")
    if agente not in NOME_AG:
        raise ValueError("pick the AI to call")
    if d.get("conta") and not conta_por_id(str(d["conta"])):
        raise ValueError("that account was removed. Pick another one")
    conta = conta_por_id(str(d.get("conta") or "")) or next((x for x in CFG["contas"] if x["agente"] == agente), None)
    if not conta or conta["agente"] != agente:
        raise ValueError("this account does not belong to the selected AI")
    pedido = str(d.get("texto") or "").strip()[:200_000]
    try:
        rodadas = max(0, min(DEBATE_MAX, int(d.get("rodadas") or 0)))
    except (TypeError, ValueError):
        rodadas = 0
    modo = d.get("modo") if d.get("modo") in ("junto", "uma", "debate") else ("debate" if rodadas else "uma")
    if modo != "debate":
        rodadas = 0
    elif not rodadas:
        rodadas = 3
    with trava:
        ex = EXECS.get(cid)
        if ex and not ex.acabou:
            raise ValueError("wait for the current reply to finish")
        c = ler_chat(cid)
        if not c["mensagens"] and not pedido and modo != "junto":
            raise ValueError("the chat is empty. Tell the other AI what to do")
        conv = c.get("convidado") or {}
        if not mesmo_convidado(conv, {"agente": agente, "conta": conta["id"]}):
            conv = {"agente": agente, "conta": conta["id"], "sessao": ""}
            if modo != "junto":
                c["duo"] = False   # the duo partner was replaced by a one-off guest
        conv["modelo"] = str(d.get("modelo") or "")[:200]
        conv["effort"] = d.get("effort") if agente == "claude" and d.get("effort") in ESFORCOS else ""
        perm = perm_convidado(c, agente)
        if conv.get("perm") != perm:
            conv["permitidos"] = []
        conv["perm"] = perm
        c["convidado"] = conv
        if rodadas:
            c["debate"] = {"total": rodadas * 2, "feitas": 0}
        else:
            c.pop("debate", None)
        if modo == "junto":
            c["duo"] = True
        c["ajustado"] = True
        if modo == "junto" and not pedido:
            checa_convidado(conv)
            conv["nome"] = nomes_ias(c, conv)[1]
            gravar_chat(c)
            return None   # joined: it answers from your next message on
        gravar_chat(c)
        # trava is reentrant: nothing starts in between
        return rodar_convidado(cid, pedido, debate=bool(rodadas), duo=modo == "junto")


def esforco_resposta(chat, modelo):
    """The effort a reply ran with: the one picked in the chat, else the account's own default for that model
    (Claude); Antigravity model ids end with it."""
    if chat.get("agente") == "agy":
        for x in (modelo, chat.get("modelo")):
            m = re.search(r"-(" + "|".join(ESFORCOS) + r")$", x or "")
            if m:
                return m.group(1)
        return ""
    if chat.get("effort") in ESFORCOS:
        return chat["effort"]
    conta = conta_por_id(chat.get("conta", ""))
    return _esforco_de(conta, modelo) if conta else ""


def seguir_duo(cid, atual):
    """In a duo chat, the other AI answers your message right after the chat's own AI."""
    with trava:
        try:
            c = ler_chat(cid)
        except (FileNotFoundError, ValueError):
            return False
        if not c.get("duo") or not c.get("convidado") or c.get("debate"):
            return False
    try:
        rodar_convidado(cid, "", atual, duo=True)
        return True
    except Exception as e:
        # it cannot answer: say so in the chat and leave the duo, so your queue goes on
        with trava:
            c = ler_chat(cid)
            cv = c.get("convidado") or {}
            c["mensagens"].append({"papel": "assistente", "texto": "", "ferramentas": [], "uso": {}, "ts": time.time(),
                                   "convidado": {"agente": cv.get("agente", ""), "conta": cv.get("conta", ""),
                                                 "nome": cv.get("nome", "")},
                                   "erro": f"could not answer: {e}. It left the chat, Call another AI brings it back"})
            c["duo"] = False
            gravar_chat(c)
        return False


def config_convidado(cid, d):
    """Change the other AI's model or effort, or take it out of the chat (its session is kept for later)."""
    with trava:
        c = ler_chat(cid)
        conv = c.get("convidado")
        if not conv:
            raise ValueError("there is no other AI in this chat")
        ex = EXECS.get(cid)
        if ("modelo" in d or "effort" in d) and ex and not ex.acabou and ex.conv:
            raise ValueError("wait for its reply to finish")
        if d.get("sair"):
            c["duo"] = False   # False, not removed: the page merges the answer into the chat it has
        if "modelo" in d:
            conv["modelo"] = str(d.get("modelo") or "")[:200]
            conv["nome"] = nomes_ias(c, conv)[1]
        if "effort" in d:
            conv["effort"] = d["effort"] if conv["agente"] == "claude" and d["effort"] in ESFORCOS else ""
        gravar_chat(c)
        return c


def seguir_debate(cid, atual):
    """The next debate turn, run by the reply that just ended (before its "fim" event, like the queue)."""
    with trava:
        try:
            c = ler_chat(cid)
        except (FileNotFoundError, ValueError):
            return False
        if not c.get("debate") or not c.get("convidado") or not c["mensagens"]:
            return False
        vez_do_convidado = not c["mensagens"][-1].get("convidado")
        if not vez_do_convidado:
            _, g = nomes_ias(c, c["convidado"])
    try:
        if vez_do_convidado:
            rodar_convidado(cid, "", atual, debate=True)
            return True
        enviar(cid, f"[A back and forth between you and {g} with the user watching. Talk to {g} directly, answer "
                    "its points, say where you disagree instead of agreeing to be polite, and keep it short. "
                    "Reply in the user's language.]", [], atual, oculta=True, debate=True)
        return True
    except Exception:
        with trava:
            c = ler_chat(cid)
            c.pop("debate", None)
            gravar_chat(c)
        raise


# ---------------------------------------------------------------- attachments
def ler_anexo(aid):
    if not re.fullmatch(r"[a-f0-9]{16}", aid or ""):
        return None
    meta = UPLOADS / (aid + ".json")
    if not meta.exists():
        return None
    return json.loads(meta.read_text())


def salvar_upload(nome, tamanho, leitor):
    nome = re.sub(r"[^\w.\- ()\[\]]+", "_", Path(nome or "arquivo").name)[:120] or "arquivo"
    aid = secrets.token_hex(8)
    pasta = UPLOADS / time.strftime("%Y-%m-%d")
    pasta.mkdir(exist_ok=True)
    destino = pasta / f"{aid[:6]}-{nome}"
    resta = tamanho
    with open(destino, "wb") as f:
        while resta > 0:
            b = leitor.read(min(1 << 20, resta))
            if not b:
                break
            f.write(b)
            resta -= len(b)
    if resta > 0:
        destino.unlink(missing_ok=True)
        raise ValueError("upload interrupted")
    tipo = mimetypes.guess_type(nome)[0] or "application/octet-stream"
    a = {"id": aid, "nome": nome, "caminho": str(destino), "tam": tamanho - resta, "tipo": tipo, "ts": time.time()}
    salvar_json(UPLOADS / (aid + ".json"), a)
    return a


# ---------------------------------------------------------------- accounts
_status_contas = {}


def status_conta(conta, forcar=False):
    c = _status_contas.get(conta["id"])
    if c and not forcar and time.time() - c["t"] < 300:
        return c["s"]
    s = {"logado": None}
    try:
        if conta["agente"] == "claude":
            r = subprocess.run([CLAUDE, "auth", "status", "--json"], env=env_conta(conta), capture_output=True,
                               text=True, timeout=20, cwd=str(HOME))
            j = json.loads(r.stdout or "{}")
            s = {"logado": bool(j.get("loggedIn")), "email": j.get("email", ""), "plano": j.get("subscriptionType", ""),
                 "expira": j.get("loginExpiresAt", "")}
        else:
            r = subprocess.run([AGY, "models"], env=env_conta(conta), capture_output=True, text=True, timeout=30,
                               cwd=str(HOME), stdin=subprocess.DEVNULL)
            mods = [l.split("\t") for l in r.stdout.splitlines() if "\t" in l]
            s = {"logado": r.returncode == 0 and bool(mods), "modelos": [{"id": m[0], "nome": m[1]} for m in mods],
                 "email": "", "plano": ""}
    except Exception as e:
        s = {"logado": None, "erro": repr(e)}
    _status_contas[conta["id"]] = {"t": time.time(), "s": s}
    return s


def abrir_login(conta):
    env = env_conta(conta)
    alvo = [CLAUDE, "auth", "login"] if conta["agente"] == "claude" else [AGY]
    if WIN:
        # a console window of its own that stays open when the sign in ends. One string: cmd.exe /s /k drops
        # the outer quotes and keeps the inner ones, which the \" of a list would break (a path with a space)
        subprocess.Popen(f'cmd.exe /s /k "{subprocess.list2cmdline(alvo)}"', env=env, cwd=str(HOME),
                         creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        return
    script = shlex.join(alvo) + '; echo; read -r -p "Press Enter to close this window. " _'
    for term, pre, junto in TERMINAIS:
        if shutil.which(term):
            cmd = ["bash", "-c", script]
            subprocess.Popen([term] + pre + ([shlex.join(cmd)] if junto else cmd), env=env, cwd=str(HOME),
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    extra = f"CLAUDE_CONFIG_DIR={shlex.quote(conta['dir'])} " if conta["agente"] == "claude" and conta.get("dir") else ""
    raise ValueError(f"no terminal emulator found, run this in a terminal: {extra}{shlex.join(alvo)}")


# terminal emulators tried for sign in, in order ($TERMINAL first): flags that run a command, and
# whether that command must be passed as one string (ptyxis -x, GNOME Console -e)
TERMINAIS = ([(os.environ["TERMINAL"], ["-e"], False)] if os.environ.get("TERMINAL") else []) + [
    ("konsole", ["-e"], False), ("ptyxis", ["-x"], True), ("gnome-terminal", ["--"], False), ("kgx", ["-e"], True),
    ("xfce4-terminal", ["-x"], False), ("kitty", [], False), ("alacritty", ["-e"], False), ("foot", [], False),
    ("wezterm", ["start", "--"], False), ("xterm", ["-e"], False)]


# ---------------------------------------------------------------- real usage, read from the CLIs
# Claude: the same endpoint the Claude Code /usage command reads, with the account's own OAuth token.
# agy: "agy -p /usage" in print mode (opens no turn and spends no quota).
# Tokens per model: local Claude Code transcripts (deduplicated by message id) and, for agy,
# the messages of this interface's chats.
import urllib.request, urllib.error
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

USO_ARQ = DATA / "uso.json"
try:
    USO_ULT = json.loads(USO_ARQ.read_text())
except Exception:
    USO_ULT = {}
_uso_cache = {}
_uso_trava = threading.Lock()
_uso_salva = threading.Lock()
_transc = {}


def _guarda_uso(cid, v):
    with _uso_salva:
        USO_ULT[cid] = v
        salvar_json(USO_ARQ, USO_ULT)


def _em_cache(chave, ttl, forcar, fn):
    c = _uso_cache.get(chave)
    if c and not forcar and time.time() - c["t"] < ttl:
        return c["v"]
    v = fn()
    _uso_cache[chave] = {"t": time.time(), "v": v}
    return v


def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def limites_claude(conta):
    try:
        o = json.loads((dir_claude(conta) / ".credentials.json").read_text()).get("claudeAiOauth") or {}
    except Exception:
        o = {}
    tok = o.get("accessToken")
    erro = None
    j = None
    if not tok:
        erro = "Not signed in"
    elif o.get("expiresAt", 0) / 1000 < time.time():
        erro = "Access token expired. It renews the next time this account runs Claude Code."
    else:
        req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "agents-web", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as f:
                j = json.loads(f.read())
        except urllib.error.HTTPError as e:
            erro = f"Usage endpoint answered HTTP {e.code}"
        except Exception:
            erro = "Usage endpoint unreachable"
    if j is None:
        ult = USO_ULT.get(conta["id"])
        return {**ult, "erro": erro, "antigo": True} if ult else {"erro": erro, "linhas": []}
    linhas = []
    cotas = {s.get("limit_index"): s for s in j.get("weekly_scoped_shares") or []}
    for i, l in enumerate(j.get("limits") or []):
        kind = l.get("kind") or ""
        mod = (((l.get("scope") or {}).get("model")) or {}).get("display_name")
        nome = {"session": "Current session", "weekly_all": "Weekly · All models"}.get(kind) or (
            f"Weekly · {mod}" if mod else kind.replace("_", " ").capitalize())
        lin = {"nome": nome, "janela": "5h" if l.get("group") == "session" else "weekly", "usado": l.get("percent") or 0,
               "reinicia": _ts(l.get("resets_at")), "nivel": l.get("severity"), "ativo": bool(l.get("is_active"))}
        if i in cotas and cotas[i].get("allowance_percent_of_weekly"):
            lin["nota"] = f"Can use up to {cotas[i]['allowance_percent_of_weekly']}% of the weekly limit"
        linhas.append(lin)
    if not linhas:
        for k, n in (("five_hour", "Current session"), ("seven_day", "Weekly · All models"),
                     ("seven_day_opus", "Weekly · Opus"), ("seven_day_sonnet", "Weekly · Sonnet")):
            if j.get(k):
                linhas.append({"nome": n, "janela": "5h" if k == "five_hour" else "weekly",
                               "usado": j[k].get("utilization") or 0, "reinicia": _ts(j[k].get("resets_at"))})
    ex = j.get("extra_usage") or {}
    out = {"linhas": linhas, "quando": time.time(),
           "extra": {"ativo": bool(ex.get("is_enabled")), "usado": ex.get("used_credits"), "limite": ex.get("monthly_limit"),
                     "moeda": ex.get("currency")},
           "origem": [{"nome": r.get("display_name"), "pct": r.get("percent")}
                      for r in ((j.get("seven_day_breakdown") or {}).get("rows") or [])]}
    _guarda_uso(conta["id"], out)
    return out


def _agy_cmd(conta, cmd):
    r = subprocess.run([AGY, "-p", cmd, "--output-format", "json"], env=env_conta(conta), capture_output=True,
                       text=True, timeout=45, cwd=str(HOME), stdin=subprocess.DEVNULL)
    for l in reversed((r.stdout or "").strip().splitlines()):
        try:
            return ((json.loads(l).get("command") or {}).get("data")) or {}
        except Exception:
            continue
    raise ValueError((r.stderr or "no output").strip()[-200:])


def cota_agy(conta):
    try:
        d = _agy_cmd(conta, "/usage")
    except Exception as e:
        ult = USO_ULT.get(conta["id"])
        return {**ult, "erro": "agy /usage failed: " + str(e), "antigo": True} if ult else {"erro": str(e), "grupos": []}
    grupos = []
    for g in d.get("groups") or []:
        ls = []
        for b in g.get("buckets") or []:
            rest = b.get("remaining_fraction")
            rest = 1.0 if rest is None else float(rest)
            ls.append({"nome": {"weekly": "Weekly", "5h": "5-hour"}.get(b.get("window"), b.get("name")),
                       "janela": b.get("window"), "usado": round((1 - rest) * 100, 1), "reinicia": _ts(b.get("reset_time")),
                       "nota": b.get("description", "")})
        grupos.append({"nome": g.get("name"), "desc": g.get("description", ""), "linhas": ls})
    out = {"grupos": grupos, "info": d.get("description", ""), "quando": time.time()}
    try:
        cr = _agy_cmd(conta, "/credits")
        out["creditos"] = cr.get("remaining_credits")
    except Exception:
        pass
    _guarda_uso(conta["id"], out)
    return out


def _entradas_transcript(p):
    st = p.stat()
    c = _transc.get(str(p))
    if c and c[0] == st.st_size and c[1] == st.st_mtime:
        return c[2]
    ini, ent = (c[3], list(c[2])) if c and st.st_size > c[0] else (0, [])
    with open(p, "rb") as f:
        f.seek(ini)
        pos = ini
        for l in f:
            if not l.endswith(b"\n"):
                break
            pos += len(l)
            if b'"usage"' not in l or b'"assistant"' not in l:
                continue
            try:
                j = json.loads(l)
            except Exception:
                continue
            m = j.get("message") or {}
            u = m.get("usage")
            mod = m.get("model")
            if j.get("type") != "assistant" or not u or not mod or mod.startswith("<"):
                continue
            try:
                dia = datetime.fromisoformat(j["timestamp"].replace("Z", "+00:00")).astimezone().date().isoformat()
            except Exception:
                continue
            ent.append(((m.get("id"), j.get("requestId")), mod, dia, u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                        u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0))
    _transc[str(p)] = (st.st_size, st.st_mtime, ent, pos)
    return ent


def _agrega(itens, dias=14):
    """itens: (modelo, dia_iso, entrada, saida, cache_l, cache_e)"""
    hoje = date.today()
    cortes = {"today": hoje, "7d": hoje - timedelta(days=6), "30d": hoje - timedelta(days=29), "all": date.min}
    jan = {k: {} for k in cortes}
    serie = {(hoje - timedelta(days=i)).isoformat(): {} for i in range(dias)}
    for mod, dia, a, b, c, d in itens:
        dd = date.fromisoformat(dia)
        for k, ini in cortes.items():
            if dd >= ini:
                v = jan[k].setdefault(mod, [0, 0, 0, 0, 0])
                v[0] += 1; v[1] += a; v[2] += b; v[3] += c; v[4] += d
        if dia in serie:
            serie[dia][mod] = serie[dia].get(mod, 0) + a + b + c + d
    return {"janelas": {k: [{"modelo": m, "n": v[0], "entrada": v[1], "saida": v[2], "cache_l": v[3], "cache_e": v[4]}
                            for m, v in sorted(t.items(), key=lambda x: -(x[1][1] + x[1][2] + x[1][3] + x[1][4]))]
                        for k, t in jan.items()},
            "dias": [{"dia": k, "m": serie[k]} for k in sorted(serie)]}


def tokens_claude(conta):
    base = dir_claude(conta) / "projects"
    vistos, itens, vivos = set(), [], set()
    arquivos = list(base.glob("**/*.jsonl")) if base.exists() else []
    for p in arquivos:
        vivos.add(str(p))
        try:
            ent = _entradas_transcript(p)
        except Exception:
            continue
        for e in ent:
            if e[0] in vistos:
                continue
            vistos.add(e[0])
            itens.append(e[1:])
    for k in [k for k in _transc if k.startswith(str(base)) and k not in vivos]:
        _transc.pop(k, None)
    out = _agrega(itens)
    out["fonte"] = f"{len(arquivos)} local Claude Code transcripts (terminal and web runs)"
    return out


def tokens_web(conta):
    itens = []
    for p in CHATS.glob("*.json"):
        try:
            c = json.loads(p.read_text())
        except Exception:
            continue
        if c.get("agente") != conta["agente"] or c.get("conta") != conta["id"]:
            continue
        for m in c.get("mensagens", []):
            u = m.get("uso") or {}
            if m.get("papel") != "assistente" or not (u.get("entrada") or u.get("saida")):
                continue
            dia = datetime.fromtimestamp(m.get("ts") or c.get("criado") or time.time()).date().isoformat()
            itens.append((m.get("modelo") or c.get("modelo") or "default", dia, u.get("entrada") or 0,
                          (u.get("saida") or 0) + (u.get("pensamento") or 0), u.get("cache_l") or 0, u.get("cache_e") or 0))
    out = _agrega(itens)
    out["fonte"] = "Chats run from this interface (agy keeps no readable token log)"
    return out


def uso_geral(forcar=""):
    """forcar: "1" atualiza tudo, ou o id de uma conta para atualizar so ela."""
    with _uso_trava:
        tarefas = {}
        with ThreadPoolExecutor(4) as ex:
            for c in CFG["contas"]:
                if c["agente"] == "claude":
                    tarefas[c["id"]] = ex.submit(_em_cache, "lim:" + c["id"], 90, forcar in ("1", c["id"]), lambda c=c: limites_claude(c))
                else:
                    tarefas[c["id"]] = ex.submit(_em_cache, "agy:" + c["id"], 120, forcar in ("1", c["id"]), lambda c=c: cota_agy(c))
            tok = {}
            for c in CFG["contas"]:
                if c["agente"] == "claude":
                    tok[c["id"]] = _em_cache("tok:" + c["id"], 45, forcar in ("1", c["id"]), lambda c=c: tokens_claude(c))
                else:
                    tok[c["id"]] = _em_cache("tok:" + c["id"], 45, forcar in ("1", c["id"]), lambda c=c: tokens_web(c))
            lim = {k: f.result() for k, f in tarefas.items()}
        return {"limites": lim, "tokens": tok, "agora": time.time()}


# ---------------------------------------------------------------- terminal (CLI) sessions
def sessoes_cli(conta):
    out = []
    if conta["agente"] == "claude":
        base = dir_claude(conta) / "projects"
        arquivos = sorted(base.glob("*/*.jsonl"), key=lambda p: -p.stat().st_mtime)[:200]
        for p in arquivos:
            titulo, cwd = "", ""
            try:
                with open(p) as f:
                    for i, l in enumerate(f):
                        if i > 400:
                            break
                        d = json.loads(l)
                        if d.get("type") == "custom-title":
                            titulo = d.get("customTitle") or titulo
                        if d.get("cwd") and not cwd:
                            cwd = d["cwd"]
                        if not titulo and d.get("type") == "user" and not d.get("isMeta"):
                            c = d.get("message", {}).get("content")
                            if isinstance(c, str) and not c.startswith("<"):
                                titulo = c[:80]
                            elif isinstance(c, list):
                                for b in c:
                                    if b.get("type") == "text" and not b["text"].startswith("<"):
                                        titulo = b["text"][:80]
                                        break
            except Exception:
                continue
            out.append({"id": p.stem, "titulo": titulo or "(untitled)", "cwd": cwd, "quando": p.stat().st_mtime,
                        "tam": p.stat().st_size})
    else:
        hist = HOME / ".gemini/antigravity-cli/history.jsonl"
        convs = {}
        if hist.exists():
            for l in hist.read_text(errors="replace").splitlines():
                try:
                    d = json.loads(l)
                except Exception:
                    continue
                cid = d.get("conversationId")
                if not cid:
                    continue
                c = convs.setdefault(cid, {"id": cid, "titulo": "", "cwd": d.get("workspace", ""), "quando": 0, "prompts": []})
                c["quando"] = max(c["quando"], d.get("timestamp", 0) / 1000)
                if d.get("type") != "slash_command":
                    c["prompts"].append(d.get("display", ""))
                    if not c["titulo"]:
                        c["titulo"] = d.get("display", "")[:80]
        for p in (HOME / ".gemini/antigravity-cli/conversations").glob("*.db"):
            c = convs.setdefault(p.stem, {"id": p.stem, "titulo": "", "cwd": str(HOME), "quando": 0, "prompts": []})
            c["quando"] = max(c["quando"], p.stat().st_mtime)
        for c in convs.values():
            c["titulo"] = c["titulo"] or "(untitled)"
            c["n"] = len(c.pop("prompts"))
            out.append(c)
        out.sort(key=lambda x: -x["quando"])
    return out


SID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
AGY_DIR = HOME / ".gemini/antigravity-cli"
NOTA_RE = re.compile(r"^\[Interface note: .*?\]\s*", re.S)
CLI_TRAVA = threading.RLock()
_jl = {}
_agy = {"chave": None, "lista": []}


def ativas_claude(conta):
    out = {}
    for p in (dir_claude(conta) / "sessions").glob("*.json"):
        try:
            d = json.loads(p.read_text())
            pid = int(d.get("pid") or 0)
            if WIN:
                if not pid_vivo(pid):
                    continue
            else:
                campos = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                if d.get("procStart") and str(d["procStart"]) != campos[19]:
                    continue
            out[d.get("sessionId")] = d.get("name") or ""
        except (OSError, ValueError, IndexError):
            continue
    return out


def _txt_usuario(d):
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        c = next((b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"), "")
    t = (c or "").strip() if isinstance(c, str) else ""
    return "" if t.startswith("<") or t.startswith("[Request interrupted") else t


def _info_jsonl(p):
    # read only what was appended since last time (transcripts only grow)
    st = p.stat()
    ent = _jl.get(p)
    if ent and ent["ino"] == st.st_ino and ent["pos"] <= st.st_size:
        if ent["pos"] == st.st_size:
            return ent["info"]
        ini, info = ent["pos"], ent["info"]
    else:
        ini, info = 0, {"nome": "", "ai": "", "primeiro": "", "cwd": "", "n": 0}
    with open(p, "rb") as f:
        f.seek(ini)
        dados = f.read()
    fim = dados.rfind(b"\n") + 1
    for l in dados[:fim].split(b"\n"):
        if l.startswith(b'{"type":"custom-title"') or l.startswith(b'{"type":"ai-title"'):
            try:
                d = json.loads(l)
            except ValueError:
                continue
            if d.get("type") == "custom-title":
                info["nome"] = d.get("customTitle") or info["nome"]
            else:
                info["ai"] = d.get("aiTitle") or info["ai"]
        elif b'"type":"user"' in l and b'"tool_result"' not in l:
            try:
                d = json.loads(l)
            except ValueError:
                continue
            if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
                continue
            if d.get("cwd") and not info["cwd"]:
                info["cwd"] = d["cwd"]
            t = _txt_usuario(d)
            if t:
                info["n"] += 1
                info["primeiro"] = info["primeiro"] or t[:120]
    _jl[p] = {"ino": st.st_ino, "pos": ini + fim, "info": info}
    return info


def lista_claude(conta):
    ativas = ativas_claude(conta)
    out = []
    for p in (dir_claude(conta) / "projects").glob("*/*.jsonl"):
        if not SID_RE.fullmatch(p.stem):
            continue
        try:
            info = _info_jsonl(p)
            quando = p.stat().st_mtime
        except OSError:
            continue
        if not info["n"]:
            continue
        titulo = ativas.get(p.stem) or info["nome"] or info["ai"] or info["primeiro"] or "(untitled)"
        out.append({"sid": p.stem, "conta": conta["id"], "agente": "claude", "titulo": re.sub(r"\s+", " ", titulo)[:120],
                    "cwd": info["cwd"], "quando": quando, "n": info["n"], "ativo": p.stem in ativas})
    return out


def _agy_db(escrita=False):
    arq = AGY_DIR / "conversation_summaries.db"
    if escrita:
        return sqlite3.connect(str(arq), timeout=3)
    return sqlite3.connect(f"file:{arq}?mode=ro", uri=True, timeout=2)


def lista_agy(conta):
    arqs = [AGY_DIR / "conversation_summaries.db", AGY_DIR / "conversation_summaries.db-wal",
            AGY_DIR / "history.jsonl", AGY_DIR / "conversations"]
    chave = tuple(a.stat().st_mtime_ns if a.exists() else 0 for a in arqs)
    if _agy["chave"] == chave and time.time() - _agy.get("t", 0) < 120:
        return [{**x, "conta": conta["id"]} for x in _agy["lista"]]
    convs = {}
    for p in (AGY_DIR / "conversations").glob("*.db"):
        if SID_RE.fullmatch(p.stem):
            convs[p.stem] = {"sid": p.stem, "agente": "agy", "titulo": "", "cwd": "", "quando": p.stat().st_mtime,
                             "n": 0, "passos": 0, "ativo": False, "primeiro": ""}
    try:
        db = _agy_db()
        try:
            for cid, tit, ws, est, passos in db.execute(
                    "select conversation_id, title, workspace_uris, status, step_count from conversation_summaries"):
                c = convs.get(cid)
                if not c:
                    continue
                c["titulo"], c["passos"] = tit or "", int(passos or 0)
                try:
                    w = json.loads(ws or "[]")
                except ValueError:
                    w = []
                if w and str(w[0]).startswith("file://"):
                    c["cwd"] = urllib.parse.unquote(str(w[0])[7:])
                c["ativo"] = bool(est) and est != "CASCADE_RUN_STATUS_IDLE" and time.time() - c["quando"] < 900
            for cid, corpo in db.execute("select conversation_id, user_body from conversations_fts where chunk_index = 0"):
                c = convs.get(cid)
                if c and corpo:
                    c["fts"] = (NOTA_RE.sub("", str(corpo)).strip().split("\n") or [""])[0][:120]
        finally:
            db.close()
    except sqlite3.Error:
        pass
    if arqs[2].exists():
        for l in arqs[2].read_text(errors="replace").splitlines():
            try:
                d = json.loads(l)
            except ValueError:
                continue
            c = convs.get(d.get("conversationId"))
            if not c or d.get("type") == "slash_command":
                continue
            c["n"] += 1
            c["primeiro"] = c["primeiro"] or str(d.get("display", ""))[:120]
            c["cwd"] = c["cwd"] or d.get("workspace", "")
    lista = []
    for c in convs.values():
        if not c["n"] and not c["passos"]:
            continue
        c["titulo"] = re.sub(r"\s+", " ", c["titulo"] or c["primeiro"] or c.get("fts") or "(untitled)")[:120]
        c.pop("primeiro", None)
        c.pop("fts", None)
        c["n"] = c["n"] or max(1, c["passos"] // 2)
        lista.append(c)
    _agy.update(chave=chave, lista=lista, t=time.time())
    return [{**x, "conta": conta["id"]} for x in lista]


def turnos_agy(sid):
    # agy's search index keeps the text of every step (user and agent) with byte offsets
    try:
        db = _agy_db()
        try:
            linhas = db.execute("select chunk_index, user_step_byte_offsets, agent_step_byte_offsets, user_body, agent_body "
                                "from conversations_fts where conversation_id=?", (sid,)).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []
    passos = []
    for ci, uo, ao, ub, ab in linhas:
        try:
            if int(ci or 0) < 0:
                continue
        except ValueError:
            continue
        for offs, corpo, papel in ((uo, ub, "usuario"), (ao, ab, "assistente")):
            try:
                lst = json.loads(offs) if offs else []
            except ValueError:
                continue
            b = (corpo or "").encode() if isinstance(corpo, str) else (corpo or b"")
            for i, par in enumerate(lst):
                ini = int(par[1])
                fim = int(lst[i + 1][1]) if i + 1 < len(lst) else len(b)
                passos.append((int(par[0]), papel, b[ini:fim].decode(errors="replace")))
    passos.sort(key=lambda x: x[0])
    msgs = []
    for _, papel, t in passos:
        t = (NOTA_RE.sub("", t) if papel == "usuario" else t).strip()
        if not t:
            continue
        if msgs and msgs[-1]["papel"] == papel:
            msgs[-1]["texto"] += "\n\n" + t
        elif papel == "usuario":
            msgs.append({"papel": "usuario", "texto": t, "anexos": [], "ts": 0})
        else:
            msgs.append({"papel": "assistente", "texto": t, "ferramentas": [], "uso": {}, "contexto": {},
                         "importada": True, "ts": 0})
    return msgs


def lista_cli():
    with CLI_TRAVA:
        out = []
        for conta in CFG["contas"]:
            if conta["agente"] == "claude":
                out += lista_claude(conta)
        agy = next((c for c in CFG["contas"] if c["agente"] == "agy"), None)
        if agy:
            out += lista_agy(agy)
        vivos = set(_jl) - {p for p in _jl if not p.exists()}
        for p in list(_jl):
            if p not in vivos:
                _jl.pop(p, None)
    ligados = set()
    for c in listar_chats():
        ligados |= {c.get("sessao")} | set(c.get("antigas") or []) | set(c.get("sids_conv") or [])
    ligados.discard("")
    ligados.discard(None)
    ligados |= {e.chat.get("sessao") for e in list(EXECS.values()) if e.chat.get("sessao")}
    nomes = CFG.get("titulos_cli") or {}
    out = [x for x in out if x["sid"] not in ligados]
    for x in out:
        if x["sid"] in nomes and not x["ativo"]:
            x["titulo"] = nomes[x["sid"]]
    out.sort(key=lambda x: -x["quando"])
    return out[:500]


def renomear_sessao(conta, sid, titulo):
    titulo = re.sub(r"\s+", " ", str(titulo or "")).strip()[:120]
    if not titulo:
        raise ValueError("the name is empty")
    if not SID_RE.fullmatch(sid or ""):
        raise ValueError("invalid session")
    if conta["agente"] == "claude":
        if sid in ativas_claude(conta):
            raise ValueError("this conversation is open in a terminal, rename it there with /rename")
        arqs = list((dir_claude(conta) / "projects").glob(f"*/{sid}.jsonl"))
        if not arqs:
            raise ValueError("session not found")
        linhas = [{"type": "custom-title", "customTitle": titulo, "sessionId": sid},
                  {"type": "agent-name", "agentName": titulo, "sessionId": sid}]
        with open(arqs[0], "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in linhas))
    with trava:
        CFG.setdefault("titulos_cli", {})[sid] = titulo
        salvar_json(CFG_FILE, CFG)
    return titulo


def _tamanho(p):
    if p.is_symlink() or not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def _remove(p):
    n = _tamanho(p)
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)
    return n


def apagar_sessao(conta, sid):
    if not SID_RE.fullmatch(sid or ""):
        raise ValueError("invalid session")
    if any(not e.acabou and e.chat.get("sessao") == sid for e in list(EXECS.values())):
        raise ValueError("this conversation is running here, stop it first")
    livres = 0
    if conta["agente"] == "claude":
        if sid in ativas_claude(conta):
            raise ValueError("this conversation is open in a terminal right now, close it there first")
        base = dir_claude(conta)
        alvos = list((base / "projects").glob(f"*/{sid}.jsonl")) + list((base / "projects").glob(f"*/{sid}"))
        alvos += [base / "file-history" / sid, base / "session-env" / sid]
    else:
        if any(x["sid"] == sid and x["ativo"] for x in lista_agy(conta)):
            raise ValueError("this conversation is running in Antigravity right now")
        alvos = [AGY_DIR / "conversations" / (sid + s) for s in (".db", ".db-wal", ".db-shm")]
        alvos += [AGY_DIR / "brain" / sid, AGY_DIR / "annotations" / (sid + ".pbtxt")]
    for a in alvos:
        if a.exists() or a.is_symlink():
            livres += _remove(a)
    if conta["agente"] == "agy":
        try:
            db = _agy_db(True)
            try:
                with db:
                    for t in ("conversation_summaries", "conversations_fts", "conversation_parents"):
                        db.execute(f"delete from {t} where conversation_id=?", (sid,))
            finally:
                db.close()
        except sqlite3.Error:
            pass
        _agy["chave"] = None
    with trava:
        if (CFG.get("titulos_cli") or {}).pop(sid, None) is not None:
            salvar_json(CFG_FILE, CFG)
    troca_fixado("cli:" + sid, None)
    return livres


def fixar(chave, fixo):
    chave = str(chave or "")
    if not (re.fullmatch(r"[a-f0-9]{12}", chave) or (chave.startswith("cli:") and SID_RE.fullmatch(chave[4:]))):
        raise ValueError("invalid chat")
    with trava:
        f = [x for x in CFG.get("fixados") or [] if x != chave]
        if fixo:
            f.insert(0, chave)
        CFG["fixados"] = f[:200]
        salvar_json(CFG_FILE, CFG)
        return list(CFG["fixados"])


def salvar_prompts(lista):
    """Saved prompts, shared by every device. The page sends the whole list each time."""
    if not isinstance(lista, list) or len(lista) > 200:
        raise ValueError("invalid prompt list (200 prompts at most)")
    out, ids, total = [], set(), 0
    for p in lista:
        if not isinstance(p, dict):
            raise ValueError("invalid prompt")
        texto = str(p.get("texto") or "")
        if not texto.strip():
            continue
        if len(texto) > 20000:
            raise ValueError("a prompt is too long (20000 characters at most)")
        total += len(texto)
        if total > 400000:
            raise ValueError("saved prompts are too long altogether")
        nome = " ".join(str(p.get("nome") or "").split())[:80] or " ".join(texto.split())[:60]
        pid = str(p.get("id") or "")
        if not re.fullmatch(r"[a-f0-9]{8}", pid) or pid in ids:
            pid = secrets.token_hex(4)
        ids.add(pid)
        out.append({"id": pid, "nome": nome, "texto": texto})
    with trava:
        CFG["prompts"] = out
        salvar_json(CFG_FILE, CFG)
    return out


def troca_fixado(velho, novo):
    with trava:
        f = CFG.get("fixados") or []
        if velho not in f:
            return
        CFG["fixados"] = [novo if x == velho else x for x in f if x != novo or novo is None]
        CFG["fixados"] = [x for x in CFG["fixados"] if x]
        salvar_json(CFG_FILE, CFG)


def importar_cli(conta, sid):
    msgs, cwd, titulo = [], str(HOME), ""
    if not SID_RE.fullmatch(sid or ""):
        raise ValueError("session not found")
    for r in listar_chats():
        if r.get("sessao") == sid:
            return ler_chat(r["id"])
    titulo_ai, cwd0 = "", ""
    if conta["agente"] == "claude":
        achados = list((dir_claude(conta) / "projects").glob(f"*/{sid}.jsonl"))
        if not achados or not re.fullmatch(r"[\w-]+", sid):
            raise ValueError("session not found")
        for l in open(achados[0]):
            try:
                d = json.loads(l)
            except Exception:
                continue
            if d.get("cwd"):
                cwd0 = cwd0 or d["cwd"]
            if d.get("type") == "custom-title":
                titulo = d.get("customTitle") or titulo
            elif d.get("type") == "ai-title":
                titulo_ai = d.get("aiTitle") or titulo_ai
            if d.get("isMeta") or d.get("isSidechain"):
                continue
            quando = _ts(d.get("timestamp")) or 0
            m = d.get("message") or {}
            c = m.get("content")
            if d.get("type") == "user":
                txt = c if isinstance(c, str) else "\n".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                if txt and not txt.startswith("<") and not txt.startswith("[Request interrupted"):
                    msgs.append({"papel": "usuario", "texto": txt, "anexos": [], "ts": quando})
            elif d.get("type") == "assistant":
                txt = "\n".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                ferr = [{"id": b.get("id"), "nome": b.get("name"), "entrada": corta(b.get("input"), 1500), "saida": "",
                         "erro": False, "fim": True} for b in (c or []) if isinstance(b, dict) and b.get("type") == "tool_use"]
                if msgs and msgs[-1]["papel"] == "assistente" and msgs[-1].get("importada"):
                    if txt:
                        msgs[-1]["texto"] = (msgs[-1]["texto"] + "\n\n" + txt).strip()
                    msgs[-1]["ferramentas"] += ferr
                else:
                    msgs.append({"papel": "assistente", "texto": txt, "ferramentas": ferr, "uso": {}, "importada": True,
                                 "contexto": {}, "ts": quando, "modelo": m.get("model", "")})
                if d.get("uuid") and txt and not ferr:
                    msgs[-1]["uuid"] = d["uuid"]
    else:
        for x in lista_agy(conta):
            if x["sid"] == sid:
                cwd = x["cwd"] or cwd
                titulo = x["titulo"]
        datas = []
        hist = AGY_DIR / "history.jsonl"
        if hist.exists():
            for l in hist.read_text(errors="replace").splitlines():
                try:
                    d = json.loads(l)
                except Exception:
                    continue
                if d.get("conversationId") == sid and d.get("type") != "slash_command":
                    datas.append((d.get("display", ""), d.get("timestamp", 0) / 1000))
        msgs = turnos_agy(sid)
        us = [m for m in msgs if m["papel"] == "usuario"]
        if len(us) == len(datas):
            for m, (_, t) in zip(us, datas):
                m["ts"] = t
        if not msgs:
            msgs = [{"papel": "usuario", "texto": t, "anexos": [], "ts": q} for t, q in datas]
            msgs.append({"papel": "assistente", "texto": "_(Antigravity has not indexed the replies of this conversation yet, so only your prompts appear here. The conversation continues normally.)_",
                         "ferramentas": [], "uso": {}, "contexto": {}, "importada": True, "ts": 0})
    if conta["agente"] == "claude" and cwd0:
        cwd = cwd0
    titulo = (CFG.get("titulos_cli") or {}).get(sid) or titulo
    if not titulo:
        titulo = titulo_ai or next((m["texto"][:60] for m in msgs if m["papel"] == "usuario"), "Terminal conversation")
    chat = novo_chat({"agente": conta["agente"], "conta": conta["id"], "cwd": cwd, "sessao": sid,
                      "titulo": re.sub(r"\s+", " ", titulo).strip()[:120]})
    chat["origem"] = "cli"
    chat["mensagens"] = msgs
    gravar_chat(chat)
    troca_fixado("cli:" + sid, chat["id"])
    try:
        if conta["agente"] == "claude":
            quando = achados[0].stat().st_mtime
        else:
            quando = next((x["quando"] for x in lista_agy(conta) if x["sid"] == sid), 0)
        if quando:
            chat["atualizado"] = quando
            salvar_json(caminho_chat(chat["id"]), chat)
    except OSError:
        pass
    return chat


# ---------------------------------------------------------------- phone pairing
PIN = {"pin": None, "ate": 0}
FALHAS = {"n": 0, "ate": 0}


def novo_pin():
    PIN["pin"] = "".join(secrets.choice("0123456789") for _ in range(6))
    PIN["ate"] = time.time() + 300
    ips = info_rede()["ips"]
    url_http = f"http://{ips[0]}:{PORTA}/?pin={PIN['pin']}" if ips else ""
    url = url_http.replace("http://", "https://", 1) if TLS["ctx"] else url_http
    svg = ""
    if url and shutil.which("qrencode"):
        svg = subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-o", "-", url], capture_output=True, text=True).stdout
    return {"pin": PIN["pin"], "url": url, "url_http": url_http, "svg": svg, "expira": PIN["ate"],
            "tls": bool(TLS["ctx"])}


PASSES = {}


def igual(a, b):
    # constant time, and safe for any text a client sends (str compare_digest rejects non-ASCII)
    return secrets.compare_digest(str(a).encode("utf-8", "surrogateescape"), str(b).encode())


def token_valido(tok):
    if not tok:
        return None
    if igual(tok, CFG["token"]):
        return "pc"
    for d in CFG["dispositivos"]:
        if igual(tok, d["token"]):
            agora = time.time()
            if agora - (d.get("visto") or 0) > 600:  # "last seen" survives a restart, saved at most every 10 min
                d["visto"] = agora
                try:
                    with trava:
                        salvar_json(CFG_FILE, CFG)
                except OSError:
                    pass
            return "dispositivo"
    return None


# ---------------------------------------------------------------- app icon and manifest (install on a phone)
def arquivo_app(nome):
    for d in (BASE, BASE.parent / "data"):  # installed copy, or a git checkout
        if (d / nome).is_file():
            return d / nome
    return None


MANIFESTO = {"name": "Agents", "short_name": "Agents", "description": "Claude Code and Antigravity on this PC",
             "start_url": "/", "scope": "/", "display": "standalone", "background_color": "#111111",
             "theme_color": "#111111",
             "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                       {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                       {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}


# ---------------------------------------------------------------- updates
# Every way of installing updates itself from the release assets: install.sh (a copy per user), the .deb,
# .rpm and pacman packages (root through pkexec), the AppImage and the Windows installer. Downloads are
# checked against the SHA256SUMS file of the release before anything runs.
REPO_GH, REPO_GL = "gabrielmf1998/Agents-Web", "gabriel17166/agents-web"
INSTALADORES = {"github": f"https://raw.githubusercontent.com/{REPO_GH}/main/install.sh",
                "gitlab": f"https://gitlab.com/{REPO_GL}/-/raw/main/install.sh"}
# tests point this at a fake GitHub API (a /releases/latest JSON with its assets)
API_RELEASES = os.environ.get("AGENTS_WEB_RELEASES_API", "")
ATUALIZA = {"quando": 0, "dados": None, "rodando": 0, "erro": "", "fase": ""}
ATUALIZA_LOG = DATA / "update.log"
TIPOS = ("script", "deb", "rpm", "pacman", "appimage", "windows")
# needs no password, so it can update by itself
SOZINHA = ("script", "appimage", "windows")
ATIVIDADE = {"t": time.time()}


def origem():
    try:
        o = (BASE / "source").read_text().strip()
    except OSError:
        o = ""
    return o if o in INSTALADORES else "github"


def _vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3]) or (0,)


def instalacao():
    """How this copy was installed (one of TIPOS), or "" for a git checkout, which updates with git."""
    try:
        k = (BASE / "install-kind").read_text().strip()
    except OSError:
        k = ""
    if k == "appimage" and not os.environ.get("APPIMAGE"):
        k = ""
    if k in TIPOS:
        return k
    # 1.8 and older: install.sh copied itself next to the server
    return "script" if (BASE / "install.sh").is_file() and (BASE / "agents-web-tray").is_file() else ""


def instalado():
    return bool(instalacao())


def maquina():
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(m, m)


def _nome_asset(tipo, v):
    m = maquina()
    return {"script": rf"agents-web-{v}-linux-{m}\.tar\.gz",
            "deb": rf"agents-web_{v}_{'amd64' if m == 'x86_64' else 'arm64' if m == 'aarch64' else m}\.deb",
            "rpm": rf"agents-web-{v}-\d+\.{m}\.rpm",
            "pacman": rf"agents-web-{v}-\d+-{m}\.pkg\.tar\.zst",
            "appimage": rf"Agents_Web-{v}-{m}\.AppImage",
            "windows": rf"AgentsWeb-Setup-{v}\.exe"}[tipo]


def _baixa_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "agents-web", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as f:
        return json.loads(f.read())


def checar_atualizacao(forcar=False):
    if ATUALIZA["dados"] and not forcar and time.time() - ATUALIZA["quando"] < 600:
        return ATUALIZA["dados"]
    o = origem()
    if API_RELEASES:
        url = API_RELEASES
    elif o == "gitlab":
        url = f"https://gitlab.com/api/v4/projects/{urllib.parse.quote(REPO_GL, safe='')}/releases/permalink/latest"
    else:
        url = f"https://api.github.com/repos/{REPO_GH}/releases/latest"
    try:
        j = _baixa_json(url)
    except Exception as e:
        raise ValueError(f"could not reach {'GitLab' if o == 'gitlab' else 'GitHub'}: {e}")
    tag = str(j.get("tag_name") or "")
    # GitHub lists assets with their download URL, GitLab as release links
    assets = {a["name"]: {"url": a.get("browser_download_url"), "digest": a.get("digest") or ""}
              for a in j.get("assets") or [] if isinstance(a, dict) and a.get("browser_download_url")}
    for l in ((j.get("assets") or {}).get("links") or []) if isinstance(j.get("assets"), dict) else []:
        assets[l.get("name")] = {"url": l.get("direct_asset_url") or l.get("url"), "digest": ""}
    tipo = instalacao()
    arquivo = bool(tipo) and any(n and re.match(_nome_asset(tipo, re.escape(tag.lstrip("v"))) + "$", n) for n in assets)
    dados = {"atual": VERSAO, "ultima": tag.lstrip("v"), "nova": _vt(tag) > _vt(VERSAO),
             "notas": str(j.get("body") or j.get("description") or "")[:8000],
             "url": j.get("html_url") or (j.get("_links") or {}).get("self") or "",
             "data": j.get("published_at") or j.get("released_at") or "", "origem": o, "instalavel": bool(tipo),
             "tipo": tipo, "arquivo": arquivo, "sozinha": arquivo and tipo in SOZINHA and _pode_escrever(),
             "senha": tipo in ("deb", "rpm", "pacman"), "maquina": maquina()}
    ATUALIZA.update(quando=time.time(), dados=dados, assets=assets)
    return dados


def _pode_escrever():
    # the AppImage replaces its own file, so its folder must be writable
    if instalacao() == "appimage":
        return os.access(os.path.dirname(os.environ.get("APPIMAGE", "/")) or "/", os.W_OK)
    return True


def _registra(texto):
    with open(ATUALIZA_LOG, "a", encoding="utf-8") as f:
        f.write(f"== {time.strftime('%Y-%m-%d %H:%M:%S')} {texto}\n")


def _baixa(url, destino, esperado=""):
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "agents-web"})
    with urllib.request.urlopen(req, timeout=60) as f, open(destino, "wb") as o:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            o.write(b)
    if esperado and h.hexdigest() != esperado:
        destino.unlink(missing_ok=True)
        raise ValueError(f"the download of {destino.name} does not match its SHA256 sum, nothing was installed")
    return h.hexdigest()


def baixar_versao(d):
    """Download this install's asset of the release in `d` and check it against SHA256SUMS."""
    tipo = d["tipo"]
    assets = ATUALIZA.get("assets") or {}
    padrao = re.compile(_nome_asset(tipo, re.escape(d["ultima"])) + "$")
    nome = next((n for n in assets if n and padrao.match(n)), None)
    if not nome:
        raise ValueError(f"release {d['ultima']} has no file for this kind of install ({tipo}, {maquina()})")
    somas = {}
    if "SHA256SUMS" in assets:
        texto = urllib.request.urlopen(urllib.request.Request(assets["SHA256SUMS"]["url"], headers={
            "User-Agent": "agents-web"}), timeout=30).read().decode()
        somas = {p[1].lstrip("*"): p[0].lower() for p in (l.split() for l in texto.splitlines()) if len(p) == 2}
    esperado = somas.get(nome) or assets[nome]["digest"].replace("sha256:", "")
    if not esperado:
        raise ValueError(f"release {d['ultima']} has no SHA256 sum for {nome}, so it was not installed")
    pasta = DATA / "updates"
    shutil.rmtree(pasta, ignore_errors=True)
    pasta.mkdir(parents=True)
    arq = pasta / nome
    _registra(f"downloading {assets[nome]['url']}")
    _baixa(assets[nome]["url"], arq, esperado)
    _registra(f"{nome}: SHA256 {esperado} checked")
    return arq


def _gerenciador(arq):
    """The command that installs a downloaded package with this system's package manager."""
    n = arq.name
    if n.endswith(".deb"):
        return ["apt-get", "install", "-y", "--allow-downgrades", str(arq)] if shutil.which("apt-get") \
            else ["dpkg", "-i", str(arq)]
    if n.endswith(".rpm"):
        for pm, cmd in (("dnf5", ["dnf5", "install", "-y", str(arq)]), ("dnf", ["dnf", "install", "-y", str(arq)]),
                        ("zypper", ["zypper", "--non-interactive", "install", "--allow-unsigned-rpm", str(arq)]),
                        ("yum", ["yum", "install", "-y", str(arq)])):
            if shutil.which(pm):
                return cmd
        return ["rpm", "-U", "--replacepkgs", str(arq)]
    return ["pacman", "-U", "--noconfirm", "--needed", str(arq)]


def ocioso():
    return not any(not e.acabou for e in EXECS.values())


def atualizar(auto=False):
    tipo = instalacao()
    if not tipo:
        raise ValueError("this copy was not installed by install.sh, a package or the installer, update it with git")
    if not ocioso():
        rod = sum(1 for e in EXECS.values() if not e.acabou)
        raise ValueError(f"wait for the running {'reply' if rod == 1 else f'{rod} replies'} to finish, or stop "
                         f"{'it' if rod == 1 else 'them'}: updating restarts the service")
    if time.time() - ATUALIZA["rodando"] < 300:
        raise ValueError("an update is already running")
    d = checar_atualizacao(forcar=True)
    if not d["nova"]:
        raise ValueError(f"{VERSAO} is already the latest version")
    ATUALIZA.update(rodando=time.time(), erro="", fase="download")
    threading.Thread(target=_atualiza, args=(d, auto), daemon=True).start()


def _atualiza(d, auto):
    tipo = d["tipo"]
    try:
        _registra(f"update {VERSAO} to {d['ultima']} ({tipo}{', automatic' if auto else ''})")
        arq = baixar_versao(d)
        ATUALIZA["fase"] = "install"
        if tipo == "windows":
            _aplica_windows(arq)
        elif tipo in ("deb", "rpm", "pacman"):
            _aplica_pacote(arq)
        elif tipo == "appimage":
            _aplica_appimage(arq)
            _espera_fim()
        else:
            _aplica_script(arq)
            _espera_fim()
    except Exception as e:
        _registra(f"failed: {e}")
        ATUALIZA.update(rodando=0, erro=str(e)[:600], fase="")


def _espera_fim():
    # the installer (or the AppImage) stops the tray, and with it this server, within seconds
    time.sleep(float(os.environ.get("AGENTS_WEB_UPDATE_WAIT") or 240))
    raise ValueError(f"the update did not restart Agents Web, see {ATUALIZA_LOG}")


def _extrai(arq):
    import tarfile
    pasta = arq.parent / "src"
    pasta.mkdir()
    with tarfile.open(arq) as tf:
        for m in tf.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts or not (m.isfile() or m.isdir()):
                raise ValueError(f"unexpected entry in {arq.name}: {m.name}")
        tf.extractall(pasta, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    raiz = next((p for p in pasta.iterdir() if (p / "install.sh").is_file()), None)
    if not raiz:
        raise ValueError(f"{arq.name} has no install.sh")
    return raiz


def _aplica_script(arq):
    # the new release's own install.sh puts the new files in place and restarts the tray
    raiz = _extrai(arq)
    _em_unidade(f"bash {shlex.quote(str(raiz / 'install.sh'))} --from {shlex.quote(str(raiz))} --no-deps"
                + (" --gitlab" if origem() == "gitlab" else ""))


def _aplica_appimage(arq):
    alvo = Path(os.environ["APPIMAGE"])
    os.chmod(arq, 0o755)
    novo = alvo.with_name(f".{alvo.name}.new")
    shutil.copy2(arq, novo)
    os.replace(novo, alvo)
    _registra(f"replaced {alvo}")
    _em_unidade(f"{shlex.quote(str(alvo))} restart")


def _aplica_pacote(arq):
    cmd = _gerenciador(arq)
    if os.geteuid() != 0:
        pk = shutil.which("pkexec")
        if not pk:
            raise ValueError("pkexec was not found, install the package yourself: sudo " + shlex.join(cmd))
        cmd = [pk] + cmd
    _registra("running " + shlex.join(cmd))
    ATUALIZA["fase"] = "senha"
    with open(ATUALIZA_LOG, "a", encoding="utf-8") as log:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    if r.returncode != 0:
        raise ValueError(("the password was not given or was wrong" if r.returncode in (126, 127) and cmd[0].endswith("pkexec")
                          else f"{cmd[1] if cmd[0].endswith('pkexec') else cmd[0]} failed with code {r.returncode}")
                         + f", see {ATUALIZA_LOG}. You can also run: sudo {shlex.join(_gerenciador(arq))}")
    _registra("package installed, restarting the server")
    ATUALIZA["fase"] = "reinicio"
    reiniciar_servidor()


def reiniciar_servidor():
    """Run the new server.py in this same process: the tray keeps watching the same PID."""
    time.sleep(0.5)
    try:
        if PK["ok"]:
            PK_SOCK.unlink(missing_ok=True)
    except OSError:
        pass
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, "-u", str(BASE / "server.py")])


SAIDA_ATUALIZA = 75   # exit code that tells AgentsWeb.exe to run the installer named in updates\\setup.txt


def _aplica_windows(arq, terminal=False):
    """Run the downloaded installer (/S /UPDATE): it stops the tray, copies the new files and starts the tray again.
    Under the tray the server is inside the tray's job object, which the installer must not be part of (stopping
    the tray kills the job), so the tray starts it: this server writes its path and exits with SAIDA_ATUALIZA."""
    if not terminal and os.environ.get("AGENTS_WEB_TRAY"):
        (arq.parent / "setup.txt").write_text(str(arq), encoding="utf-8")
        _registra(f"asking the tray to run {arq.name} /S /UPDATE")
        ATUALIZA["fase"] = "reinicio"
        time.sleep(0.5)
        sys.stdout.flush()
        os._exit(SAIDA_ATUALIZA)
    _registra(f"starting {arq.name} /S /UPDATE")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(arq), "/S", "/UPDATE"], cwd=str(arq.parent), creationflags=flags, close_fds=True)


def _em_unidade(comando):
    script = (f"exec >>{shlex.quote(str(ATUALIZA_LOG))} 2>&1 </dev/null; sleep 1; {comando}")
    if not _atualizar_unidade(script):
        # its own session: the tray stops this server's whole process group while it reinstalls
        subprocess.Popen(["bash", "-c", script], cwd=str(HOME), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True)


def _atualizar_unidade(script):
    # The tray often runs inside a systemd unit (autostart, systemd-run). When the installer stops the tray, a
    # unit that ends with its main process kills everything left in it, the installer included. A unit of its
    # own avoids that, and ExitType=cgroup keeps it alive for the tray the installer starts again inside it.
    sr = shutil.which("systemd-run")
    if not sr:
        return False
    cmd = [sr, "--user", "--collect", "--quiet", "-p", "ExitType=cgroup", f"--unit=agents-web-update-{int(time.time())}",
           f"--working-directory={HOME}"]
    for k in ("PATH", "HOME", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
              "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "LANG", "AGENTS_WEB_PORT", "AGENTS_WEB_CONFIG_DIR",
              "AGENTS_WEB_DATA_DIR", "AGENTS_WEB_RELEASES_API"):
        if os.environ.get(k):
            cmd.append(f"--setenv={k}={os.environ[k]}")
    try:
        r = subprocess.run(cmd + ["bash", "-c", script], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def resumo_atualizacao():
    d = ATUALIZA["dados"] or {}
    return {"ultima": d.get("ultima", ""), "nova": bool(d.get("nova")), "tipo": instalacao(),
            "sozinha": bool(d.get("sozinha")), "senha": bool(d.get("senha")), "erro": ATUALIZA["erro"],
            "fase": ATUALIZA["fase"], "rodando": bool(ATUALIZA["rodando"])}


def vigia_atualizacoes():
    """Look for a new release a while after start and then twice a day. With automatic updates, install it
    at a quiet moment (no reply running for 10 minutes); installs that need a password only say so."""
    time.sleep(float(os.environ.get("AGENTS_WEB_UPDATE_DELAY") or 120))
    ultima = 0
    while True:
        modo = CFG["padrao"].get("atualizar", "auto")
        if modo != "nao" and instalado():
            try:
                if time.time() - ultima > 12 * 3600:
                    checar_atualizacao(forcar=True)
                    ultima = time.time()
                d = ATUALIZA["dados"]
                if (modo == "auto" and d and d["nova"] and d["sozinha"] and not ATUALIZA["rodando"]
                        and not ATUALIZA["erro"] and ocioso()
                        and time.time() - ATIVIDADE["t"] > float(os.environ.get("AGENTS_WEB_UPDATE_IDLE") or 600)):
                    atualizar(auto=True)
            except Exception as e:
                print(f"update check failed: {e}", flush=True)
        time.sleep(float(os.environ.get("AGENTS_WEB_UPDATE_EVERY") or 300))


def atualizar_terminal():
    """`agents-web update`: the same update, in the terminal, with sudo for packages."""
    tipo = instalacao()
    if not tipo:
        sys.exit("this copy is a git checkout, update it with git pull")
    d = checar_atualizacao(forcar=True)
    if not d["nova"]:
        print(f"Agents Web {VERSAO} is the latest version.")
        return
    print(f"Updating Agents Web {VERSAO} to {d['ultima']} ({tipo})")
    arq = baixar_versao(d)
    print(f"Downloaded {arq.name}, SHA256 checked")
    if tipo in ("deb", "rpm", "pacman"):
        cmd = _gerenciador(arq)
        if os.geteuid() != 0:
            cmd = (["sudo"] if shutil.which("sudo") else ["pkexec"]) + cmd
        print("+ " + shlex.join(cmd), flush=True)
        r = subprocess.run(cmd)
        if r.returncode:
            sys.exit(r.returncode)
        print("Installed. Run 'agents-web restart' if it is running.")
    elif tipo == "appimage":
        _aplica_appimage(arq)
        print("Done, Agents Web restarts by itself.")
    elif tipo == "windows":
        _aplica_windows(arq, terminal=True)
        print("The installer is running, Agents Web starts again when it ends.")
    else:
        raiz = _extrai(arq)
        sys.exit(subprocess.run(["bash", str(raiz / "install.sh"), "--from", str(raiz), "--no-deps"]
                                + (["--gitlab"] if origem() == "gitlab" else [])).returncode)



# ---------------------------------------------------------------- HTTP
PAGINA = BASE / "index.html"

LOGIN = """<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Agents</title><style>body{background:#08090c;color:#e8e8ee;font:15px system-ui;display:grid;place-items:center;height:100vh;margin:0}
form{border:1px solid #2a2d38;background:#101218;padding:28px;border-radius:14px;width:min(340px,90vw)}h1{font-size:20px;margin:0 0 6px}
input,button{width:100%;box-sizing:border-box;font:inherit;padding:12px;margin-top:12px;background:#08090c;color:#fff;border:1px solid #333847;border-radius:10px}
input{font:600 24px ui-monospace,monospace;letter-spacing:8px;text-align:center}button{background:#fff;color:#000;font-weight:600;border:0}p{color:#8a8fa3;font-size:14px;margin:0}</style>
<form method=get action="/"><h1>Agents</h1><p>Enter the pairing code shown on your PC (tray icon, Pair phone). %ERRO%</p>
<input name=pin inputmode=numeric autocomplete=one-time-code maxlength=6 autofocus placeholder="000000"><button>Connect</button></form></html>"""


class Servidor(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # on Windows SO_REUSEADDR lets a second server take the same port
    allow_reuse_address = not WIN

    def process_request_thread(self, request, client_address):
        # same port for http and https: a first byte of 0x16 is a TLS ClientHello
        try:
            request.settimeout(20)
            if request.recv(1, socket.MSG_PEEK) == b"\x16":
                if not TLS["ctx"]:
                    raise OSError("https is not ready")
                request = TLS["ctx"].wrap_socket(request, server_side=True)
            # generous, but finite: SSE pings every 15 s and uploads keep sending, so only dead peers hit it
            request.settimeout(300)
            self.finish_request(request, client_address)
        except Exception:
            pass
        finally:
            self.shutdown_request(request)


class H(http.server.BaseHTTPRequestHandler):
    server_version = "AgentsWeb"
    sys_version = ""

    def log_message(self, *a):
        pass

    # -- helpers
    def resp(self, codigo, corpo, tipo="application/json; charset=utf-8", extra=None):
        if isinstance(corpo, (dict, list)):
            corpo = json.dumps(corpo, ensure_ascii=False).encode()
        elif isinstance(corpo, str):
            corpo = corpo.encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def corpo_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20_000_000:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def cookie(self, nome):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == nome:
                return v
        return ""

    def portaria(self):
        ip = self.client_address[0]
        if not origem_ok(ip) or not host_ok(self.headers.get("Host")):
            self.resp(403, {"erro": "outside the local network"})
            return False
        self.local = ipaddress.ip_address(ip).is_loopback
        self.tls = isinstance(self.connection, ssl.SSLSocket)
        return True

    def cookie_ag(self, tok):
        return (f"{'ags' if self.tls else 'ag'}={tok}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"
                + ("; Secure" if self.tls else ""))

    def autorizado(self, permitir_query=False):
        tok = self.headers.get("X-Token", "")
        if not tok and permitir_query:
            tok = self.q.get("t", [""])[0]
        quem = token_valido(tok)
        if not quem:
            self.resp(401, {"erro": "unauthorized"})
        return quem

    # -- routes
    def do_GET(self):
        self.rota("GET")

    def do_POST(self):
        self.rota("POST")

    def do_HEAD(self):
        if self.path.startswith("/api/"):
            return self.resp(405, b"", "text/plain")
        self.rota("GET")

    def rota(self, metodo):
        if not self.portaria():
            return
        u = urllib.parse.urlparse(self.path)
        self.q = urllib.parse.parse_qs(u.query)
        p = u.path
        try:
            if p == "/saude" and self.local:
                rod = sum(1 for e in EXECS.values() if not e.acabou)
                return self.resp(200, f"ok {rod}", "text/plain")
            if p in ("/", "/index.html") and metodo == "GET":
                return self.pagina()
            if p in ("/favicon.ico", "/icon.svg", "/icon-192.png", "/icon-512.png"):
                nome = {"/favicon.ico": "agents-web.svg", "/icon.svg": "agents-web.svg"}.get(p, p[1:])
                arq = arquivo_app(nome)
                if not arq:
                    return self.resp(404 if p != "/favicon.ico" else 204, b"", "text/plain")
                return self.resp(200, arq.read_bytes(), "image/svg+xml" if nome.endswith(".svg") else "image/png",
                                 {"Cache-Control": "max-age=86400"})
            if p == "/manifest.webmanifest":
                return self.resp(200, json.dumps(MANIFESTO), "application/manifest+json")
            if p == "/sw.js":
                return self.resp(200, SW_JS, "text/javascript; charset=utf-8")
            if p == "/ca.crt":
                ca = TLS_DIR / "ca.crt"
                if not ca.exists():
                    return self.resp(404, {"erro": "no certificate yet"})
                return self.resp(200, ca.read_bytes(), "application/x-x509-ca-cert",
                                 {"Content-Disposition": 'attachment; filename="agents-pc.crt"'})
            if p.startswith("/f/") and metodo == "GET":
                alvo = acha_local(p[3:])
                if not alvo:
                    return self.resp(404, {"erro": "file not found or link expired"})
                return self.servir_local(alvo)
            if not p.startswith("/api/"):
                return self.resp(404, {"erro": "not found"})
            if p == "/api/arquivo":
                if not self.autorizado(True):
                    return
                return self.arquivo()
            quem = self.autorizado(permitir_query=p.endswith("/eventos"))
            if not quem:
                return
            self.quem = quem
            return self.api(metodo, p)
        except (ValueError, FileNotFoundError, KeyError) as e:
            return self.resp(400, {"erro": str(e)})
        except PermissionError as e:
            return self.resp(403, {"erro": f"no permission: {e.filename or e.strerror}"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            return self.resp(500, {"erro": repr(e)})

    def pagina(self):
        tok = None
        extra = {}
        if self.local:
            tok = CFG["token"]
        else:
            c = self.cookie("ags" if self.tls else "ag")
            if token_valido(c) == "dispositivo":
                tok = c
            passe = self.q.get("passe", [""])[0]
            if passe:
                v = PASSES.pop(passe, None)
                if v and v[1] > time.time() and token_valido(v[0]) == "dispositivo":
                    return self.resp(303, b"", "text/plain", {"Location": "/", "Set-Cookie": self.cookie_ag(v[0])})
            pin = self.q.get("pin", [""])[0]
            if not tok and pin:
                if FALHAS["ate"] > time.time():
                    return self.resp(429, LOGIN.replace("%ERRO%", "Too many attempts. Please wait a few minutes."), "text/html; charset=utf-8")
                if PIN["pin"] and time.time() < PIN["ate"] and igual(pin, PIN["pin"]):
                    tok = secrets.token_urlsafe(32)
                    with trava:
                        CFG["dispositivos"].append({"token": tok, "nome": (self.headers.get("User-Agent") or "?")[:120],
                                                    "ip": self.client_address[0], "criado": time.time(), "visto": time.time()})
                        salvar_json(CFG_FILE, CFG)
                    PIN["pin"] = None
                    FALHAS["n"] = 0
                    return self.resp(303, b"", "text/plain", {"Location": "/", "Set-Cookie": self.cookie_ag(tok)})
                FALHAS["n"] += 1
                if FALHAS["n"] >= 5:
                    FALHAS.update(n=0, ate=time.time() + 600)
                return self.resp(200, LOGIN.replace("%ERRO%", "<br><br><b style=color:#ff8080>Wrong or expired code.</b>"), "text/html; charset=utf-8")
            if not tok:
                return self.resp(200, LOGIN.replace("%ERRO%", ""), "text/html; charset=utf-8")
        html_ = PAGINA.read_text().replace("__TOKEN__", tok).replace("__LOCAL__", "true" if self.local else "false")
        self.resp(200, html_, "text/html; charset=utf-8", {"Content-Security-Policy":
                  "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data: blob:; "
                  "media-src 'self' blob:; connect-src 'self'; worker-src 'self'; frame-src 'self'; frame-ancestors 'none'"})

    def arquivo(self):
        a = ler_anexo(self.q.get("id", [""])[0])
        if not a or not os.path.exists(a["caminho"]):
            return self.resp(404, {"erro": "file no longer exists"})
        tam = os.path.getsize(a["caminho"])
        self.send_response(200)
        tipo = a["tipo"] if a["tipo"].startswith(("image/", "video/", "audio/", "text/plain", "application/pdf")) else "application/octet-stream"
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(tam))
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + urllib.parse.quote(a["nome"]))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox")
        self.end_headers()
        with open(a["caminho"], "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def servir_local(self, p):
        ext = p.suffix.lower()
        tipo = TIPOS_EXTRA.get(ext) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        doc = tipo in ("text/html", "image/svg+xml", "application/xhtml+xml")
        extra = AUTO_ALTURA if tipo == "text/html" else b""
        tam = p.stat().st_size
        ini, fim, codigo = 0, tam - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and not extra and (m.group(1) or m.group(2)):
            if m.group(1):
                ini = int(m.group(1))
                fim = min(int(m.group(2)) if m.group(2) else tam - 1, tam - 1)
            else:
                ini = max(0, tam - int(m.group(2)))
            if ini > fim:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{tam}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            codigo = 206
        n = max(0, fim - ini + 1)
        self.send_response(codigo)
        self.send_header("Content-Type", tipo + ("; charset=utf-8" if tipo.startswith("text/") else ""))
        self.send_header("Content-Length", str(n + len(extra)))
        self.send_header("Accept-Ranges", "bytes")
        if codigo == 206:
            self.send_header("Content-Range", f"bytes {ini}-{fim}/{tam}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store" if doc else "private, max-age=600")
        if tipo != "application/pdf":
            self.send_header("Content-Security-Policy", CSP_DOC if doc else "sandbox")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            f.seek(ini)
            resta = n
            while resta > 0:
                b = f.read(min(1 << 20, resta))
                if not b:
                    break
                self.wfile.write(b)
                resta -= len(b)
        if extra:
            self.wfile.write(extra)

    def sse(self, ex, desde):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            i = max(desde, int(self.headers.get("Last-Event-ID") or 0), 0)
        except ValueError:
            i = max(desde, 0)
        while True:
            with ex.cond:
                while i >= len(ex.eventos) and not ex.acabou:
                    if not ex.cond.wait(15):
                        break
                novos = ex.eventos[i:]
                acabou = ex.acabou
            if not novos:
                self.wfile.write(b": ping\n\n")
            for ev in novos:
                i += 1
                self.wfile.write(f"id: {i}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            if acabou and i >= len(ex.eventos):
                return

    def api(self, metodo, p):
        partes = p.strip("/").split("/")[1:]
        if partes == ["estado"]:
            contas = [{**c, "status": (_status_contas.get(c["id"]) or {}).get("s")} for c in CFG["contas"]]
            return self.resp(200, {"versao": VERSAO, "atualizacao": resumo_atualizacao(), "windows": WIN,
                                   "log_atualizacao": str(ATUALIZA_LOG), "contas": contas, "chats": listar_chats(), "limites": LIMITES,
                                   "padrao": CFG["padrao"], "local": self.local, "ips": info_rede()["ips"], "porta": PORTA,
                                   "home": str(HOME), "tls": bool(TLS["ctx"]), "seguro": self.tls,
                                   "pedidos": pk_pendentes(), "root": PK["ok"], "fixados": CFG.get("fixados") or [],
                                   "prompts": CFG.get("prompts") or [],
                                   "principal": (conta_principal() or {}).get("id", ""),
                                   "https": f"https://{(self.headers.get('Host') or '').rsplit(':', 1)[0]}:{PORTA}/"})
        if partes == ["busca"]:
            return self.resp(200, buscar(self.q.get("q", [""])[0]))
        if partes == ["pedido"] and metodo == "POST":
            d = self.corpo_json()
            with PK_TRAVA:
                p = PEDIDOS.get(str(d.get("id")))
                if p and p.get("ctl"):
                    if p["pub"]["estado"] != "pendente":
                        raise ValueError("this request is no longer waiting")
                    ctl_responde(p, d)
                    return self.resp(200, {"ok": True})
            pk_responde(d.get("id"), d.get("acao"), d.get("senha"), self.tls or self.local)
            d = None
            return self.resp(200, {"ok": True})
        if partes == ["pastas", "criar"] and metodo == "POST":
            d = self.corpo_json()
            base = Path(os.path.expanduser(str(d.get("base") or "")))
            nome = str(d.get("nome") or "").strip()
            if not base.is_absolute() or not base.is_dir():
                raise ValueError("the current folder does not exist")
            if not nome or nome in (".", "..") or "/" in nome or "\0" in nome or len(nome.encode()) > 255:
                raise ValueError("invalid folder name")
            novo = base / nome
            try:
                novo.mkdir()
            except FileExistsError:
                if not novo.is_dir():
                    raise ValueError("a file with this name already exists")
            except PermissionError:
                raise ValueError("no permission to create a folder here")
            return self.resp(200, {"caminho": str(novo)})
        if partes == ["preview"] and metodo == "POST":
            d = self.corpo_json()
            return self.resp(200, {"url": salva_preview(d.get("codigo"), d.get("tipo", "html"))})
        if partes == ["assinar"] and metodo == "POST":
            out = {}
            for c in (self.corpo_json().get("caminhos") or [])[:200]:
                i = info_local(c)
                if i:
                    out[c] = i
            return self.resp(200, out)
        if partes == ["prompts"] and metodo == "POST":
            return self.resp(200, {"prompts": salvar_prompts(self.corpo_json().get("prompts"))})
        if partes == ["fixar"] and metodo == "POST":
            d = self.corpo_json()
            return self.resp(200, {"fixados": fixar(d.get("id"), bool(d.get("fixo")))})
        if partes == ["atualizacao"]:
            d = checar_atualizacao(self.q.get("forcar", ["0"])[0] == "1")
            return self.resp(200, {**d, **resumo_atualizacao()})
        if partes == ["atualizar"] and metodo == "POST":
            atualizar()
            return self.resp(200, {"ok": True})
        if partes == ["titulos"] and metodo == "POST":
            with trava:
                novo = not NOMEANDO["rodando"]
                if novo:
                    NOMEANDO.update(rodando=True, feitos=0, tentados=0, total=0)
            if novo:
                threading.Thread(target=nomear_antigos, daemon=True).start()
            return self.resp(200, NOMEANDO)
        if partes == ["titulos"]:
            return self.resp(200, NOMEANDO)
        if partes == ["preferencias"] and metodo == "POST":
            d = self.corpo_json()
            with trava:
                for k in ("dica", "root", "titulos", "troca_auto", "memoria_unica"):
                    if k in d:
                        CFG["padrao"][k] = bool(d[k])
                if d.get("atualizar") in ("auto", "avisar", "nao"):
                    CFG["padrao"]["atualizar"] = d["atualizar"]
                    ATUALIZA["erro"] = ""
                salvar_json(CFG_FILE, CFG)
            if d.get("memoria_unica"):
                threading.Thread(target=unir_memorias, daemon=True).start()
            return self.resp(200, CFG["padrao"])
        if partes == ["celular", "passe"] and metodo == "POST":
            if self.quem != "dispositivo":
                raise ValueError("only paired phones use this")
            for k in [k for k, v in PASSES.items() if v[1] < time.time()]:
                PASSES.pop(k, None)
            code = secrets.token_urlsafe(18)
            PASSES[code] = (self.headers.get("X-Token", ""), time.time() + 180)
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return self.resp(200, {"url": f"https://{host}:{PORTA}/?passe={code}"})
        if partes == ["uso"]:
            return self.resp(200, uso_geral(self.q.get("forcar", [""])[0]))
        if partes == ["chats", "apagar"] and metodo == "POST":
            d = self.corpo_json()
            return self.resp(200, apagar_chats(d.get("ids") or [], bool(d.get("arquivos")), bool(d.get("sessoes"))))
        if partes == ["chats"] and metodo == "POST":
            return self.resp(200, novo_chat(self.corpo_json()))
        if len(partes) >= 2 and partes[0] == "chats":
            cid = partes[1]
            acao = partes[2] if len(partes) > 2 else ""
            if acao == "" and metodo == "GET":
                c = ler_chat(cid)
                ex = EXECS.get(cid)
                c["rodando"] = bool(ex and not ex.acabou)
                c["vivo"] = ex.msg.get("convidado") if c["rodando"] else None
                c["totais"] = totais(c)
                return self.resp(200, c)
            if acao == "eventos":
                ex = EXECS.get(cid)
                if not ex:
                    return self.resp(404, {"erro": "nothing running"})
                return self.sse(ex, int(self.q.get("desde", ["0"])[0]))
            if metodo != "POST":
                return self.resp(405, {"erro": "method not allowed"})
            if acao == "enviar":
                d = self.corpo_json()
                if not (d.get("texto") or "").strip() and not d.get("anexos"):
                    raise ValueError("empty message")
                with trava:
                    ex = EXECS.get(cid)
                    if ex and not ex.acabou:
                        c = ler_chat(cid)
                        c.setdefault("fila", []).append({"texto": d.get("texto", ""), "ts": time.time(),
                                                         "anexos": [a for a in (d.get("anexos") or []) if ler_anexo(a)]})
                        gravar_chat(c)
                        return self.resp(200, {"ok": True, "fila": len(c["fila"])})
                enviar(cid, d.get("texto", ""), d.get("anexos"))
                return self.resp(200, {"ok": True})
            if acao == "convidar":
                ex = convidar(cid, self.corpo_json())
                return self.resp(200, {"ok": True, "rodando": bool(ex)})
            if acao == "convidado":
                return self.resp(200, config_convidado(cid, self.corpo_json()))
            if acao == "debate":
                # end the debate after the reply that is running now
                with trava:
                    c = ler_chat(cid)
                    c.pop("debate", None)
                    gravar_chat(c)
                return self.resp(200, {"ok": True})
            if acao == "fila":
                d = self.corpo_json()
                i = int(d.get("i", -1))
                with trava:
                    c = ler_chat(cid)
                    f = c.get("fila") or []
                    if not 0 <= i < len(f):
                        raise ValueError("queued message not found")
                    ex = EXECS.get(cid)
                    if d.get("acao") == "enviar" and ex and not ex.acabou:
                        raise ValueError("wait for the current reply to finish")
                    item = f.pop(i)
                    c["fila"] = f
                    gravar_chat(c)
                if d.get("acao") == "enviar":
                    enviar(cid, item.get("texto", ""), item.get("anexos"))
                return self.resp(200, {"ok": True, "fila": f})
            if acao == "parar":
                ex = EXECS.get(cid)
                if ex and not ex.acabou:
                    threading.Thread(target=ex.parar, daemon=True).start()
                return self.resp(200, {"ok": True})
            if acao == "config":
                d = self.corpo_json()
                with trava:
                    c = ler_chat(cid)
                    ag = d.get("agente")
                    if ag in ("claude", "agy") and ag != c["agente"] and not c.get("sessao") and not c["mensagens"]:
                        c["agente"] = ag
                        c["conta"] = next(x["id"] for x in CFG["contas"] if x["agente"] == ag)
                        c["modelo"], c["effort"] = "", ""
                        c["perm"] = CFG["padrao"]["perm_claude"] if ag == "claude" else CFG["padrao"]["perm_agy"]
                    for k in ("titulo", "modelo", "perm", "effort"):
                        if k in d:
                            c[k] = str(d[k])[:200]
                    if "titulo" in d:
                        c["titulo_auto"] = False
                        c.pop("renomear_cli", None)
                        c.pop("nomear_depois", None)
                    if any(k in d for k in ("titulo", "modelo", "perm", "effort", "cwd")):
                        c["ajustado"] = True
                    if "titulo" in d and c.get("sessao") and not c.get("fork") and conta_por_id(c["conta"]):
                        try:
                            renomear_sessao(conta_por_id(c["conta"]), c["sessao"], c["titulo"])
                        except ValueError:
                            pass
                    if "cwd" in d and (not c.get("sessao") or c["agente"] == "claude"):
                        novo = os.path.expanduser(str(d["cwd"]))
                        if not os.path.isdir(novo):
                            raise ValueError("folder not found")
                        c["cwd"] = novo
                    if not c.get("sessao"):
                        if "conta" in d:
                            ct = conta_por_id(d["conta"])
                            if ct and ct["agente"] == c["agente"]:
                                c["conta"] = ct["id"]
                    gravar_chat(c)
                return self.resp(200, c)
            if acao == "apagar":
                r = apagar_chats([cid], bool(self.corpo_json().get("arquivos")))
                if r["pulados"]:
                    raise ValueError("stop the chat before deleting it")
                return self.resp(200, {"ok": True, **r})
            if acao == "duplicar":
                c = ler_chat(cid)
                n = novo_chat({**{k: c[k] for k in ("agente", "conta", "modelo", "perm", "effort", "cwd")},
                               "ajustado": True})
                return self.resp(200, n)
            if acao == "reenviar":
                d = self.corpo_json()
                reenviar(cid, d.get("indice"), d.get("texto"))
                return self.resp(200, {"ok": True})
            if acao == "titulo":
                c = ler_chat(cid)
                if not c.get("mensagens"):
                    raise ValueError("send a message first")
                if not _contas_titulo(c):
                    raise ValueError("no Claude account can make a title now (all of them reached their limit)")
                if not nomear_chat(cid, condicao=lambda c: True):
                    raise ValueError("Claude could not make a title right now. Try again in a moment")
                return self.resp(200, ler_chat(cid))
            if acao == "conta":
                return self.resp(200, mover_conta(cid, self.corpo_json().get("conta")))
            if acao == "bifurcar":
                return self.resp(200, bifurcar(cid, self.corpo_json().get("indice")))
            if acao == "permitidos":
                with trava:
                    c = ler_chat(cid)
                    tira = self.corpo_json().get("tirar")
                    c["permitidos"] = [x for x in (c.get("permitidos") or []) if tira not in (None, x)]
                    gravar_chat(c)
                ex = EXECS.get(cid)
                if ex and not ex.acabou and not ex.conv:
                    ex.chat["permitidos"] = list(c["permitidos"])
                return self.resp(200, {"permitidos": c["permitidos"]})
        if partes == ["upload"] and metodo == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 8 * 1024 ** 3:
                raise ValueError("invalid size (max 8 GB)")
            return self.resp(200, salvar_upload(self.q.get("nome", ["arquivo"])[0], n, self.rfile))
        if partes == ["contas", "status"]:
            forcar = self.q.get("forcar", ["0"])[0] == "1"
            res = {}
            ths = []
            for c in CFG["contas"]:
                def f(c=c):
                    res[c["id"]] = status_conta(c, forcar)
                t = threading.Thread(target=f)
                t.start()
                ths.append(t)
            for t in ths:
                t.join(40)
            return self.resp(200, res)
        if partes == ["contas", "login"] and metodo == "POST":
            if not self.local:
                raise ValueError("sign in opens a window on the PC, do it from the PC")
            c = conta_por_id(self.corpo_json().get("id"))
            if not c:
                raise ValueError("account not found")
            _status_contas.pop(c["id"], None)
            abrir_login(c)
            return self.resp(200, {"ok": True})
        if partes == ["contas", "nova"] and metodo == "POST":
            d = self.corpo_json()
            if d.get("agente") != "claude":
                raise ValueError("only Claude accounts can be added (agy uses a single Google account)")
            cid = re.sub(r"[^a-z0-9]", "", (d.get("nome") or "").lower())[:20] or secrets.token_hex(3)
            if conta_por_id(cid):
                cid += secrets.token_hex(2)
            with trava:
                CFG["contas"].append({"id": cid, "agente": "claude", "nome": d.get("nome") or cid,
                                      "dir": str(CFG_DIR / "contas" / cid)})
                salvar_json(CFG_FILE, CFG)
            return self.resp(200, {"ok": True, "id": cid})
        if partes == ["contas", "editar"] and metodo == "POST":
            d = self.corpo_json()
            c = conta_por_id(d.get("id"))
            if not c:
                raise ValueError("account not found")
            with trava:
                if d.get("remover"):
                    if c["id"] in ("claude1", "google"):
                        raise ValueError("this is the CLI default account and cannot be removed")
                    CFG["contas"].remove(c)
                elif d.get("nome"):
                    c["nome"] = d["nome"][:40]
                salvar_json(CFG_FILE, CFG)
            return self.resp(200, {"ok": True})
        if partes == ["cli", "sessoes"]:
            c = conta_por_id(self.q.get("conta", [""])[0])
            if not c:
                raise ValueError("account not found")
            return self.resp(200, sessoes_cli(c))
        if partes == ["cli", "lista"]:
            return self.resp(200, lista_cli())
        if partes == ["cli", "renomear"] and metodo == "POST":
            d = self.corpo_json()
            c = conta_por_id(d.get("conta"))
            if not c:
                raise ValueError("account not found")
            return self.resp(200, {"titulo": renomear_sessao(c, d.get("id", ""), d.get("titulo"))})
        if partes == ["cli", "apagar"] and metodo == "POST":
            feitos, livres, erros = 0, 0, []
            for x in (self.corpo_json().get("itens") or [])[:500]:
                c = conta_por_id(x.get("conta"))
                try:
                    if not c:
                        raise ValueError("account not found")
                    livres += apagar_sessao(c, x.get("id", ""))
                    feitos += 1
                except ValueError as e:
                    erros.append(f"{x.get('titulo') or x.get('id')}: {e}")
            return self.resp(200, {"apagados": feitos, "liberado": livres, "erros": erros})
        if partes == ["cli", "importar"] and metodo == "POST":
            d = self.corpo_json()
            c = conta_por_id(d.get("conta"))
            if not c:
                raise ValueError("account not found")
            return self.resp(200, importar_cli(c, d.get("id", "")))
        if partes == ["modelos"]:
            if self.q.get("agente", [""])[0] == "agy":
                c = next(c for c in CFG["contas"] if c["agente"] == "agy")
                return self.resp(200, status_conta(c).get("modelos", []))
            return self.resp(200, [{"id": "", "nome": "Account default"}, {"id": "opus", "nome": "Opus (alias)"},
                                   {"id": "sonnet", "nome": "Sonnet (alias)"}, {"id": "haiku", "nome": "Haiku (alias)"},
                                   {"id": "claude-fable-5-2", "nome": "Fable 5.2"}, {"id": "claude-opus-5-5", "nome": "Opus 5.5"},
                                   {"id": "claude-sonnet-5-5", "nome": "Sonnet 5.5"}, {"id": "claude-haiku-5-5", "nome": "Haiku 5.5"}])
        if partes == ["celular", "pin"] and metodo == "POST":
            if not self.local:
                raise ValueError("only the PC can generate a PIN")
            return self.resp(200, novo_pin())
        if partes == ["celular", "dispositivos"]:
            return self.resp(200, [{"id": d["token"][:8], "nome": d["nome"], "ip": d["ip"], "criado": d["criado"],
                                    "visto": d.get("visto")} for d in CFG["dispositivos"]])
        if partes == ["celular", "revogar"] and metodo == "POST":
            alvo = str(self.corpo_json().get("id") or "")
            with trava:
                antes = len(CFG["dispositivos"])
                CFG["dispositivos"] = [d for d in CFG["dispositivos"] if d["token"][:8] != alvo]
                if len(CFG["dispositivos"]) == antes:
                    raise ValueError("device not found")
                salvar_json(CFG_FILE, CFG)
            return self.resp(200, {"ok": True})
        if partes == ["pastas"]:
            base = Path(os.path.expanduser(self.q.get("de", [str(HOME)])[0]))
            if not base.is_dir():
                base = HOME
            subs = sorted([x.name for x in base.iterdir() if x.is_dir() and not x.name.startswith(".")])[:300]
            return self.resp(200, {"base": str(base), "pai": str(base.parent), "subs": subs})
        return self.resp(404, {"erro": "route not found"})


def encerrar(*_):
    for ex in list(EXECS.values()):
        if not ex.acabou and ex.proc and ex.proc.poll() is None:
            ex.parado = True
            mata_arvore(ex.proc)
    t0 = time.time()
    while time.time() - t0 < 3 and any(not e.acabou for e in EXECS.values()):
        time.sleep(0.1)
    for ex in list(EXECS.values()):
        mata_arvore(ex.proc, forte=True)
    if PK["ok"]:
        try:
            PK_SOCK.unlink()
        except OSError:
            pass
    os._exit(0)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--update"]:
        try:
            atualizar_terminal()
        except ValueError as e:
            sys.exit(f"agents-web: {e}")
        sys.exit(0)
    signal.signal(signal.SIGTERM, encerrar)
    signal.signal(signal.SIGINT, encerrar)
    if not WIN:
        signal.signal(signal.SIGHUP, encerrar)
    if OPENSSL:
        garante_tls(info_rede()["ips"])
    # the in-page root approvals wrap pkexec with a small script run by the system's /usr/bin/python3
    if not WIN and shutil.which("pkttyagent") and (PK_BIN / "pkexec").exists() and os.path.exists("/usr/bin/python3"):
        threading.Thread(target=pk_servidor, daemon=True).start()
    threading.Thread(target=lambda: lista_cli(), daemon=True).start()
    threading.Thread(target=limpar_vazios, daemon=True).start()
    threading.Thread(target=unir_memorias, daemon=True).start()
    threading.Thread(target=vigia_atualizacoes, daemon=True).start()
    srv = Servidor(("0.0.0.0", PORTA), H)
    print(f"Agents Web {VERSAO} on http://127.0.0.1:{PORTA}", flush=True)
    srv.serve_forever()
