#!/usr/bin/env python3
"""End to end tests for src/server.py (Python standard library only).

Each run starts the real server in a throwaway HOME, on a free port, with a fake `claude` binary
that speaks the stream-json protocol. Nothing touches your real chats, logins or quota.

    python3 -m unittest discover -s tests -v
"""
import http.client
import json
import os
import re
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
# AGENTS_WEB_TEST_SERVER tests an installed copy instead (the package tests in CI use it)
SERVIDOR = Path(os.environ.get("AGENTS_WEB_TEST_SERVER") or RAIZ / "src" / "server.py")
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
    log("claude-env.jsonl", {{"conta": os.environ.get("CLAUDE_CONFIG_DIR", "")}})
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
    if os.path.exists((os.environ.get("CLAUDE_CONFIG_DIR") or H + "/.claude") + "/limitado"):
        # an account at its usage limit, as the real CLI reports it
        out({{"type": "rate_limit_event", "rate_limit_info": {{"status": "rejected", "rateLimitType": "five_hour",
             "resetsAt": int(time.time()) + 3600}}}})
        out({{"type": "result", "subtype": "success", "is_error": True, "session_id": "{SID}",
             "result": "You've hit your 5-hour limit · resets 5am (UTC)"}})
        ate_fechar()
        sys.exit(0)
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


