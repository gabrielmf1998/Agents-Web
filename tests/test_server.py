#!/usr/bin/env python3
"""End to end tests for src/server.py (Python standard library only).

Each run starts the real server in a throwaway HOME, on a free port, with a fake `claude` binary
that speaks the stream-json protocol. Nothing touches your real chats, logins or quota.

    python3 -m unittest discover -s tests -v
"""
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.parse
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SERVIDOR = RAIZ / "src" / "server.py"
SID = "0b6f3c2e-6d55-4f3e-9a51-1f2e3d4c5b6a"

# A tiny stand-in for the Claude Code CLI. It logs its argv, then answers in stream-json.
FAKE_CLAUDE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    args = sys.argv[1:]
    with open(os.environ["HOME"] + "/claude-args.jsonl", "a") as f:
        f.write(json.dumps(args) + "\\n")
    if args[:2] == ["auth", "status"]:
        print(json.dumps({{"loggedIn": True, "email": "test@example.invalid", "subscriptionType": "pro"}}))
        sys.exit(0)
    prompt = sys.stdin.read()
    out = lambda d: (print(json.dumps(d)), sys.stdout.flush())
    out({{"type": "system", "subtype": "init", "session_id": "{SID}", "model": "claude-fake-1",
         "permissionMode": "bypassPermissions"}})
    if "slow" in prompt:
        time.sleep(1.5)
    if "hang" in prompt:
        time.sleep(60)
    reply = "echo: " + prompt.strip().splitlines()[0]
    out({{"type": "stream_event", "event": {{"type": "content_block_delta",
         "delta": {{"type": "text_delta", "text": reply}}}}}})
    out({{"type": "assistant", "message": {{"id": "msg1", "content": [{{"type": "text", "text": reply}}],
         "usage": {{"input_tokens": 7, "output_tokens": 3}}}}}})
    out({{"type": "result", "usage": {{"input_tokens": 7, "output_tokens": 3}}, "total_cost_usd": 0.001,
         "num_turns": 1, "session_id": "{SID}"}})
""")


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Servidor:
    """The real server, started in a temporary HOME."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agents-web-test-"))
        self.home = self.tmp / "home"
        binarios = self.home / ".local/bin"
        binarios.mkdir(parents=True)
        fake = binarios / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        run = self.tmp / "run"
        run.mkdir(mode=0o700)
        self.porta = porta_livre()
        env = {"HOME": str(self.home), "PATH": f"{binarios}:/usr/bin:/bin", "XDG_RUNTIME_DIR": str(run),
               "AGENTS_WEB_PORT": str(self.porta), "LANG": "C.UTF-8"}
        self.log = open(self.tmp / "server.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-I", "-u", str(SERVIDOR)], env=env, cwd=str(self.tmp),
                                     stdout=self.log, stderr=subprocess.STDOUT)
        fim = time.time() + 30
        while time.time() < fim:
            try:
                if self.pedir("GET", "/saude")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.parar()
            raise RuntimeError("server did not start:\n" + (self.tmp / "server.log").read_text())
        self.token = json.loads((self.home / ".config/agents-web/config.json").read_text())["token"]

    def pedir(self, metodo, caminho, corpo=None, cab=None, tls=False):
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            c = http.client.HTTPSConnection("127.0.0.1", self.porta, timeout=20, context=ctx)
        else:
            c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=20)
        try:
            h = dict(cab or {})
            if isinstance(corpo, (dict, list)):
                corpo = json.dumps(corpo).encode()
                h.setdefault("Content-Type", "application/json")
            c.request(metodo, caminho, body=corpo, headers=h)
            r = c.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            c.close()

    def api(self, caminho, corpo=None, esperado=200):
        st, b, _ = self.pedir("POST" if corpo is not None else "GET", caminho, corpo, {"X-Token": self.token})
        if st != esperado:
            raise AssertionError(f"{caminho}: HTTP {st} (expected {esperado}): {b[:300]!r}")
        return json.loads(b or b"null")

    def eventos(self, cid, limite=20):
        """Read the Server Sent Events of a running reply until it ends."""
        c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=limite)
        try:
            c.request("GET", f"/api/chats/{cid}/eventos?desde=0", headers={"X-Token": self.token})
            r = c.getresponse()
            if r.status == 404:
                return []
            out = []
            for linha in r:
                if linha.startswith(b"data: "):
                    ev = json.loads(linha[6:])
                    out.append(ev)
                    if ev["t"] == "fim":
                        return out
            return out
        finally:
            c.close()

    def espera_parado(self, cid, limite=20):
        fim = time.time() + limite
        while time.time() < fim:
            c = self.api(f"/api/chats/{cid}")
            if not c["rodando"]:
                return c
            time.sleep(0.1)
        raise AssertionError("the reply never ended")

    def args_claude(self):
        p = self.home / "claude-args.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def parar(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(shutil.which("setpriv"), "needs setpriv (util-linux)")
