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

# A tiny stand-in for the Claude Code CLI. It logs its argv and what it reads on stdin, then answers in
# stream-json, with the same control protocol as `claude -p --input-format stream-json
# --permission-prompt-tool stdio` (permission requests, questions, plans, interrupt).
FAKE_CLAUDE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    args = sys.argv[1:]
    H = os.environ["HOME"]
    def log(nome, d):
        with open(H + "/" + nome, "a") as f:
            f.write(json.dumps(d) + "\\n")
    if "--no-session-persistence" in args:   # the short call that names a chat
        entrada = sys.stdin.read()
        log("claude-titulo.jsonl", {{"args": args, "stdin": entrada, "conta": os.environ.get("CLAUDE_CONFIG_DIR", "")}})
        if "no title" in entrada:
            print(json.dumps({{"is_error": True, "result": "error"}}))
        else:
            print(json.dumps({{"type": "result", "is_error": False, "result": '"Fake title."'}}))
        sys.exit(0)
    log("claude-args.jsonl", args)
    if args[:2] == ["auth", "status"]:
        print(json.dumps({{"loggedIn": True, "email": "test@example.invalid", "subscriptionType": "pro"}}))
        sys.exit(0)
    def ler():
        l = sys.stdin.readline()
        if not l:
            return None
        d = json.loads(l)
        log("claude-stdin.jsonl", d)
        return d
    def out(d):
        print(json.dumps(d))
        sys.stdout.flush()
    def ate_fechar():
        while ler() is not None:
            pass
    def interrompido(d):
        if d.get("type") == "control_request" and d["request"].get("subtype") == "interrupt":
            out({{"type": "control_response", "response": {{"subtype": "success", "request_id": d["request_id"]}}}})
            out({{"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "{SID}"}})
            ate_fechar()
            sys.exit(0)
    def pede(nome, entrada, rid):
        out({{"type": "control_request", "request_id": rid, "request": {{"subtype": "can_use_tool",
             "tool_name": nome, "display_name": nome, "input": entrada, "tool_use_id": "tu-" + rid}}}})
        while True:
            d = ler()
            if d is None:
                sys.exit(5)
            interrompido(d)
            if d.get("type") == "control_response" and d["response"].get("request_id") == rid:
                return d["response"]["response"]
    c = ((ler() or {{}}).get("message") or {{}}).get("content")
    if isinstance(c, list):
        c = "\\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    prompt = c or ""
    with open(H + "/claude-args.jsonl") as f:
        n = len(f.read().splitlines())
    out({{"type": "system", "subtype": "init", "session_id": "{SID}", "model": "claude-fake-1",
         "permissionMode": "bypassPermissions"}})
    if "slow" in prompt:
        time.sleep(1.5)
    if "hang" in prompt:
        while True:
            d = ler()
            if d is None:
                sys.exit(5)
            interrompido(d)
    reply = "echo: " + prompt.strip().splitlines()[0]
    if prompt.startswith("ask tool"):
        r = pede("Bash", {{"command": "ls /"}}, "r1")
        reply = "tool " + r["behavior"] + (": " + r["message"] if r.get("message") else "")
    elif prompt.startswith("ask question"):
        r = pede("AskUserQuestion", {{"questions": [{{"question": "Pick a color?", "header": "Color",
                 "options": [{{"label": "Red", "description": "warm"}}, {{"label": "Blue", "description": "cool"}}],
                 "multiSelect": False}}]}}, "r2")
        reply = ("answers " + json.dumps(r["updatedInput"]["answers"])) if r["behavior"] == "allow" else "no answer"
    elif prompt.startswith("ask plan"):
        r = pede("ExitPlanMode", {{"plan": "# Plan\\n\\n1. Do it"}}, "r3")
        reply = "plan " + r["behavior"] + " " + json.dumps(r.get("updatedPermissions") or [])
    elif prompt.startswith("ask edit "):
        alvo = prompt.split(" ", 2)[2].strip()
        e = {{"file_path": alvo, "old_string": "beta", "new_string": "BETA"}}
        out({{"type": "assistant", "message": {{"id": "m0", "content": [{{"type": "tool_use", "id": "tu1",
             "name": "Edit", "input": e}}]}}}})
        r = pede("Edit", e, "r4")
        if r["behavior"] == "allow":
            with open(alvo) as f:
                orig = f.read()
            with open(alvo, "w") as f:
                f.write(orig.replace("beta", "BETA", 1))
            out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu1",
                 "content": "The file has been updated."}}]}}, "tool_use_result": {{"filePath": alvo,
                 "oldString": "beta", "newString": "BETA", "originalFile": orig, "structuredPatch": [
                 {{"oldStart": 1, "oldLines": 3, "newStart": 1, "newLines": 3,
                   "lines": [" alpha", "-beta", "+BETA", " gamma"]}}]}}}})
            reply = "edited"
        else:
            out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "tu1",
                 "content": r.get("message", ""), "is_error": True}}]}}}})
            reply = "edit denied"
    out({{"type": "stream_event", "event": {{"type": "content_block_delta",
         "delta": {{"type": "text_delta", "text": reply}}}}}})
    out({{"type": "assistant", "uuid": "uuid-" + str(n), "message": {{"id": "msg1",
         "content": [{{"type": "text", "text": reply}}], "usage": {{"input_tokens": 7, "output_tokens": 3}}}}}})
    out({{"type": "result", "usage": {{"input_tokens": 7, "output_tokens": 3}}, "total_cost_usd": 0.001,
         "num_turns": 1, "session_id": "{SID}"}})
    ate_fechar()  # like the real CLI, which exits once its stdin closes