# A tiny stand-in for the Antigravity CLI (`agy --output-format stream-json -p <prompt>`): it logs its argv
# and answers with the last line of the prompt, always in the same conversation.
FAKE_AGY = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["HOME"] + "/agy-args.jsonl", "a") as f:
        f.write(json.dumps(args) + "\\n")
    prompt = args[args.index("-p") + 1] if "-p" in args else ""
    linhas = [l for l in prompt.strip().splitlines() if l.strip()]
    reply = "agy: " + (linhas[-1] if linhas else "")
    def out(d):
        print(json.dumps(d))
        sys.stdout.flush()
    out({{"event": "init", "conversation_id": "agy-conv-1", "init": {{"model": "gemini-fake"}}}})
    out({{"event": "step_update", "step_update": {{"step_type": "agent_response", "text_delta": reply,
         "state": "DONE", "usage": {{"input_tokens": 5, "output_tokens": 2}}}}}})
    out({{"event": "result", "result": {{"status": "SUCCESS", "response": reply, "conversation_id": "agy-conv-1",
         "usage": {{"input_tokens": 5, "output_tokens": 2}}, "num_turns": 1}}}})
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
        agy = binarios / "agy"
        agy.write_text(FAKE_AGY)
        agy.chmod(0o755)
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

    def args_agy(self):
        p = self.home / "agy-args.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def args_claude(self):
        p = self.home / "claude-args.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def execucoes(self):
        """(argv, CLAUDE_CONFIG_DIR) of every chat run of the fake CLI, oldest first."""
        p = self.home / "claude-env.jsonl"
        envs = [json.loads(l)["conta"] for l in p.read_text().splitlines()] if p.exists() else []
        return [(a, e) for a, e in zip(self.args_claude(), envs) if a[:2] != ["auth", "status"]]

    def dir_conta(self, cid):
        return self.home / ".claude" if cid == "claude1" else self.home / ".config/agents-web/contas" / cid

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

    def test_mover_tudo_ou_nada(self):
        seg = self.s.api("/api/contas/nova", {"agente": "claude", "nome": "Atomic"})
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hi"})
        self.s.espera_parado(c["id"])
        atual, velha = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
        arq = self.s.home / ".local/share/agents-web/chats" / f"{c['id']}.json"
        d = json.loads(arq.read_text())
        d["sessao"], d["antigas"] = atual, [velha]
        arq.write_text(json.dumps(d))
        A, B = self.s.dir_conta("claude1"), self.s.dir_conta(seg["id"])
        for sid in (atual, velha):
            (A / "projects/-tmp-at").mkdir(parents=True, exist_ok=True)
            (A / "projects/-tmp-at" / f"{sid}.jsonl").write_text('{"a":1}\n')
        (A / "file-history" / velha).mkdir(parents=True)
        (A / "file-history" / velha / "f@v1").write_text("x")
        B.mkdir(parents=True, exist_ok=True)
        (B / "file-history").write_text("in the way")      # the older branch cannot be copied
        st, corpo, _ = self.s.pedir("POST", f"/api/chats/{c['id']}/conta", {"conta": seg["id"]}, {"X-Token": self.s.token})
        self.assertEqual(st, 400)
        self.assertIn("Nothing changed", json.loads(corpo)["erro"])
        self.assertEqual(self.s.api(f"/api/chats/{c['id']}")["conta"], "claude1")
        self.assertTrue((A / "projects/-tmp-at" / f"{atual}.jsonl").exists())
        self.assertTrue((A / "file-history" / velha / "f@v1").exists())
        self.assertFalse((B / "projects").exists())     # nothing left behind on the other account
        # with the obstacle gone, and copies already on the other account: a longer one is kept, a shorter
        # one is replaced, a different one is replaced and kept as .bak
        (B / "file-history").unlink()
        (B / "projects/-tmp-at").mkdir(parents=True)
        (B / "projects/-tmp-at" / f"{atual}.jsonl").write_text('{"a":1}\n{"b":2}\n')
        (B / "file-history" / velha).mkdir(parents=True)
        (B / "file-history" / velha / "f@v1").write_text("other")
        r = self.s.api(f"/api/chats/{c['id']}/conta", {"conta": seg["id"]})
        self.assertEqual(r["conta"], seg["id"])
        self.assertEqual((B / "projects/-tmp-at" / f"{atual}.jsonl").read_text(), '{"a":1}\n{"b":2}\n')
        self.assertEqual((B / "projects/-tmp-at" / f"{velha}.jsonl").read_text(), '{"a":1}\n')
        self.assertEqual((B / "file-history" / velha / "f@v1").read_text(), "x")
        self.assertEqual([p.read_text() for p in (B / "file-history" / velha).glob("f@v1.aw-*.bak")], ["other"])
        self.assertFalse((A / "projects/-tmp-at" / f"{atual}.jsonl").exists())
        self.assertFalse((A / "file-history" / velha).exists())
        self.assertEqual([p.name for p in (B / "projects/-tmp-at").iterdir() if p.name.startswith(".")], [])

    def test_mover_mantem_modelo_e_esforco(self):
        seg = self.s.api("/api/contas/nova", {"agente": "claude", "nome": "Pinned"})
        A = self.s.dir_conta("claude1")
        A.mkdir(parents=True, exist_ok=True)
        (A / "settings.json").write_text(json.dumps({"modelSettings": {"claude-fake-1": {"effortLevel": "xhigh"}}}))
        try:
            c = self.novo_chat()
            self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hi"})
            self.s.espera_parado(c["id"])
            r = self.s.api(f"/api/chats/{c['id']}/conta", {"conta": seg["id"]})
            self.assertEqual(r["fixado"], {"modelo": "claude-fake-1", "effort": "xhigh"})
            self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "go on"})
            self.s.espera_parado(c["id"])
            a, conta = self.s.execucoes()[-1]
            self.assertEqual(a[a.index("--model") + 1], "claude-fake-1")
            self.assertEqual(a[a.index("--effort") + 1], "xhigh")
            self.assertEqual(conta, str(self.s.dir_conta(seg["id"])))
            # a chat with its own model and effort is left alone
            c2 = self.novo_chat(modelo="opus", effort="low")
            self.s.api(f"/api/chats/{c2['id']}/enviar", {"texto": "hi"})
            self.s.espera_parado(c2["id"])
            r = self.s.api(f"/api/chats/{c2['id']}/conta", {"conta": seg["id"]})
            self.assertNotIn("fixado", r)
            self.assertEqual((r["modelo"], r["effort"]), ("opus", "low"))
        finally:
            (A / "settings.json").unlink()

    def test_memoria_unica(self):
        seg = self.s.api("/api/contas/nova", {"agente": "claude", "nome": "Memory"})
        A, B = self.s.dir_conta("claude1"), self.s.dir_conta(seg["id"])
        repo = self.s.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
        nome = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(repo))
        # what the other account remembered on its own, and a file the main account has with other content
        (B / "projects" / nome / "memory").mkdir(parents=True)
        (B / "projects" / nome / "memory/MEMORY.md").write_text("Rules\n- [Tabs](tabs.md) - use tabs\n- [Style](style.md) - x\n")
        (B / "projects" / nome / "memory/tabs.md").write_text("tabs")
        (B / "projects" / nome / "memory/style.md").write_text("style B")
        (A / "projects" / nome / "memory").mkdir(parents=True)
        (A / "projects" / nome / "memory/MEMORY.md").write_text("- [Style](style.md) - main\n")
        (A / "projects" / nome / "memory/style.md").write_text("style A")
        c = self.s.api("/api/chats", {"agente": "claude", "conta": seg["id"], "cwd": str(repo / "sub")})
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hi"})
        self.s.espera_parado(c["id"])
        a, _ = self.s.execucoes()[-1]
        ajuste = json.loads(a[a.index("--settings") + 1])
        self.assertEqual(ajuste, {"autoMemoryDirectory": str(A / "projects" / nome / "memory")})
        m = A / "projects" / nome / "memory"
        self.assertEqual((m / "tabs.md").read_text(), "tabs")
        self.assertEqual((m / "style.md").read_text(), "style A")
        self.assertEqual((m / f"style-{seg['id']}.md").read_text(), "style B")
        idx = (m / "MEMORY.md").read_text()
        self.assertIn("- [Tabs](tabs.md) - use tabs", idx)
        self.assertIn(f"- [Style](style-{seg['id']}.md) - x", idx)
        self.assertIn("- [Style](style.md) - main", idx)
        self.assertTrue((B / "projects" / nome / "memory/tabs.md").exists())   # the originals stay
        # merged once: later runs add nothing twice
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "again"})
        self.s.espera_parado(c["id"])
        self.assertEqual((m / "MEMORY.md").read_text(), idx)
        # a linked git worktree shares the memory of its main checkout
        wt = self.s.tmp / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt\n")
        c2 = self.s.api("/api/chats", {"agente": "claude", "conta": seg["id"], "cwd": str(wt)})
        self.s.api(f"/api/chats/{c2['id']}/enviar", {"texto": "hi"})
        self.s.espera_parado(c2["id"])
        a, _ = self.s.execucoes()[-1]
        self.assertEqual(json.loads(a[a.index("--settings") + 1])["autoMemoryDirectory"], str(m))
        # the main account uses its own folder, which is the shared one
        c3 = self.s.api("/api/chats", {"agente": "claude", "conta": "claude1", "cwd": str(repo)})
        self.s.api(f"/api/chats/{c3['id']}/enviar", {"texto": "hi"})
        self.s.espera_parado(c3["id"])
        self.assertNotIn("--settings", self.s.execucoes()[-1][0])
        # turned off: each account keeps its own memory
        self.s.api("/api/preferencias", {"memoria_unica": False})
        try:
            self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hi"})
            self.s.espera_parado(c["id"])
            self.assertNotIn("--settings", self.s.execucoes()[-1][0])
        finally:
            self.s.api("/api/preferencias", {"memoria_unica": True})

    def test_troca_automatica_no_limite(self):
        fora = self.s.api("/api/contas/nova", {"agente": "claude", "nome": "Signed out"})
        reserva = self.s.api("/api/contas/nova", {"agente": "claude", "nome": "Spare"})
        claudes = [x["id"] for x in self.s.api("/api/estado")["contas"] if x["agente"] == "claude"]
        limitado = lambda cid: self.s.dir_conta(cid) / "limitado"
        login = lambda cid: self.s.dir_conta(cid) / ".credentials.json"
        for cid in claudes:
            if cid != fora["id"]:
                # signed in, with an expired token so the usage lookup stays offline
                self.s.dir_conta(cid).mkdir(parents=True, exist_ok=True)
                login(cid).write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": 1000}}))
        limitado("claude1").write_text("")
        try:
            # off: the reply ends with the limit error and the chat stays where it was
            c = self.novo_chat()
            self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "work"})
            c = self.s.espera_parado(c["id"])
            self.assertEqual(c["conta"], "claude1")
            self.assertIn("5-hour limit", c["mensagens"][-1]["erro"])
            # on: it moves to a free account and goes on by itself
            self.s.api("/api/preferencias", {"troca_auto": True})
            for cid in claudes:      # only the spare account has room left (and one that is signed out)
                if cid not in ("claude1", reserva["id"], fora["id"]):
                    self.s.dir_conta(cid).mkdir(parents=True, exist_ok=True)
                    limitado(cid).write_text("")
            c = self.novo_chat()
            self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "work"})
            time.sleep(0.3)
            c = self.s.espera_parado(c["id"])
            self.assertEqual(c["conta"], reserva["id"])
            ms = c["mensagens"]
            # accounts that turn out to be at their limit too are each tried once on the way
            trocas = [m["troca"] for m in ms if m.get("troca")]
            self.assertEqual(trocas[0]["de"], "claude1")
            self.assertEqual(trocas[-1]["para"], reserva["id"])
            self.assertEqual(len({t["de"] for t in trocas}), len(trocas))
            self.assertNotIn(fora["id"], [t["para"] for t in trocas])
            self.assertTrue(all(m.get("auto") for m in ms[2::2]))
            self.assertEqual(ms[-2]["texto"], "Continue where you left off.")
            self.assertEqual(ms[-1]["texto"], "echo: Continue where you left off.")
            self.assertFalse(ms[-1].get("erro"))
            self.assertEqual(self.s.execucoes()[-1][1], str(self.s.dir_conta(reserva["id"])))
            # every account at its limit: each one is tried once, then it stops with the error
            limitado(reserva["id"]).write_text("")
            antes = len(self.s.execucoes())
            self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "more"})
            time.sleep(0.3)
            fim = time.time() + 30
            while self.s.api(f"/api/chats/{c['id']}")["rodando"] and time.time() < fim:
                time.sleep(0.2)
            c = self.s.api(f"/api/chats/{c['id']}")
            self.assertFalse(c["rodando"])
            self.assertIn("5-hour limit", c["mensagens"][-1]["erro"])
            self.assertLessEqual(len(self.s.execucoes()) - antes, len(claudes))
        finally:
            self.s.api("/api/preferencias", {"troca_auto": False})
            for cid in claudes:
                limitado(cid).unlink(missing_ok=True)
                login(cid).unlink(missing_ok=True)

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

    def test_prompts_salvos(self):
        r = self.s.api("/api/prompts", {"prompts": [
            {"nome": "  Code   review ", "texto": "Review the code in this folder"},
            {"nome": "", "texto": "Summarize\nthe log"},
            {"nome": "blank", "texto": "   "},
        ]})["prompts"]
        self.assertEqual([p["nome"] for p in r], ["Code review", "Summarize the log"])
        self.assertTrue(all(len(p["id"]) == 8 for p in r))
        self.assertEqual(self.s.api("/api/estado")["prompts"], r)
        # ids survive an edit, duplicates get a new one
        r2 = self.s.api("/api/prompts", {"prompts": [r[1], {**r[1], "nome": "copy"}]})["prompts"]
        self.assertEqual(r2[0]["id"], r[1]["id"])
        self.assertNotEqual(r2[1]["id"], r[1]["id"])
        self.s.api("/api/prompts", {"prompts": [{"texto": "x" * 20001}]}, esperado=400)
        self.s.api("/api/prompts", {"prompts": "nope"}, esperado=400)
        self.s.api("/api/prompts", {"prompts": [{"texto": "y"}] * 201}, esperado=400)
        self.assertEqual(self.s.api("/api/estado")["prompts"], r2)
        self.s.api("/api/prompts", {"prompts": []})
        self.assertEqual(self.s.api("/api/estado")["prompts"], [])

    def test_preview_isolado(self):
        r = self.s.api("/api/preview", {"codigo": "<h1>hi</h1><script>1</script>", "tipo": "html"})
        st, corpo, cab = self.s.pedir("GET", r["url"])
        self.assertEqual(st, 200)
        self.assertIn(b"<h1>hi</h1>", corpo)
        self.assertTrue(cab["Content-Security-Policy"].startswith("sandbox"))
        self.assertNotIn("allow-same-origin", cab["Content-Security-Policy"])


    # ---------------------------------------------------------------- Call another AI
    def espera_n(self, cid, n, limite=30):
        fim = time.time() + limite
        while time.time() < fim:
            c = self.s.api(f"/api/chats/{cid}")
            if len(c["mensagens"]) >= n and not c["rodando"]:
                return c
            time.sleep(0.1)
        raise AssertionError(f"expected {n} messages: {[m.get('texto') for m in c['mensagens']]}")

    def ultimo_prompt_claude(self):
        return [d for d in self.s.stdin_claude() if d.get("type") == "user"][-1]["message"]["content"]

    def test_chamar_outra_ia_uma_vez(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "what is a monad"})
        self.s.espera_parado(c["id"])
        antes = len(self.s.args_agy())
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy", "conta": "google", "texto": "is that right?"})
        chat = self.espera_n(c["id"], 4)
        pedido, resp = chat["mensagens"][2], chat["mensagens"][3]
        self.assertEqual(pedido["para"], "Antigravity")
        self.assertEqual(resp["convidado"], {"agente": "agy", "conta": "google", "nome": "Antigravity"})
        self.assertEqual(resp["texto"], "agy: is that right?")
        # its own session, kept apart from the chat's Claude session
        self.assertEqual(chat["sessao"], SID)
        self.assertEqual(chat["convidado"]["sessao"], "agy-conv-1")
        self.assertIn("agy-conv-1", chat.get("sids_conv"))
        args = self.s.args_agy()[antes]
        self.assertNotIn("--conversation", args)
        prompt = args[args.index("-p") + 1]
        self.assertIn("### User\nwhat is a monad", prompt)
        self.assertIn("### Claude\necho: what is a monad", prompt)
        self.assertNotIn("### User (to", prompt)   # the request itself comes after the conversation
        self.assertTrue(prompt.rstrip().endswith("The user asks you:\nis that right?"))
        # Claude gets the other AI's reply with your next message, on its own session
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "thanks both"})
        self.espera_n(c["id"], 6)
        dado = self.ultimo_prompt_claude()
        self.assertIn("called Antigravity into this chat", dado)
        self.assertIn("### User (to Antigravity)\nis that right?", dado)
        self.assertIn("### Antigravity\nagy: is that right?", dado)
        self.assertTrue(dado.endswith("The user's new message:\nthanks both"))
        ultimo = self.s.args_claude()[-1]
        self.assertEqual(ultimo[ultimo.index("--resume") + 1], SID)
        # the next Claude message carries nothing extra
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "plain"})
        self.espera_n(c["id"], 8)
        self.assertEqual(self.ultimo_prompt_claude(), "plain")
        # called again: resumes its conversation and only gets what it has not seen
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy", "conta": "google"})
        chat = self.espera_n(c["id"], 9)
        args = self.s.args_agy()[-1]
        self.assertEqual(args[args.index("--conversation") + 1], "agy-conv-1")
        prompt = args[args.index("-p") + 1]
        self.assertNotIn("what is a monad", prompt)
        self.assertIn("### User\nthanks both", prompt)
        self.assertIn("Give your own view", prompt)
        self.assertNotIn("para", chat["mensagens"][-1])   # no request of yours this time
        # not listed again as a terminal conversation
        self.assertFalse(any(x.get("sid") == "agy-conv-1" for x in self.s.api("/api/cli/lista")))

    def test_debate_vai_e_volta_e_para_sozinho(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "tabs or spaces"})
        self.s.espera_parado(c["id"])
        n_claude = len(self.s.args_claude())
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy", "conta": "google", "rodadas": 2})
        self.espera_n(c["id"], 6, 40)
        time.sleep(0.5)
        chat = self.s.api(f"/api/chats/{c['id']}")
        quem = [("agy" if m.get("convidado") else "claude") if m["papel"] == "assistente" else "voce"
                for m in chat["mensagens"]]
        self.assertEqual(quem, ["voce", "claude", "agy", "claude", "agy", "claude"])
        self.assertTrue(all(m.get("debate") for m in chat["mensagens"][2:]))
        self.assertNotIn("debate", chat)   # 4 replies done, it ended by itself
        self.assertFalse(chat["rodando"])
        self.assertEqual(len(self.s.args_claude()) - n_claude, 2)
        # Claude's debate turns: no message of yours, the other AI's reply goes in
        dado = self.ultimo_prompt_claude()
        self.assertIn("### Antigravity\nagy:", dado)
        self.assertIn("Talk to Antigravity directly", dado)
        p1 = self.s.args_agy()[-2]
        self.assertIn("reply 1 of 4", p1[p1.index("-p") + 1])
        p2 = self.s.args_agy()[-1]
        self.assertIn("reply 3 of 4", p2[p2.index("-p") + 1])
        self.assertEqual(p2[p2.index("--conversation") + 1], "agy-conv-1")

    def test_debate_parar_encerra(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "first"})
        self.s.espera_parado(c["id"])
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "claude", "conta": "claude1",
                                                       "texto": "hang here", "rodadas": 3})
        time.sleep(0.6)
        chat = self.s.api(f"/api/chats/{c['id']}")
        self.assertTrue(chat["rodando"])
        self.assertEqual(chat["vivo"]["nome"], "Claude (guest)")
        self.assertEqual(chat["debate"], {"total": 6, "feitas": 0})
        self.s.api(f"/api/chats/{c['id']}/parar", {})
        self.s.espera_parado(c["id"])
        time.sleep(0.5)
        chat = self.s.api(f"/api/chats/{c['id']}")
        self.assertNotIn("debate", chat)
        self.assertIn("(stopped)", chat["mensagens"][-1]["erro"])
        self.assertEqual(len(chat["mensagens"]), 4)
        self.assertEqual(chat["sessao"], SID)

    def test_encerrar_debate_depois_da_resposta(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "first"})
        self.s.espera_parado(c["id"])
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "claude", "conta": "claude1",
                                                       "texto": "slow please", "rodadas": 5})
        self.s.api(f"/api/chats/{c['id']}/debate", {})
        self.s.espera_parado(c["id"])
        time.sleep(0.5)
        chat = self.s.api(f"/api/chats/{c['id']}")
        self.assertEqual(len(chat["mensagens"]), 4)
        self.assertTrue(chat["mensagens"][-1]["texto"].startswith("echo: "))
        self.assertNotIn("debate", chat)

    def test_editar_depois_do_convidado(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "one"})
        self.s.espera_parado(c["id"])
        uuid1 = self.s.api(f"/api/chats/{c['id']}")["mensagens"][1]["uuid"]
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy", "conta": "google", "texto": "hi agy"})
        self.espera_n(c["id"], 4)
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "two"})
        self.espera_n(c["id"], 6)
        # your request to the other AI cannot be edited, it is called again instead
        self.s.api(f"/api/chats/{c['id']}/reenviar", {"indice": 2, "texto": "x"}, esperado=400)
        # editing "two" branches Claude at its own last reply, skipping the other AI's part
        self.s.api(f"/api/chats/{c['id']}/reenviar", {"indice": 4, "texto": "two again"})
        self.espera_n(c["id"], 6)
        ultimo = self.s.args_claude()[-1]
        self.assertEqual(ultimo[ultimo.index("--resume-session-at") + 1], uuid1)
        dado = self.ultimo_prompt_claude()
        self.assertIn("### Antigravity\nagy: hi agy", dado)
        self.assertTrue(dado.endswith("two again"))

    def test_chamar_validacoes(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy"}, esperado=400)       # empty chat
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "x", "texto": "a"}, esperado=400)
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy", "conta": "claude1", "texto": "a"}, esperado=400)

    def test_convidado_nao_tem_mais_permissao_que_o_chat(self):
        c = self.novo_chat(perm="plan")
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy", "conta": "google", "texto": "look around"})
        chat = self.espera_n(c["id"], 2)
        args = self.s.args_agy()[-1]
        self.assertEqual(args[args.index("--mode") + 1], "plan")
        self.assertNotIn("--dangerously-skip-permissions", args)
        # a chat started by calling the other AI gets a title too
        self.assertNotIn(chat["titulo"], ("New chat", "Novo chat"))
        c2 = self.novo_chat()
        self.s.api(f"/api/chats/{c2['id']}/convidar", {"agente": "agy", "conta": "google", "texto": "hello"})
        self.espera_n(c2["id"], 2)
        args = self.s.args_agy()[-1]
        self.assertIn("--dangerously-skip-permissions", args)

    # ---------------------------------------------------------------- duo chats
    def test_duo_as_duas_respondem_cada_mensagem(self):
        c = self.novo_chat(effort="high")
        r = self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy", "conta": "google", "modo": "junto",
                                                           "modelo": "gemini-3.8-flash-low"})
        self.assertFalse(r["rodando"])   # joining an empty chat waits for your message
        chat = self.s.api(f"/api/chats/{c['id']}")
        self.assertTrue(chat["duo"])
        self.assertEqual(chat["mensagens"], [])
        self.assertEqual(next(x for x in self.s.api("/api/estado")["chats"] if x["id"] == c["id"])["duo"], "agy")
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "plan a trip"})
        chat = self.espera_n(c["id"], 3)
        quem = [m["papel"] + ("/" + m["convidado"]["agente"] if m.get("convidado") else "") for m in chat["mensagens"]]
        self.assertEqual(quem, ["usuario", "assistente", "assistente/agy"])
        host, conv = chat["mensagens"][1], chat["mensagens"][2]
        self.assertTrue(conv.get("duo"))
        self.assertEqual(host["effort"], "high")
        self.assertEqual(conv["effort"], "low")
        # Claude knows it has a teammate; the other AI gets the message and Claude's reply, and works on top of it
        self.assertIn("Antigravity is in this chat with you as a team", self.ultimo_prompt_claude())
        self.assertTrue(self.ultimo_prompt_claude().endswith("The user's new message:\nplan a trip"))
        args = self.s.args_agy()[-1]
        self.assertEqual(args[args.index("--model") + 1], "gemini-3.8-flash-low")
        prompt = args[args.index("-p") + 1]
        self.assertIn("### User\nplan a trip", prompt)
        self.assertIn("### Claude\necho:", prompt)
        self.assertIn("You and Claude are a team", prompt)
        self.assertNotIn("Give your own view", prompt)
        # next message: Claude gets the other AI's reply first, the other AI only what is new
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "cheaper please"})
        chat = self.espera_n(c["id"], 6)
        self.assertEqual(len(chat["mensagens"]), 6)
        self.assertIn("### Antigravity\nagy:", self.ultimo_prompt_claude())
        args = self.s.args_agy()[-1]
        prompt = args[args.index("-p") + 1]
        self.assertEqual(args[args.index("--conversation") + 1], "agy-conv-1")
        self.assertNotIn("plan a trip", prompt)
        self.assertIn("### User\ncheaper please", prompt)
        # leaving: Claude alone again
        n = len(self.s.args_agy())
        chat = self.s.api(f"/api/chats/{c['id']}/convidado", {"sair": True})
        self.assertFalse(chat["duo"])
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "solo"})
        chat = self.espera_n(c["id"], 8)
        time.sleep(0.5)
        self.assertEqual(len(self.s.api(f"/api/chats/{c['id']}")["mensagens"]), 8)
        self.assertEqual(len(self.s.args_agy()), n)

    def test_duo_fila_espera_as_duas(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "agy", "conta": "google", "modo": "junto"})
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "slow first"})
        r = self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "second"})
        self.assertEqual(r.get("fila"), 1)
        chat = self.espera_n(c["id"], 6, 40)
        quem = [("agy" if m.get("convidado") else "claude") if m["papel"] == "assistente" else m["texto"]
                for m in chat["mensagens"]]
        self.assertEqual(quem, ["slow first", "claude", "agy", "second", "claude", "agy"])

    def test_duo_com_texto_e_ajustes(self):
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "hi"})
        self.s.espera_parado(c["id"])
        r = self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "claude", "conta": "claude1", "modo": "junto",
                                                           "texto": "say hello", "effort": "low"})
        self.assertTrue(r["rodando"])
        chat = self.espera_n(c["id"], 4)
        self.assertEqual(chat["mensagens"][-1]["effort"], "low")
        ultimo = self.s.args_claude()[-1]
        self.assertEqual(ultimo[ultimo.index("--effort") + 1], "low")
        chat = self.s.api(f"/api/chats/{c['id']}/convidado", {"effort": "max", "modelo": "haiku"})
        self.assertEqual((chat["convidado"]["effort"], chat["convidado"]["modelo"]), ("max", "haiku"))
        self.assertEqual(chat["convidado"]["nome"], "Claude (haiku)")
        self.s.api(f"/api/chats/{c['id']}/convidado", {"effort": "nope"})
        self.assertEqual(self.s.api(f"/api/chats/{c['id']}")["convidado"]["effort"], "")

    def test_duo_convidado_que_nao_pode_responder_libera_a_fila(self):
        self.s.api("/api/contas/nova", {"agente": "claude", "nome": "Duo Gone"})
        cid_conta = next(x["id"] for x in self.s.api("/api/estado")["contas"] if x["nome"] == "Duo Gone")
        c = self.novo_chat()
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "claude", "conta": cid_conta, "modo": "junto"})
        self.s.api("/api/contas/editar", {"id": cid_conta, "remover": True})
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "slow one"})
        self.s.api(f"/api/chats/{c['id']}/enviar", {"texto": "two"})
        chat = self.espera_n(c["id"], 5, 30)
        quem = [m["papel"] + ("/erro" if m.get("erro") else "") for m in chat["mensagens"]]
        self.assertEqual(quem, ["usuario", "assistente", "assistente/erro", "usuario", "assistente"])
        self.assertIn("could not answer", chat["mensagens"][2]["erro"])
        self.assertFalse(chat["duo"])
        # joining with an account that is gone is refused at once
        self.s.api(f"/api/chats/{c['id']}/convidar", {"agente": "claude", "conta": cid_conta, "modo": "junto"},
                   esperado=400)


if __name__ == "__main__":
    unittest.main()