class TestServidor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Servidor()

    @classmethod
    def tearDownClass(cls):
        cls.s.parar()

    def novo_chat(self, **kw):
        return self.s.api("/api/chats", {"agente": "claude", **kw})

    # ---------------------------------------------------------------- access
    def test_pagina_no_pc_tem_token_e_csp(self):
        st, corpo, cab = self.s.pedir("GET", "/", cab={"Host": f"127.0.0.1:{self.s.porta}"})
        self.assertEqual(st, 200)
        self.assertIn(self.s.token.encode(), corpo)
        self.assertNotIn(b"__TOKEN__", corpo)
        self.assertIn("frame-ancestors 'none'", cab["Content-Security-Policy"])

    def test_api_sem_token(self):
        st, _, _ = self.s.pedir("GET", "/api/estado")
        self.assertEqual(st, 401)

    def test_token_errado_e_nao_ascii(self):
        for tok in ("errado", "çãø-ñ", "x" * 500):
            st, _, _ = self.s.pedir("GET", "/api/estado", cab={"X-Token": tok.encode("utf-8").decode("latin-1")})
            self.assertEqual(st, 401, tok)

    def test_host_estranho_bloqueado(self):
        # DNS rebinding: a website pointing its own name at this PC
        st, _, _ = self.s.pedir("GET", "/", cab={"Host": "evil.example.com"})
        self.assertEqual(st, 403)

    def test_estado(self):
        e = self.s.api("/api/estado")
        self.assertEqual(e["home"], str(self.s.home))
        self.assertTrue(any(c["id"] == "claude1" for c in e["contas"]))

    def test_https_na_mesma_porta(self):
        if not shutil.which("openssl"):
            self.skipTest("needs openssl")
        st, corpo, _ = self.s.pedir("GET", "/", tls=True)
        self.assertEqual(st, 200)
        self.assertIn(self.s.token.encode(), corpo)
        ca = self.s.home / ".config/agents-web/tls/ca.crt"
        txt = subprocess.run(["openssl", "x509", "-in", str(ca), "-noout", "-text"], capture_output=True, text=True).stdout
        self.assertIn("Name Constraints: critical", txt)

    def test_revogar_dispositivo_desconhecido(self):
        self.s.api("/api/celular/revogar", {"id": ""}, esperado=400)
        self.s.api("/api/celular/revogar", {"id": "a"}, esperado=400)

    # ---------------------------------------------------------------- chats and runs
    def test_conversa_completa_e_resume(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hello there"})
        evs = self.s.eventos(c["id"])
        tipos = [e["t"] for e in evs]
        self.assertIn("sistema", tipos)
        self.assertIn("texto", tipos)
        self.assertEqual(tipos[-1], "fim")
        chat = self.s.espera_parado(c["id"])
        self.assertEqual(chat["sessao"], SID)
        self.assertEqual(chat["titulo"], "hello there")
        resp = chat["mensagens"][-1]
        self.assertEqual(resp["papel"], "assistente")
        self.assertEqual(resp["texto"], "echo: hello there")
        self.assertEqual(resp["uso"]["entrada"], 7)
        self.assertEqual(chat["totais"]["total"], 10)
        # the second message resumes the CLI session
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "again"})
        self.s.espera_parado(c["id"])
        ultimo = self.s.args_claude()[-1]
        self.assertIn("--resume", ultimo)
        self.assertEqual(ultimo[ultimo.index("--resume") + 1], SID)
        self.assertIn("--permission-mode", ultimo)

    def test_fila(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "slow one"})
        r = self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "queued two"})
        self.assertEqual(r.get("fila"), 1)
        fim = time.time() + 20
        while time.time() < fim:
            chat = self.s.api(f"/api/chats/{c['id']}")
            if len(chat["mensagens"]) == 4 and not chat["rodando"]:
                break
            time.sleep(0.1)
        textos = [m["texto"] for m in chat["mensagens"]]
        self.assertEqual(textos, ["slow one", "echo: slow one", "queued two", "echo: queued two"])
        self.assertEqual(chat.get("fila"), [])

    def test_parar(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hang please"})
        time.sleep(0.5)
        self.s.api(f"/api/chats/{c['id']}/parar", {})
        chat = self.s.espera_parado(c["id"], 15)
        self.assertIn("(stopped)", chat["mensagens"][-1]["erro"])

    def test_mensagem_vazia(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "   "}, esperado=400)

    def test_config_e_pasta_invalida(self):
        c = self.novo_chat()
        r = self.s.api(f"/api/chats/{c['id']}/config", {"titulo": "Renamed", "effort": "high"})
        self.assertEqual((r["titulo"], r["effort"]), ("Renamed", "high"))
        self.s.api(f"/api/chats/{c['id']}/config", {"cwd": "/does/not/exist"}, esperado=400)

    def test_conta_removida_nao_usa_login_padrao(self):
        nova = self.s.api("/api/contas/nova", {"agente": "claude", "nome": "Extra Test"})
        c = self.novo_chat(conta=nova["id"])
        self.s.api("/api/contas/editar", {"id": nova["id"], "remover": True})
        antes = len(self.s.args_claude())
        r = self.s.pedir("POST", f"/api/chats/{c['id']}/enviar", {"texto": "hi"}, {"X-Token": self.s.token})
        self.assertEqual(r[0], 400)
        self.assertIn(b"removed", r[1])
        self.assertEqual(len(self.s.args_claude()), antes)

    def test_busca_duplicar_apagar(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "zebra unicorn"})
        self.s.espera_parado(c["id"])
        achados = self.s.api("/api/busca?q=" + urllib.parse.quote("UNICÓRN"))
        self.assertTrue(any(x["id"] == c["id"] for x in achados), "search ignores case and accents")
        d = self.s.api(f"/api/chats/{c['id']}/duplicar", {})
        self.assertEqual(d["mensagens"], [])
        r = self.s.api("/api/chats/apagar", {"ids": [c["id"], d["id"]]})
        self.assertEqual(r["apagados"], 2)
        self.s.api(f"/api/chats/{c['id']}", esperado=400)
        self.s.api("/api/chats/apagar", {"ids": ["../../etc"]}, esperado=400)

    # ---------------------------------------------------------------- files
    def test_pastas(self):
        base = self.s.home / "Projects"
        base.mkdir(exist_ok=True)
        r = self.s.api("/api/pastas/criar", {"base": str(base), "nome": "novo"})
        self.assertTrue(Path(r["caminho"]).is_dir())
        for ruim in ("..", "a/b", "", "."):
            self.s.api("/api/pastas/criar", {"base": str(base), "nome": ruim}, esperado=400)
        lista = self.s.api("/api/pastas?de=" + urllib.parse.quote(str(base)))
        self.assertIn("novo", lista["subs"])

    def test_upload_e_download(self):
        dados = os.urandom(4096)
        st, b, _ = self.s.pedir("POST", "/api/upload?nome=" + urllib.parse.quote("../../evil name.bin"), dados,
                                {"X-Token": self.s.token, "Content-Type": "application/octet-stream"})
        self.assertEqual(st, 200)
        a = json.loads(b)
        self.assertNotIn("/", a["nome"])
        self.assertTrue(a["caminho"].startswith(str(self.s.home / ".local/share/agents-web/uploads")))
        st, b, _ = self.s.pedir("GET", f"/api/arquivo?id={a['id']}&t={self.s.token}")
        self.assertEqual((st, b), (200, dados))

    def test_links_assinados(self):
        pasta = self.s.home / "Pictures/shots/2026"
        (pasta / "sub").mkdir(parents=True)
        (pasta / ".secret").mkdir()
        (pasta / "a.png").write_bytes(b"\x89PNG fake")
        (pasta / "sub/b.png").write_bytes(b"\x89PNG other")
        (pasta / ".secret/c.png").write_bytes(b"\x89PNG hidden")
        r = self.s.api("/api/assinar", {"caminhos": [str(pasta / "a.png"), "/etc/passwd", "relative.png"]})
        self.assertEqual(list(r), [str(pasta / "a.png")])
        url = r[str(pasta / "a.png")]["url"]
        self.assertEqual(self.s.pedir("GET", url)[0], 200)
        sig = url.split("/")[2]
        # the signature covers the folder and its visible subfolders, nothing else
        self.assertEqual(self.s.pedir("GET", f"/f/{sig}{urllib.parse.quote(str(pasta / 'sub/b.png'))}")[0], 200)
        self.assertEqual(self.s.pedir("GET", f"/f/{sig}{urllib.parse.quote(str(pasta / '.secret/c.png'))}")[0], 404)
        self.assertEqual(self.s.pedir("GET", f"/f/{sig}{urllib.parse.quote(str(pasta))}/../../../../etc/passwd")[0], 404)
        self.assertEqual(self.s.pedir("GET", f"/f/{'0' * 32}{urllib.parse.quote(str(pasta / 'a.png'))}")[0], 404)
        # ranges, for video seeking
        st, b, cab = self.s.pedir("GET", url, cab={"Range": "bytes=1-3"})
        self.assertEqual((st, b), (206, b"PNG"))

    def test_preview_isolado(self):
        r = self.s.api("/api/preview", {"codigo": "<h1>hi</h1><script>1</script>", "tipo": "html"})
        st, corpo, cab = self.s.pedir("GET", r["url"])
        self.assertEqual(st, 200)
        self.assertIn(b"<h1>hi</h1>", corpo)
        self.assertTrue(cab["Content-Security-Policy"].startswith("sandbox"))
        self.assertNotIn("allow-same-origin", cab["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
