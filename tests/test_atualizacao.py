#!/usr/bin/env python3
"""Tests for the updater of src/server.py: a fake release (file:// URLs, as GitHub lists them) and an installed
copy of the server in a throwaway folder. Nothing is downloaded and nothing outside the temporary folder changes.

    python3 -m unittest discover -s tests -v
"""
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import FAKE_CLAUDE, RAIZ, SERVIDOR, Servidor, porta_livre

NOVA = "9.9.9"
MAQ = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(platform.machine().lower(), platform.machine().lower())
DEB = {"x86_64": "amd64", "aarch64": "arm64"}.get(MAQ, MAQ)

# stands in for the install.sh of the new release: it only notes how it was called
INSTALL_SH = b'#!/bin/bash\necho "$@" > "$HOME/instalado"\n'


def tarball(versao=NOVA):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for nome, dados, modo in ((f"agents-web-{versao}/install.sh", INSTALL_SH, 0o755),
                                  (f"agents-web-{versao}/src/server.py", b"# new\n", 0o644)):
            info = tarfile.TarInfo(nome)
            info.size, info.mode = len(dados), modo
            tf.addfile(info, io.BytesIO(dados))
    return buf.getvalue()


class Instalada(Servidor):
    """The server copied to a folder that looks like an install of kind `tipo`, with a fake release."""

    def __init__(self, tipo, arquivos, somas="certas", digest=False, versao=NOVA, env=None, bins=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="agents-web-update-"))
        self.app = self.tmp / "app"
        self.app.mkdir()
        # from the checkout, or from the installed copy named by AGENTS_WEB_TEST_SERVER
        for f in ("server.py", "index.html", "agents-web.svg", "icon-192.png", "icon-512.png"):
            shutil.copy(next(p for p in (SERVIDOR.parent / f, RAIZ / "data" / f) if p.exists()), self.app)
        (self.app / "install-kind").write_text(tipo + "\n")
        self.home = self.tmp / "home"
        binarios = self.home / ".local/bin"
        binarios.mkdir(parents=True)
        (binarios / "claude").write_text(FAKE_CLAUDE)
        (binarios / "claude").chmod(0o755)
        for nome, texto in (bins or {}).items():
            (binarios / nome).write_text(texto)
            (binarios / nome).chmod(0o755)
        rel = self.tmp / "release"
        rel.mkdir()
        assets, linhas = [], []
        for nome, dados in arquivos.items():
            (rel / nome).write_bytes(dados)
            h = hashlib.sha256(dados).hexdigest()
            linhas.append(f"{'0' * 64 if somas == 'erradas' else h}  {nome}")
            assets.append({"name": nome, "browser_download_url": (rel / nome).as_uri(),
                           "digest": f"sha256:{h}" if digest else None})
        if somas:
            (rel / "SHA256SUMS").write_text("\n".join(linhas) + "\n")
            assets.append({"name": "SHA256SUMS", "browser_download_url": (rel / "SHA256SUMS").as_uri()})
        (rel / "release.json").write_text(json.dumps({"tag_name": f"v{versao}", "body": "notes", "assets": assets}))
        run = self.tmp / "run"
        run.mkdir(mode=0o700)
        self.porta = porta_livre()
        self.env = {"HOME": str(self.home), "PATH": f"{binarios}:/usr/bin:/bin", "XDG_RUNTIME_DIR": str(run),
                    "AGENTS_WEB_PORT": str(self.porta), "LANG": "C.UTF-8",
                    "AGENTS_WEB_RELEASES_API": (rel / "release.json").as_uri(), "AGENTS_WEB_UPDATE_DELAY": "3600",
                    **(env or {})}
        self.log = open(self.tmp / "server.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-I", "-u", str(self.app / "server.py")], env=self.env,
                                     cwd=str(self.tmp), stdout=self.log, stderr=subprocess.STDOUT)
        self.espera_subir()
        self.token = json.loads((self.home / ".config/agents-web/config.json").read_text())["token"]

    def espera_subir(self, limite=30):
        fim = time.time() + limite
        while time.time() < fim:
            try:
                if self.pedir("GET", "/saude")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        log = (self.tmp / "server.log").read_text()
        self.parar()
        raise RuntimeError("server did not start:\n" + log)

    def espera(self, cond, limite=20):
        fim = time.time() + limite
        while time.time() < fim:
            v = cond()
            if v:
                return v
            time.sleep(0.1)
        raise AssertionError("timed out; update.log:\n" + self.registro() + "\nserver.log:\n"
                             + (self.tmp / "server.log").read_text()[-3000:])

    def estado(self):
        return self.api("/api/estado")["atualizacao"]

    def registro(self):
        p = self.home / ".local/share/agents-web/update.log"
        return p.read_text() if p.exists() else ""

    def marca(self, nome="instalado"):
        p = self.home / nome
        return p.read_text() if p.exists() else ""


