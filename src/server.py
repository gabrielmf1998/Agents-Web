#!/usr/bin/env python3
# Agents Web: a local web interface for the Claude Code CLI and the Antigravity CLI (agy).
# Python standard library only. Listens on 0.0.0.0 (IPv4) but only accepts loopback and the
# local network (zt*, docker, VPN and similar interfaces are left out). Phones pair with a PIN.
# https://github.com/gabrielmf1998/Agents-Web (MIT license)
import json, os, sys, time, threading, subprocess, signal, secrets, re, ipaddress
import shutil, uuid, urllib.parse, http.server, socketserver, html, mimetypes
import ssl, hmac, hashlib, socket, unicodedata, struct, shlex, sqlite3
from pathlib import Path

VERSAO = "1.3.1"
BASE = Path(__file__).resolve().parent
HOME = Path.home()
CFG_DIR = HOME / ".config/agents-web"
DATA = HOME / ".local/share/agents-web"
CHATS = DATA / "chats"
UPLOADS = DATA / "uploads"
CFG_FILE = CFG_DIR / "config.json"
PORTA = int(os.environ.get("AGENTS_WEB_PORT", "47830"))
CLAUDE = shutil.which("claude") or str(HOME / ".local/bin/claude")
AGY = shutil.which("agy") or str(HOME / ".local/bin/agy")

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
        os.replace(tmp, caminho)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def carregar_config():
    cfg = {}
    if CFG_FILE.exists():
        try:
            cfg = json.loads(CFG_FILE.read_text())
        except Exception:
            cfg = {}
    cfg.setdefault("token", secrets.token_urlsafe(32))
    cfg.setdefault("dispositivos", [])
    cfg.setdefault("contas", [
        {"id": "claude1", "agente": "claude", "nome": "Claude", "dir": ""},
        {"id": "google", "agente": "agy", "nome": "Antigravity", "dir": ""},
    ])
    cfg.setdefault("padrao", {"cwd": str(HOME), "perm_claude": "bypassPermissions", "perm_agy": "skip"})
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
    env["PATH"] = str(HOME / ".local/bin") + ":" + env.get("PATH", "/usr/bin")
    if conta and conta["agente"] == "claude" and conta.get("dir"):
        Path(conta["dir"]).mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = conta["dir"]
    return env


def dir_claude(conta):
    return Path(conta["dir"]) if conta.get("dir") else HOME / ".claude"


# ---------------------------------------------------------------- network
_rede = {"t": 0, "redes": [], "ips": []}


def info_rede():
    if time.time() - _rede["t"] < 30:
        return _rede
    redes, ips = [], []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        for linha in out.splitlines():
            p = linha.split()
            nome, cidr = p[1], p[3]
            if re.match(r"^(lo|zt|docker|veth|br-|virbr|tun|tap|wg|tailscale)", nome):
                continue
            itf = ipaddress.ip_interface(cidr)
            if not itf.ip.is_private:
                continue
            redes.append(itf.network)
            ips.append(str(itf.ip))
    except Exception:
        pass
    _rede.update(t=time.time(), redes=redes, ips=ips)
    if TLS["ips"] is not None and sorted(set(ips)) != TLS["ips"]:
        threading.Thread(target=garante_tls, args=(ips,), daemon=True).start()
    return _rede


# ---------------------------------------------------------------- https on the same port
# Own CA limited by nameConstraints to private IPs and localhost: even when installed on a
# phone it cannot sign any internet site.
TLS_DIR = CFG_DIR / "tls"
TLS = {"ctx": None, "ips": None, "erro": ""}
_tls_trava = threading.Lock()