""")


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Servidor:
    """The real server, started in a temporary HOME."""

    def __init__(self, preparar=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="agents-web-test-"))
        self.home = self.tmp / "home"
        binarios = self.home / ".local/bin"
        binarios.mkdir(parents=True)
        if preparar:
            preparar(self.home)
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

    def espera_pedido(self, cid, limite=15):
        """Follow the event stream until the run asks for something, and return that request."""
        c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=limite)
        fim = time.time() + limite
        try:
            c.request("GET", f"/api/chats/{cid}/eventos?desde=0", headers={"X-Token": self.token})
            r = c.getresponse()
            for linha in r:
                if time.time() > fim:
                    break
                if linha.startswith(b"data: "):
                    ev = json.loads(linha[6:])
                    if ev["t"] == "pedido" and ev["estado"] == "pendente":
                        return ev
                    if ev["t"] == "fim":
                        raise AssertionError("the run ended without a request")
            raise AssertionError("no request")
        finally:
            c.close()

    def stdin_claude(self):
        p = self.home / "claude-stdin.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

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
class TestLimpeza(unittest.TestCase):
    def test_chats_vazios_antigos_somem(self):
        velho = time.time() - 8 * 86400
        casos = {"vazio_velho": {"atualizado": velho}, "vazio_novo": {"atualizado": time.time() - 3600},
                 "ajustado": {"atualizado": velho, "ajustado": True}, "sem_data": {},
                 "com_msg": {"atualizado": velho, "mensagens": [{"papel": "usuario", "texto": "x"}]},
                 "com_fila": {"atualizado": velho, "fila": [{"texto": "x"}]}}
        def preparar(home):
            d = home / ".local/share/agents-web/chats"
            d.mkdir(parents=True)
            for nome, extra in casos.items():
                c = {"id": nome, "titulo": "New chat", "agente": "claude", "conta": "claude1", "cwd": str(home),
                     "sessao": "", "mensagens": [], **extra}
                (d / f"{nome}.json").write_text(json.dumps(c))
        s = Servidor(preparar)
        try:
            fim = time.time() + 5
            d = s.home / ".local/share/agents-web/chats"
            while (d / "vazio_velho.json").exists() and time.time() < fim:
                time.sleep(0.1)
            self.assertEqual(sorted(p.stem for p in d.glob("*.json")),
                             sorted(set(casos) - {"vazio_velho"}))
        finally:
            s.parar()


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
        self.assertIn(chat["titulo"], ("Hello there", "Fake title"))  # the smart title may already be in
        resp = chat["mensagens"][-1]
        self.assertEqual(resp["papel"], "assistente")
        self.assertEqual(resp["texto"], "echo: hello there")
        self.assertEqual(resp["uso"]["entrada"], 7)
        self.assertEqual(chat["totais"]["total"], 10)
        self.assertTrue(resp["uuid"].startswith("uuid-"))
        # the prompt goes in as a stream-json user message, with permission requests on stdio
        args = self.s.args_claude()[-1]
        self.assertEqual(args[args.index("--input-format") + 1], "stream-json")
        self.assertEqual(args[args.index("--permission-prompt-tool") + 1], "stdio")
        self.assertIn({"type": "user", "message": {"role": "user", "content": "hello there"},
                       "parent_tool_use_id": None, "session_id": ""}, self.s.stdin_claude())
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
        self.assertEqual(chat["mensagens"][-1]["erro"], "(stopped)")
        # a clean interrupt on stdin, not a kill
        self.assertTrue(any(d.get("type") == "control_request" and d["request"]["subtype"] == "interrupt"
                            for d in self.s.stdin_claude()))

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

    # ---------------------------------------------------------------- approvals, questions, plans
    def respostas_ctl(self):
        return {d["response"]["request_id"]: d["response"]["response"] for d in self.s.stdin_claude()
                if d.get("type") == "control_response"}

    def test_aprovar_negar_e_sempre(self):
        c = self.novo_chat(perm="manual")
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "ask tool"})
        p = self.s.espera_pedido(c["id"])
        self.assertEqual((p["tipo"], p["ferramenta"], p["entrada"]), ("ferramenta", "Bash", {"command": "ls /"}))
        self.assertTrue(any(x["id"] == p["id"] for x in self.s.api("/api/estado")["pedidos"]))
        self.s.api("/api/pedido", {"id": p["id"], "acao": "negar", "motivo": "not   now"})
        self.s.api("/api/pedido", {"id": p["id"], "acao": "aprovar"}, esperado=400)  # answered already
        chat = self.s.espera_parado(c["id"])
        m = chat["mensagens"][-1]
        self.assertEqual(m["texto"], "tool deny: The user denied this in the web interface and said: not now")
        self.assertEqual((m["pedidos"][0]["estado"], m["pedidos"][0]["motivo"]), ("negado", "not now"))
        # allow, and remember it for this chat
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "ask tool"})
        p = self.s.espera_pedido(c["id"])
        self.s.api("/api/pedido", {"id": p["id"], "acao": "sempre"})
        chat = self.s.espera_parado(c["id"])
        self.assertEqual(chat["mensagens"][-1]["texto"], "tool allow")
        self.assertEqual(chat["permitidos"], ["Bash"])
        # now Bash goes through without a card
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "ask tool"})
        chat = self.s.espera_parado(c["id"])
        self.assertEqual(chat["mensagens"][-1]["texto"], "tool allow")
        self.assertNotIn("pedidos", chat["mensagens"][-1])
        r = self.s.api(f"/api/chats/{c['id']}/permitidos", {})
        self.assertEqual(r["permitidos"], [])

    def test_pergunta(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "ask question"})
        p = self.s.espera_pedido(c["id"])
        self.assertEqual(p["tipo"], "pergunta")
        self.s.api("/api/pedido", {"id": p["id"], "acao": "responder", "respostas": {"Other?": "x"}}, esperado=400)
        self.s.api("/api/pedido", {"id": p["id"], "acao": "responder", "respostas": {"Pick a color?": "Blue\nish"}})
        chat = self.s.espera_parado(c["id"])
        self.assertEqual(chat["mensagens"][-1]["texto"], 'answers {"Pick a color?": "Blue ish"}')
        self.assertEqual(chat["mensagens"][-1]["pedidos"][0]["estado"], "respondido")

    def test_plano(self):
        c = self.novo_chat(perm="plan")
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "ask plan"})
        p = self.s.espera_pedido(c["id"])
        self.assertEqual((p["tipo"], p["entrada"]["plan"]), ("plano", "# Plan\n\n1. Do it"))
        self.s.api("/api/pedido", {"id": p["id"], "acao": "aprovar", "modo": "acceptEdits"})
        chat = self.s.espera_parado(c["id"])
        self.assertEqual(chat["mensagens"][-1]["texto"],
                         'plan allow [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]')
        self.assertEqual(chat["perm"], "acceptEdits")  # out of plan mode, like the terminal
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "next"})
        self.s.espera_parado(c["id"])
        args = self.s.args_claude()[-1]
        self.assertEqual(args[args.index("--permission-mode") + 1], "acceptEdits")

    def test_edicao_mostra_diff(self):
        alvo = self.s.home / "edit-me.txt"
        alvo.write_text("alpha\nbeta\ngamma\n")
        c = self.novo_chat(perm="manual")
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": f"ask edit {alvo}"})
        p = self.s.espera_pedido(c["id"])
        self.assertEqual(p["patch"]["arquivo"], str(alvo))
        linhas = p["patch"]["hunks"][0]["linhas"]
        self.assertIn("-beta", linhas)
        self.assertIn("+BETA", linhas)
        self.s.api("/api/pedido", {"id": p["id"], "acao": "aprovar"})
        chat = self.s.espera_parado(c["id"])
        f = chat["mensagens"][-1]["ferramentas"][0]
        self.assertEqual(f["patch"]["hunks"][0]["linhas"], [" alpha", "-beta", "+BETA", " gamma"])
        self.assertEqual(alvo.read_text(), "alpha\nBETA\ngamma\n")

    def test_parar_com_pedido_aberto_cancela(self):
        c = self.novo_chat(perm="manual")
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "ask tool"})
        p = self.s.espera_pedido(c["id"])
        self.s.api(f"/api/chats/{c['id']}/parar", {})
        chat = self.s.espera_parado(c["id"], 15)
        self.assertEqual(chat["mensagens"][-1]["pedidos"][0]["estado"], "cancelado")
        self.s.api("/api/pedido", {"id": p["id"], "acao": "aprovar"}, esperado=400)

    # ---------------------------------------------------------------- edit, retry, fork
    def test_reenviar_e_bifurcar(self):
        c = self.novo_chat()
        for t in ("first", "second"):
            self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": t})
            chat = self.s.espera_parado(c["id"])
        u1 = chat["mensagens"][1]["uuid"]
        self.s.api(f"/api/chats/{c['id']}/reenviar", {"indice": 1}, esperado=400)  # not a user message
        self.s.api(f"/api/chats/{c['id']}/reenviar", {"indice": 2, "texto": "second, edited"})
        chat = self.s.espera_parado(c["id"])
        args = self.s.args_claude()[-1]
        self.assertEqual(args[args.index("--resume-session-at") + 1], u1)
        self.assertIn("--fork-session", args)
        self.assertEqual([m["texto"] for m in chat["mensagens"]],
                         ["first", "echo: first", "second, edited", "echo: second, edited"])
        self.assertNotIn("fork", chat)
        self.assertIn(SID, chat["antigas"])
        # after the branch, the chat simply resumes its new session
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "third"})
        self.s.espera_parado(c["id"])
        self.assertNotIn("--fork-session", self.s.args_claude()[-1])
        # retry the first message: a fresh session
        self.s.api(f"/api/chats/{c['id']}/reenviar", {"indice": 0, "texto": None})
        chat = self.s.espera_parado(c["id"])
        self.assertNotIn("--resume", self.s.args_claude()[-1])
        self.assertEqual([m["texto"] for m in chat["mensagens"]], ["first", "echo: first"])
        # fork from a reply into a new chat
        n = self.s.api(f"/api/chats/{c['id']}/bifurcar", {"indice": 1})
        self.assertNotEqual(n["id"], c["id"])
        self.assertTrue(n["titulo"].startswith("Fork of"))
        self.assertEqual(len(n["mensagens"]), 2)
        self.s.api(f"/api/chats/{n['id']}/enviar", {"texto": "in the fork"})
        self.s.espera_parado(n["id"])
        args = self.s.args_claude()[-1]
        self.assertEqual(args[args.index("--resume-session-at") + 1], chat["mensagens"][1]["uuid"])
        self.assertIn("--fork-session", args)
        self.s.api(f"/api/chats/{c['id']}/bifurcar", {"indice": 0}, esperado=400)

    # ---------------------------------------------------------------- app
    def espera_titulo(self, cid, esperado, limite=10):
        fim = time.time() + limite
        while time.time() < fim:
            t = self.s.api(f"/api/chats/{cid}")["titulo"]
            if t == esperado:
                return t
            time.sleep(0.1)
        self.fail(f"title stayed {t!r}, expected {esperado!r}")

    def test_titulos(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hello https://example.com/docs/page?x=1 world"})
        self.s.espera_parado(c["id"])
        self.espera_titulo(c["id"], "Fake title")
        chamada = json.loads((self.s.home / "claude-titulo.jsonl").read_text().splitlines()[-1])
        a = chamada["args"]
        self.assertEqual(a[a.index("--model") + 1], "haiku")
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", a)
        self.assertIn("User: hello https://example.com/docs/page?x=1 world", chamada["stdin"])
        # the main runs never see the title call
        self.assertNotIn("--no-session-persistence", self.s.args_claude()[-1])
        # without smart titles: the first message, tidied
        self.s.api("/api/preferencias", {"titulos": False})
        try:
            c2 = self.novo_chat()
            self.s.api(f"/api/chats/{c2['id']}/enviar", {"texto": "  check https://github.com/a/Agents-Web/  now please "})
            self.assertEqual(self.s.api(f"/api/chats/{c2['id']}")["titulo"], "Check Agents-Web now please")
            longo = "word " * 30
            c3 = self.novo_chat()
            self.s.api(f"/api/chats/{c3['id']}/enviar", {"texto": longo})
            t = self.s.api(f"/api/chats/{c3['id']}")["titulo"]
            self.assertTrue(t.startswith("Word word") and t.endswith("word…") and len(t) <= 61, t)
            self.s.espera_parado(c2["id"]); self.s.espera_parado(c3["id"])
        finally:
            self.s.api("/api/preferencias", {"titulos": True})
        self.s.api("/api/preferencias", {"titulos": False})
        try:
            for texto, esperado in ((".env is not loaded", ".env is not loaded"), ("iPhone build fails", "iPhone build fails"),
                                    ("--force flag broke.", "--force flag broke")):
                cx = self.novo_chat()
                self.s.api(f"/api/chats/{cx['id']}/enviar", {"texto": texto})
                self.assertEqual(self.s.api(f"/api/chats/{cx['id']}")["titulo"], esperado)
                self.s.espera_parado(cx["id"])
        finally:
            self.s.api("/api/preferencias", {"titulos": True})
        # a short first message ("hi") is named once the reply is in
        curto = self.novo_chat()
        n0 = len((self.s.home / "claude-titulo.jsonl").read_text().splitlines())
        self.s.api(f"/api/chats/{curto['id']}/enviar", {"texto": "hi"})
        self.s.espera_parado(curto["id"])
        self.espera_titulo(curto["id"], "Fake title")
        chamada = json.loads((self.s.home / "claude-titulo.jsonl").read_text().splitlines()[n0])
        self.assertIn("Assistant: echo: hi", chamada["stdin"])
        # a chat without messages cannot be named; settings mark a chat as chosen, so it is kept
        vazio = self.novo_chat()
        st, _, _ = self.s.pedir("POST", f"/api/chats/{vazio['id']}/titulo", {}, {"X-Token": self.s.token})
        self.assertEqual(st, 400)
        self.assertFalse(next(x for x in self.s.api("/api/estado")["chats"] if x["id"] == vazio["id"])["ajustado"])
        self.s.api(f"/api/chats/{vazio['id']}/config", {"effort": "low"})
        self.assertTrue(next(x for x in self.s.api("/api/estado")["chats"] if x["id"] == vazio["id"])["ajustado"])
        # a failed title call keeps the tidy title; "Name it with AI" retries on demand
        c4 = self.novo_chat()
        self.s.api(f"/api/chats/{c4['id']}/enviar", {"texto": "no title here"})
        self.s.espera_parado(c4["id"])
        time.sleep(0.5)
        self.assertEqual(self.s.api(f"/api/chats/{c4['id']}")["titulo"], "No title here")
        st, _, _ = self.s.pedir("POST", f"/api/chats/{c4['id']}/titulo", {}, {"X-Token": self.s.token})
        self.assertEqual(st, 400)
        self.assertEqual(self.s.api(f"/api/chats/{c2['id']}/titulo", {})["titulo"], "Fake title")
        # renaming by hand wins over a title that arrives later
        r = self.s.api(f"/api/chats/{c3['id']}/config", {"titulo": "Mine"})
        self.assertFalse(r["titulo_auto"])
        # old chats named after their first message get a title in the background
        arq = self.s.home / ".local/share/agents-web/chats" / f"{c4['id']}.json"
        d = json.loads(arq.read_text())
        d.pop("titulo_auto", None)
        d["mensagens"][0]["texto"] = "an old question"
        d["mensagens"][1]["texto"] = "an old answer"
        d["titulo"] = "an old question"
        arq.write_text(json.dumps(d))
        self.s.api("/api/titulos", {})
        fim = time.time() + 10
        while self.s.api("/api/titulos")["rodando"] and time.time() < fim:
            time.sleep(0.1)
        self.assertEqual(self.s.api(f"/api/chats/{c4['id']}")["titulo"], "Fake title")
        self.assertEqual(self.s.api(f"/api/chats/{c3['id']}")["titulo"], "Mine")

    def test_mover_para_outra_conta(self):
        segunda = self.s.api("/api/contas/nova", {"agente": "claude", "nome": "Second Test"})
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hello"})
        self.s.espera_parado(c["id"])
        # a session of its own (the fake CLI always answers with the same id, which other chats share)
        sid = "7e1d2c3b-4a59-4687-9a0b-c1d2e3f4a5b6"
        arq = self.s.home / ".local/share/agents-web/chats" / f"{c['id']}.json"
        d = json.loads(arq.read_text())
        d["sessao"] = sid
        arq.write_text(json.dumps(d))
        org = self.s.home / ".claude/projects/-tmp-x" / f"{sid}.jsonl"
        org.parent.mkdir(parents=True, exist_ok=True)
        org.write_text('{"type":"user"}\n')
        (org.parent / sid / "subagents").mkdir(parents=True)
        r = self.s.api(f"/api/chats/{c['id']}/conta", {"conta": segunda["id"]})
        self.assertEqual(r["conta"], segunda["id"])
        dst = self.s.home / ".config/agents-web/contas" / segunda["id"] / "projects/-tmp-x"
        self.assertEqual((dst / f"{sid}.jsonl").read_text(), '{"type":"user"}\n')
        self.assertTrue((dst / sid / "subagents").is_dir())
        self.assertFalse(org.exists())
        # the next message resumes the same session, now on the other account
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "go on"})
        self.s.espera_parado(c["id"])
        ultimo = self.s.args_claude()[-1]
        self.assertEqual(ultimo[ultimo.index("--resume") + 1], sid)
        # a session another chat of the old account still uses is copied, not moved
        outro = self.novo_chat()
        self.s.api(f"/api/chats/{outro['id']}/enviar", {"texto": "hi"})
        self.s.espera_parado(outro["id"])
        comum = self.s.home / ".claude/projects/-tmp-y" / f"{SID}.jsonl"
        comum.parent.mkdir(parents=True, exist_ok=True)
        comum.write_text("{}\n")
        terceiro = self.novo_chat()
        self.s.api(f"/api/chats/{terceiro['id']}/enviar", {"texto": "hey"})
        self.s.espera_parado(terceiro["id"])
        self.s.api(f"/api/chats/{terceiro['id']}/conta", {"conta": segunda["id"]})
        self.assertTrue(comum.exists())
        self.assertTrue((self.s.home / ".config/agents-web/contas" / segunda["id"] / "projects/-tmp-y" / f"{SID}.jsonl").exists())
        # Antigravity chats and unknown accounts are refused
        st, b, _ = self.s.pedir("POST", f"/api/chats/{c['id']}/conta", {"conta": "nope"}, {"X-Token": self.s.token})
        self.assertEqual(st, 400)

    def test_manifesto_e_icones(self):
        st, b, cab = self.s.pedir("GET", "/manifest.webmanifest")
        self.assertEqual(st, 200)
        m = json.loads(b)
        self.assertEqual((m["name"], m["display"], m["start_url"]), ("Agents", "standalone", "/"))
        for nome, ini in (("/icon-192.png", b"\x89PNG"), ("/icon-512.png", b"\x89PNG"), ("/icon.svg", b"<")):
            st, b, _ = self.s.pedir("GET", nome)
            self.assertEqual((st, b[:len(ini)]), (200, ini), nome)

    def test_atualizar_fora_da_instalacao(self):
        # a checkout is not an installed copy, so it never updates itself
        r = self.s.pedir("POST", "/api/atualizar", {}, {"X-Token": self.s.token})
        self.assertEqual(r[0], 400)

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
