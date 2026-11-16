#!/usr/bin/env python3
"""End to end check of moving Claude chats between accounts, with the real `claude` CLI and two of your own
Claude accounts. It is not part of the unit tests (it spends a little of each account's usage: about 15 short
Claude Haiku replies), so run it by hand:

    python3 tests/e2e_contas.py [account A] [account B]

Account ids are the ones in ~/.config/agents-web/config.json (default: the first two Claude accounts).
It starts a separate Agents Web on a free port with its own temporary data, so your chats are not touched,
works in a temporary folder, and deletes the sessions and memory it made in both accounts at the end.
"""
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
HOME = Path.home()
CFG_REAL = Path(os.environ.get("AGENTS_WEB_CONFIG_DIR") or HOME / ".config/agents-web") / "config.json"
RES = []


def dir_conta(c):
    return Path(c["dir"]) if c.get("dir") else HOME / ".claude"


def check(nome, ok, extra=""):
    RES.append((nome, bool(ok)))
    print(("  PASS " if ok else "  FAIL ") + nome + (f"  ({str(extra)[:90]})" if extra else ""), flush=True)


class Instancia:
    def __init__(self, contas, principal):
        self.tmp = Path(tempfile.mkdtemp(prefix="agents-web-e2e-"))
        self.proj = self.tmp / "projeto"
        self.proj.mkdir()
        (self.tmp / "cfg").mkdir()
        (self.tmp / "run").mkdir(mode=0o700)
        # memories of the other accounts count as merged already: only the test folder gets merged here
        feitos = {c["id"]: sorted(p.parent.name for p in (dir_conta(c) / "projects").glob("*/memory"))
                  for c in contas if c["id"] != principal}
        cfg = {"contas": contas, "memorias_unidas": feitos,
               "padrao": {"cwd": str(self.proj), "perm_claude": "bypassPermissions", "perm_agy": "skip",
                          "titulos": False, "dica": False, "memoria_unica": True}}
        f = self.tmp / "cfg/config.json"
        f.write_text(json.dumps(cfg))
        f.chmod(0o600)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.porta = s.getsockname()[1]
        s.close()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_CONFIG_DIR"))}
        env.update(AGENTS_WEB_PORT=str(self.porta), AGENTS_WEB_CONFIG_DIR=str(self.tmp / "cfg"),
                   AGENTS_WEB_DATA_DIR=str(self.tmp / "data"), XDG_RUNTIME_DIR=str(self.tmp / "run"),
                   PATH=f"{HOME}/.local/bin:{env.get('PATH', '/usr/bin')}")
        self.log = open(self.tmp / "server.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-I", "-u", str(RAIZ / "src/server.py")], env=env,
                                     cwd=str(self.tmp), stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if self.pedir("GET", "/saude")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        self.token = json.loads(f.read_text())["token"]

    def pedir(self, metodo, caminho, corpo=None, cab=None):
        c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=60)
        c.request(metodo, caminho, json.dumps(corpo) if corpo is not None else None,
                  {"Content-Type": "application/json", **(cab or {})})
        r = c.getresponse()
        return r.status, r.read()

    def api(self, caminho, corpo=None):
        st, b = self.pedir("POST" if corpo is not None else "GET", caminho, corpo, {"X-Token": self.token})
        if st != 200:
            raise RuntimeError(f"{caminho} answered {st}: {b[:300]!r}")
        return json.loads(b)

    def espera(self, cid, limite=300):
        fim = time.time() + limite
        while time.time() < fim:
            c = self.api(f"/api/chats/{cid}")
            if not c["rodando"]:
                return c
            time.sleep(0.5)
        raise RuntimeError("the reply never ended")

    def envia(self, cid, texto):
        self.api(f"/api/chats/{cid}/enviar", {"texto": texto})
        time.sleep(0.5)
        c = self.espera(cid)
        m = c["mensagens"][-1]
        print(f"     [{c['conta']}] {texto[:60]!r} -> {m.get('texto', '')[:70]!r}"
              + (f" ERROR {m['erro'][:80]}" if m.get("erro") else ""), flush=True)
        return c, m

    def parar(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


def main():
    cfg = json.loads(CFG_REAL.read_text())
    claudes = [c for c in cfg["contas"] if c["agente"] == "claude"]
    pedidas = sys.argv[1:3]
    if pedidas:
        claudes = [next(c for c in claudes if c["id"] == x) for x in pedidas]
    if len(claudes) < 2:
        sys.exit("needs two Claude accounts in Agents Web")
    A, B = claudes[0], claudes[1]
    if dir_conta(A).resolve() == dir_conta(B).resolve():
        sys.exit("those two accounts share one login folder")
    principal = next((c["id"] for c in cfg["contas"] if c["agente"] == "claude"), A["id"])
    contas = [{**c} for c in cfg["contas"] if c["agente"] == "claude"]
    print(f"A = {A['id']} ({A.get('nome')}), B = {B['id']} ({B.get('nome')}), shared memory of {principal}")
    s = Instancia(contas, principal)
    nome = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(s.proj))
    sessao = lambda c, sid: (dir_conta(c) / "projects" / nome / f"{sid}.jsonl").exists()
    novo = lambda conta: s.api("/api/chats", {"agente": "claude", "conta": conta["id"], "modelo": "haiku", "cwd": str(s.proj)})
    try:
        palavra = "ZEBRA-" + str(secrets.randbelow(9000) + 1000)
        print("== 1. A to B and back")
        c = novo(A)
        c, m = s.envia(c["id"], f"Remember this code word: {palavra}. Reply with only: OK")
        sid = c["sessao"]
        check("first reply on A", not m.get("erro") and "OK" in m["texto"].upper())
        check("session file on A", sessao(A, sid) and not sessao(B, sid))
        c = s.api(f"/api/chats/{c['id']}/conta", {"conta": B["id"]})
        check("moved to B, nothing left on A", sessao(B, sid) and not sessao(A, sid))
        c, m = s.envia(c["id"], "What is the code word I gave you? Reply with only the word.")
        check("B remembers the conversation", palavra in m["texto"] and not m.get("erro"), m["texto"])
        check("same session id", c["sessao"] == sid)
        c = s.api(f"/api/chats/{c['id']}/conta", {"conta": A["id"]})
        check("moved back to A", sessao(A, sid) and not sessao(B, sid))
        c, m = s.envia(c["id"], "Repeat the code word, then a space and the number 7. Nothing else.")
        check("A remembers after the round trip", f"{palavra} 7" in m["texto"], m["texto"])

        print("== 2. tools")
        c = novo(A)
        notas = s.proj / "notes.txt"
        c, m = s.envia(c["id"], "Use the Write tool to create notes.txt in the current folder containing exactly the line alpha. Then reply DONE.")
        check("file written on A", notas.exists() and notas.read_text().strip() == "alpha")
        c = s.api(f"/api/chats/{c['id']}/conta", {"conta": B["id"]})
        c, m = s.envia(c["id"], "Use the Edit tool to add a second line beta to notes.txt (keep alpha). Then reply DONE.")
        check("B edits the same file", notas.read_text().split() == ["alpha", "beta"])
        c, m = s.envia(c["id"], "Which tools did you use in this conversation so far? Reply with just their names, comma separated.")
        check("B sees the tool calls made on A", "Write" in m["texto"] and "Edit" in m["texto"], m["texto"])

        print("== 3. an edited message, then a move")
        c = novo(A)
        c, m = s.envia(c["id"], "My favourite colour is teal. Reply with only: OK")
        c, m = s.envia(c["id"], "My pet is called Bolt. Reply with only: OK")
        orig = c["sessao"]
        s.api(f"/api/chats/{c['id']}/reenviar", {"indice": 2, "texto": "My pet is called Nimbus. Reply with only: OK"})
        time.sleep(0.5)
        c = s.espera(c["id"])
        ramo = c["sessao"]
        check("the edit forks a new session", ramo != orig and orig in (c.get("antigas") or []))
        c = s.api(f"/api/chats/{c['id']}/conta", {"conta": B["id"]})
        check("both branches moved", all(sessao(B, x) and not sessao(A, x) for x in (orig, ramo)))
        c, m = s.envia(c["id"], "What is my favourite colour and what is my pet called? Reply as: colour, pet")
        t = m["texto"].lower()
        check("B sees the edited branch", "teal" in t and "nimbus" in t and "bolt" not in t, m["texto"])
        editado = c["id"]

        print("== 4. a fork moved before its first message")
        f = s.api(f"/api/chats/{editado}/bifurcar", {"indice": 1})
        f = s.api(f"/api/chats/{f['id']}/conta", {"conta": A["id"]})
        check("the shared session is copied, B keeps it", sessao(A, ramo) and sessao(B, ramo))
        f, m = s.envia(f["id"], "Did I tell you my pet's name yet? And what is my favourite colour? Reply as: yes/no, colour")
        check("the fork on A only knows what came before the fork", m["texto"].lower().startswith("no") and "teal" in m["texto"].lower(), m["texto"])
        c, m = s.envia(editado, "What is my pet called? One word.")
        check("the original chat on B still works", "nimbus" in m["texto"].lower(), m["texto"])

        print("== 5. claude --resume in a terminal finds a moved session")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
        env["CLAUDE_CONFIG_DIR"] = str(dir_conta(B))
        r = subprocess.run(["claude", "-p", "--model", "haiku", "--resume", ramo, "--no-session-persistence",
                            "What is my favourite colour? One word."], cwd=str(s.proj), env=env,
                           capture_output=True, text=True, timeout=300)
        check("terminal resume on B", "teal" in r.stdout.lower(), r.stdout or r.stderr)

        print("== 6. one memory for both accounts")
        outra = B if principal == A["id"] else A
        dona = A if principal == A["id"] else B
        fruta = "mango" + str(secrets.randbelow(900) + 100)
        c = novo(outra)
        c, m = s.envia(c["id"], f"Save a new memory with your memory system: the test fruit for this folder is {fruta}. Then reply DONE.")
        pasta = dir_conta(dona) / "projects" / nome / "memory"
        achou = pasta.is_dir() and any(fruta in p.read_text(errors="replace") for p in pasta.glob("*.md"))
        check(f"a chat on {outra['id']} wrote to the memory of {dona['id']}", achou, sorted(p.name for p in pasta.glob("*")) if pasta.is_dir() else "no folder")
        c = novo(dona)
        c, m = s.envia(c["id"], "What is the test fruit for this folder, according to your memory? Reply with only the word.")
        check(f"a new chat on {dona['id']} recalls it", fruta in m["texto"].lower(), m["texto"])
    finally:
        s.parar()
        for c in (A, B):
            alvo = dir_conta(c) / "projects" / nome
            if alvo.is_dir() and nome.startswith("-tmp-agents-web-e2e-"):
                shutil.rmtree(alvo)
        shutil.rmtree(s.tmp, ignore_errors=True)
    ok = sum(1 for _, x in RES if x)
    print(f"\n{ok}/{len(RES)} passed")
    for n, x in RES:
        if not x:
            print("  failed:", n)
    sys.exit(0 if ok == len(RES) else 1)


if __name__ == "__main__":
    main()