def _ossl(*args):
    r = subprocess.run(["openssl", *args], capture_output=True, text=True, timeout=30)
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
                valido = subprocess.run(["openssl", "x509", "-checkend", str(30 * 86400), "-noout", "-in", str(sc)],
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
        if sessoes and c.get("sessao") and conta_por_id(c.get("conta", "")):
            try:
                livres += apagar_sessao(conta_por_id(c["conta"]), c["sessao"])
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
    gravar_chat(chat)
    return chat


def resumo_chat(c):
    tot = totais(c)
    return {"id": c["id"], "titulo": c["titulo"], "agente": c["agente"], "conta": c["conta"],
            "modelo": c.get("modelo", ""), "atualizado": c.get("atualizado", 0), "n": len(c["mensagens"]),
            "tokens": tot["total"], "fila": len(c.get("fila") or []), "sessao": c.get("sessao", ""),
            "cwd": c.get("cwd", ""), "origem": c.get("origem", ""),
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
        "text answer complete on its own. The frame already carries the interface's dark theme and font: leave the page "
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
PK_SOCK = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "agents-web-pk.sock"
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


class Execucao:
    def __init__(self, chat, texto, anexos):
        self.chat_id = chat["id"]
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
        self.inicio = time.time()
        self.pk = secrets.token_hex(16)

    def emite(self, ev):
        with self.cond:
            self.eventos.append(ev)
            self.cond.notify_all()

    def comando(self):
        c = self.chat
        if c["agente"] == "claude":
            cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
            if c.get("modelo"):
                cmd += ["--model", c["modelo"]]
            if c.get("perm"):
                cmd += ["--permission-mode", c["perm"]]
            if c.get("effort"):
                cmd += ["--effort", c["effort"]]
            if c.get("sessao"):
                cmd += ["--resume", c["sessao"]]
            extra = [DICA] if CFG["padrao"].get("dica", True) else []
            if pk_ativo():
                extra.append(DICA_PK)
            if extra:
                cmd += ["--append-system-prompt", " ".join(extra)]
            return cmd, "stdin"
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
        # setpriv --pdeathsig: if this server dies, the agent dies with it
        cmd = ["setpriv", "--pdeathsig", "KILL", "--"] + cmd
        env = env_conta(conta)
        if pk_ativo():
            PK_TOKENS[self.pk] = self
            env.update(PATH=f"{PK_BIN}:{env['PATH']}", AGW_PK=self.pk, AGW_PK_SOCK=str(PK_SOCK))
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True,
                                     stdin=subprocess.PIPE if modo == "stdin" else subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.erros = b""

        def le_erro():
            for linha in self.proc.stderr:
                self.erros = (self.erros + linha)[-6000:]
        threading.Thread(target=le_erro, daemon=True).start()
        if modo == "stdin":
            try:
                self.proc.stdin.write(texto.encode())
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
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
        rc = self.proc.wait()
        if rc != 0 and not self.msg["texto"] and not self.msg["erro"] and not self.parado:
            self.msg["erro"] = f"exited with code {rc}: " + self.erros.decode(errors="replace")[-1500:]
            self.emite({"t": "erro", "s": self.msg["erro"]})

    # ---- Claude Code stream-json
    def parse_claude(self, d):
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
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
            for b in (d.get("message", {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    cont = b.get("content")
                    if isinstance(cont, list):
                        cont = "\n".join(x.get("text", "[" + x.get("type", "?") + "]") for x in cont if isinstance(x, dict))
                    f = self.ferr.get(b.get("tool_use_id"))
                    if f:
                        f.update(saida=corta(cont or "", 6000), erro=bool(b.get("is_error")), fim=True)
                        self.emite({"t": "resultado", "id": f["id"], "saida": f["saida"], "erro": f["erro"]})
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
                self.msg["erro"] = str(d.get("result") or d.get("subtype") or "erro")
            elif not self.msg["texto"] and d.get("result"):
                self.msg["texto"] = d["result"]
            if d.get("permission_denials"):
                self.msg["negadas"] = [x.get("tool_name") for x in d["permission_denials"]]
            if d.get("session_id"):
                self.chat["sessao"] = d["session_id"]

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

    def parar(self):
        self.parado = True
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                time.sleep(1.5)
                if self.proc.poll() is None:
                    os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def finalizar(self):
        PK_TOKENS.pop(self.pk, None)
        with PK_TRAVA:
            for p in PEDIDOS.values():
                if p["ex"] is self and p["pub"]["estado"] == "pendente":
                    p["resp"] = {"r": "negar", "msg": "the reply ended before an answer"}
                    pk_muda(p, "cancelado")
                    p["ev"].set()
        self.msg["duracao"] = round(time.time() - self.inicio, 1)
        self.msg["texto"] = self.msg["texto"].strip()
        if self.parado:
            self.msg["erro"] = (self.msg["erro"] + " " if self.msg["erro"] else "") + "(stopped)"
        with trava:
            try:
                atual = ler_chat(self.chat_id)
                atual["sessao"] = self.chat.get("sessao", atual.get("sessao", ""))
                atual["mensagens"].append(self.msg)
                gravar_chat(atual)
            except FileNotFoundError:
                pass
        if not self.parado and not self.msg["erro"]:
            try:
                proxima_fila(self.chat_id, self)
            except Exception:
                pass
        with self.cond:
            self.acabou = True
            self.eventos.append({"t": "fim", "msg": self.msg})
            self.cond.notify_all()

        def solta():
            if EXECS.get(self.chat_id) is self:
                EXECS.pop(self.chat_id, None)
        t = threading.Timer(180, solta)
        t.daemon = True
        t.start()


def enviar(cid, texto, anexos_ids, substitui=None):
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
        chat["mensagens"].append(msg)
        if chat["titulo"] in ("New chat", "Novo chat") and texto.strip():
            chat["titulo"] = re.sub(r"\s+", " ", texto.strip())[:60]
        gravar_chat(chat)
        ex = Execucao(chat, texto, anexos)
        EXECS[cid] = ex
    threading.Thread(target=ex.rodar, daemon=True).start()
    return ex


def proxima_fila(cid, atual):
    # called by the run that ends, before the "fim" event, so whoever is watching reopens the
    # chat with the next queued message already running
    with trava:
        try:
            c = ler_chat(cid)
        except (FileNotFoundError, ValueError):
            return
        f = c.get("fila") or []
        if not f:
            return
        item = f.pop(0)
        c["fila"] = f
        gravar_chat(c)
        try:
            enviar(cid, item.get("texto", ""), item.get("anexos"), atual)
        except Exception:
            c = ler_chat(cid)
            c.setdefault("fila", []).insert(0, item)
            gravar_chat(c)


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
    ligados = {c.get("sessao") for c in listar_chats() if c.get("sessao")}
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
    allow_reuse_address = True

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
            if p == "/favicon.ico":
                return self.resp(204, b"", "image/x-icon")
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
            return self.resp(200, {"versao": VERSAO, "contas": contas, "chats": listar_chats(), "limites": LIMITES,
                                   "padrao": CFG["padrao"], "local": self.local, "ips": info_rede()["ips"], "porta": PORTA,
                                   "home": str(HOME), "tls": bool(TLS["ctx"]), "seguro": self.tls,
                                   "pedidos": pk_pendentes(), "root": PK["ok"], "fixados": CFG.get("fixados") or [],
                                   "https": f"https://{(self.headers.get('Host') or '').rsplit(':', 1)[0]}:{PORTA}/"})
        if partes == ["busca"]:
            return self.resp(200, buscar(self.q.get("q", [""])[0]))
        if partes == ["pedido"] and metodo == "POST":
            d = self.corpo_json()
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
        if partes == ["fixar"] and metodo == "POST":
            d = self.corpo_json()
            return self.resp(200, {"fixados": fixar(d.get("id"), bool(d.get("fixo")))})
        if partes == ["preferencias"] and metodo == "POST":
            d = self.corpo_json()
            with trava:
                for k in ("dica", "root"):
                    if k in d:
                        CFG["padrao"][k] = bool(d[k])
                salvar_json(CFG_FILE, CFG)
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
                    if "titulo" in d and c.get("sessao") and conta_por_id(c["conta"]):
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
                n = novo_chat({k: c[k] for k in ("agente", "conta", "modelo", "perm", "effort", "cwd")})
                return self.resp(200, n)
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
            try:
                os.killpg(ex.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    t0 = time.time()
    while time.time() - t0 < 3 and any(not e.acabou for e in EXECS.values()):
        time.sleep(0.1)
    for ex in list(EXECS.values()):
        if ex.proc and ex.proc.poll() is None:
            try:
                os.killpg(ex.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    os._exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, encerrar)
    signal.signal(signal.SIGINT, encerrar)
    signal.signal(signal.SIGHUP, encerrar)
    if shutil.which("openssl"):
        garante_tls(info_rede()["ips"])
    if shutil.which("pkttyagent") and (PK_BIN / "pkexec").exists():
        threading.Thread(target=pk_servidor, daemon=True).start()
    threading.Thread(target=lambda: lista_cli(), daemon=True).start()
    srv = Servidor(("0.0.0.0", PORTA), H)
    print(f"Agents Web {VERSAO} on http://127.0.0.1:{PORTA}", flush=True)
    srv.serve_forever()