class TestAtualizacao(unittest.TestCase):
    def inst(self, *a, **k):
        s = Instalada(*a, **k)
        self.addCleanup(s.parar)
        return s

    def test_script_baixa_confere_e_instala(self):
        s = self.inst("script", {f"agents-web-{NOVA}-linux-{MAQ}.tar.gz": tarball()})
        d = s.api("/api/atualizacao?forcar=1")
        self.assertEqual((d["ultima"], d["nova"], d["tipo"], d["sozinha"], d["senha"]), (NOVA, True, "script", True, False))
        s.api("/api/atualizar", {})
        args = s.espera(s.marca)
        self.assertIn("--from", args)
        self.assertIn("--no-deps", args)
        self.assertIn("SHA256", s.registro())

    def test_soma_errada_nao_instala(self):
        nome = f"agents-web-{NOVA}-linux-{MAQ}.tar.gz"
        s = self.inst("script", {nome: tarball()}, somas="erradas")
        s.api("/api/atualizar", {})
        erro = s.espera(lambda: s.estado()["erro"])
        self.assertIn("does not match its SHA256", erro)
        time.sleep(1.5)
        self.assertEqual(s.marca(), "")
        self.assertFalse((s.home / ".local/share/agents-web/updates" / nome).exists())

    def test_sem_soma_nao_instala(self):
        s = self.inst("script", {f"agents-web-{NOVA}-linux-{MAQ}.tar.gz": tarball()}, somas=None)
        s.api("/api/atualizar", {})
        self.assertIn("no SHA256 sum", s.espera(lambda: s.estado()["erro"]))
        self.assertEqual(s.marca(), "")

    def test_digest_do_github_basta(self):
        s = self.inst("script", {f"agents-web-{NOVA}-linux-{MAQ}.tar.gz": tarball()}, somas=None, digest=True)
        s.api("/api/atualizar", {})
        self.assertIn("--from", s.espera(s.marca))

    def test_release_sem_arquivo_do_tipo(self):
        s = self.inst("rpm", {f"agents-web_{NOVA}_{DEB}.deb": b"x"})
        s.api("/api/atualizar", {})
        self.assertIn("has no file for this kind of install", s.espera(lambda: s.estado()["erro"]))

    def test_mesma_versao(self):
        s = self.inst("script", {}, versao="0.0.1")
        self.assertFalse(s.api("/api/atualizacao?forcar=1")["nova"])
        self.assertEqual(s.pedir("POST", "/api/atualizar", {}, {"X-Token": s.token})[0], 400)

    def test_pacote_pede_senha_e_reinicia(self):
        pk = '#!/bin/sh\necho "$@" > "$HOME/pkexec"\nexit 0\n'
        s = self.inst("deb", {f"agents-web_{NOVA}_{DEB}.deb": b"deb"}, bins={"pkexec": pk})
        d = s.api("/api/atualizacao?forcar=1")
        self.assertEqual((d["tipo"], d["sozinha"], d["senha"]), ("deb", False, True))
        pid = s.proc.pid
        s.api("/api/atualizar", {})
        cmd = s.espera(lambda: s.marca("pkexec")).split()
        self.assertIn(cmd[0], ("apt-get", "dpkg"))
        self.assertTrue(cmd[-1].endswith(f"agents-web_{NOVA}_{DEB}.deb"))
        s.espera(lambda: "restarting the server" in s.registro())
        time.sleep(1)
        s.espera_subir()
        self.assertEqual(s.proc.pid, pid)      # same process, so the tray keeps watching it
        self.assertIsNone(s.proc.poll())

    def test_pacote_senha_negada(self):
        s = self.inst("pacman", {f"agents-web-{NOVA}-1-{MAQ}.pkg.tar.zst": b"pkg"}, bins={"pkexec": "#!/bin/sh\nexit 126\n"})
        s.api("/api/atualizar", {})
        self.assertIn("password was not given", s.espera(lambda: s.estado()["erro"]))
        self.assertIsNone(s.proc.poll())

    def test_appimage_troca_o_arquivo(self):
        velho = b'#!/bin/sh\necho old\n'
        novo = b'#!/bin/sh\necho "$@" >> "$HOME/appimage"\n'
        tmp = Path(tempfile.mkdtemp(prefix="agents-web-ai-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        alvo = tmp / "Agents_Web.AppImage"
        alvo.write_bytes(velho)
        alvo.chmod(0o755)
        s = self.inst("appimage", {f"Agents_Web-{NOVA}-{MAQ}.AppImage": novo}, env={"APPIMAGE": str(alvo)})
        self.assertTrue(s.api("/api/atualizacao?forcar=1")["sozinha"])
        s.api("/api/atualizar", {})
        self.assertEqual(s.espera(lambda: s.marca("appimage")).strip(), "restart")
        self.assertEqual(alvo.read_bytes(), novo)
        self.assertTrue(os.access(alvo, os.X_OK))

    def test_appimage_sem_variavel_e_checkout(self):
        # an AppImage folder run without the AppImage runtime is treated as a checkout
        s = self.inst("appimage", {f"Agents_Web-{NOVA}-{MAQ}.AppImage": b"x"})
        self.assertEqual(s.api("/api/atualizacao?forcar=1")["tipo"], "")

    def test_automatica_quando_parado(self):
        s = self.inst("script", {f"agents-web-{NOVA}-linux-{MAQ}.tar.gz": tarball()},
                      env={"AGENTS_WEB_UPDATE_DELAY": "0.3", "AGENTS_WEB_UPDATE_EVERY": "0.3", "AGENTS_WEB_UPDATE_IDLE": "0"})
        self.assertIn("--from", s.espera(s.marca))
        self.assertIn("automatic", s.registro())

    def test_avisar_so_avisa(self):
        s = self.inst("script", {f"agents-web-{NOVA}-linux-{MAQ}.tar.gz": tarball()},
                      env={"AGENTS_WEB_UPDATE_DELAY": "2", "AGENTS_WEB_UPDATE_EVERY": "0.3", "AGENTS_WEB_UPDATE_IDLE": "0"})
        self.assertEqual(s.api("/api/preferencias", {"atualizar": "avisar"})["atualizar"], "avisar")
        s.espera(lambda: s.estado()["nova"], limite=10)
        time.sleep(2)
        self.assertEqual(s.marca(), "")

    def test_terminal(self):
        s = self.inst("script", {f"agents-web-{NOVA}-linux-{MAQ}.tar.gz": tarball()})
        r = subprocess.run([sys.executable, "-I", str(s.app / "server.py"), "--update"], env=s.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SHA256 checked", r.stdout)
        self.assertIn("--from", s.marca())


if __name__ == "__main__":
    unittest.main()
