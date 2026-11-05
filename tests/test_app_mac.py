"""Test di Vertov come app del Mac: le scelte dei file fatte da Vertov.app, il server con --servizio, lo scambio
dell'app durante un aggiornamento, il riconoscimento delle analisi e i modelli protetti di Libera spazio.
Senza finestre vere, senza rete e senza Ollama; il server di prova gira su una porta libera e con una cartella
home provvisoria, così non tocca le sessioni vere né un Vertov aperto.

    ~/.venvs/descrittore/bin/python -m unittest discover -s tests -v
"""

import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

CODICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODICE))

import aggiornamenti  # noqa: E402
import app  # noqa: E402
import coordinamento  # noqa: E402
import pulizia  # noqa: E402


class ProvaScelte(unittest.TestCase):
    """Le finestre di scelta le apre Vertov.app, che manda i percorsi scelti nella richiesta."""

    def test_file_scelti_dall_app(self):
        self.assertEqual(app.scegli("file", "Materiali", {"percorsi": ["/a/film.mov", "/b/documenti/"]}),
                         ["/a/film.mov", "/b/documenti"])

    def test_cartella_scelta_dall_app(self):
        self.assertEqual(app.scegli("cartella", "Cartella", {"percorsi": ["/Volumes/Disco/Esportazioni/"]}),
                         "/Volumes/Disco/Esportazioni")

    def test_scelta_annullata(self):
        self.assertIsNone(app.scegli("file", "Materiali", {"percorsi": []}))
        self.assertIsNone(app.scegli("cartella", "Cartella", {"percorsi": []}))

    def test_senza_percorsi_in_servizio(self):
        with mock.patch("app.SERVIZIO", True):
            with self.assertRaises(RuntimeError):
                app.scegli("file", "Materiali", {})


def porta_libera() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Il server di prova: app.py --servizio su un'altra porta, senza riaprire davvero Vertov dopo un «riavvia».
# Con LAVORO_FINTO avvia un lavoro che dura due minuti, per vedere che SIGTERM lo ferma.
SERVIZIO_DI_PROVA = """
import os, sys
sys.argv = ["app.py", "--servizio"]
import app, aggiornamenti
app.PORTA = int(os.environ["PORTA_DI_PROVA"])
aggiornamenti.riavvia = lambda porta: None
if os.environ.get("LAVORO_FINTO"):
    import threading, time
    def finto():
        time.sleep(1)
        app.LAVORO.avvia([sys.executable, "-c", "import time; time.sleep(120)"], "descrizioni", "prova")
        print("LAVORO", app.LAVORO.processo.pid, flush=True)
    threading.Thread(target=finto, daemon=True).start()
app.main()
"""


class ProvaServizio(unittest.TestCase):
    def setUp(self):
        self.casa = tempfile.TemporaryDirectory()
        self.porta = porta_libera()
        self.ambiente = {**os.environ, "HOME": self.casa.name, "PORTA_DI_PROVA": str(self.porta),
                         "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}
        self.processo = None

    def tearDown(self):
        if self.processo and self.processo.poll() is None:
            self.processo.kill()
            self.processo.wait()
        if self.processo:
            self.processo.stdout.close()
        self.casa.cleanup()

    def avvia(self, **altro):
        self.processo = subprocess.Popen([sys.executable, "-c", SERVIZIO_DI_PROVA], cwd=CODICE,
                                         env={**self.ambiente, **altro}, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True)

    def chiedi(self, percorso: str, dati: dict = None):
        corpo = None if dati is None else json.dumps(dati).encode()
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{self.porta}{percorso}", data=corpo),
                                    timeout=5) as r:
            return json.load(r)

    def aspetta_pronto(self):
        for _ in range(150):
            try:
                return self.chiedi("/api/sessioni")
            except OSError:
                self.assertIsNone(self.processo.poll(), "il server si è fermato da solo")
                time.sleep(0.1)
        self.fail("il server non risponde")

    def test_risponde_e_si_ferma_con_sigterm(self):
        self.avvia()
        self.assertIn("sessioni", self.aspetta_pronto())
        self.assertEqual(self.chiedi("/api/preferenze")["chiudi_senza_chiedere"], False)
        self.chiedi("/api/preferenze", {"chiudi_senza_chiedere": True})
        self.assertEqual(self.chiedi("/api/preferenze")["chiudi_senza_chiedere"], True)
        # Senza percorsi, in servizio la finestra di scelta non la apre il server.
        with self.assertRaises(urllib.error.HTTPError) as rifiutata:
            self.chiedi("/api/sfoglia", {"tipo": "file"})
        self.assertIn("Vertov.app", json.load(rifiutata.exception)["errore"])
        rifiutata.exception.close()
        # Con i percorsi scelti dall'app: annullata non aggiunge niente.
        self.assertEqual(self.chiedi("/api/sfoglia", {"tipo": "file", "percorsi": []})["esiti"], [])
        self.processo.send_signal(signal.SIGTERM)
        self.assertEqual(self.processo.wait(timeout=5), 0)

    def test_sigterm_ferma_il_lavoro_in_corso(self):
        self.avvia(LAVORO_FINTO="1")
        self.aspetta_pronto()
        riga = ""
        limite = time.time() + 15
        while not riga.startswith("LAVORO") and time.time() < limite:
            riga = self.processo.stdout.readline()
        self.assertTrue(riga.startswith("LAVORO"), "il lavoro di prova non è partito")
        pid = int(riga.split()[1])
        os.kill(pid, 0)  # c'è
        self.processo.send_signal(signal.SIGTERM)
        self.assertEqual(self.processo.wait(timeout=5), 0)
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("il lavoro è rimasto acceso dopo la chiusura del server")

    def test_porta_occupata(self):
        with socket.socket() as occupata:
            occupata.bind(("127.0.0.1", self.porta))
            occupata.listen()
            self.avvia()
            self.assertEqual(self.processo.wait(timeout=30), app.CODICE_PORTA_OCCUPATA)

    def test_riavvio_dopo_un_aggiornamento(self):
        self.avvia()
        self.aspetta_pronto()
        self.assertEqual(self.chiedi("/api/aggiornamenti", {"azione": "riavvia"}), {"ok": True})
        self.assertEqual(self.processo.wait(timeout=10), app.CODICE_RIAVVIO)


class ProvaInstallazioneApp(unittest.TestCase):
    """L'aggiornamento di Vertov sostituisce tutta Vertov.app; quella di prima va in versioni_precedenti."""

    def pacchetto(self, cartella: Path, testo: str) -> bytes:
        nuova = cartella / "nuova" / "Vertov.app"
        for rel, contenuto in (("Contents/MacOS/Vertov", "eseguibile"),
                               ("Contents/Resources/programma/app.py", testo)):
            (nuova / rel).parent.mkdir(parents=True, exist_ok=True)
            (nuova / rel).write_text(contenuto)
        (nuova / "Contents/MacOS/Vertov").chmod(0o755)
        dati = io.BytesIO()
        with zipfile.ZipFile(dati, "w") as z:
            for f in nuova.rglob("*"):
                if f.is_file():
                    z.write(f, "Vertov.app/" + str(f.relative_to(nuova)))
        return dati.getvalue()

    def test_sostituisce_l_app_e_conserva_la_vecchia(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            installata = d / "Applications" / "Vertov.app"
            (installata / "Contents/Resources/programma").mkdir(parents=True)
            (installata / "Contents/Resources/programma/app.py").write_text("vecchia")
            zip_ = self.pacchetto(d, "nuova")
            risposta = mock.MagicMock()
            risposta.__enter__.return_value = risposta
            risposta.headers = {"Content-Length": str(len(zip_))}
            risposta.read.side_effect = [zip_, b""]
            voce = {"nuova": "9.9.9", "pacchetto": "https://esempio.prova/vertov.zip",
                    "sha256": hashlib.sha256(zip_).hexdigest()}
            with mock.patch.object(aggiornamenti, "APP", installata), \
                    mock.patch.object(aggiornamenti, "PRECEDENTI", d / "precedenti"), \
                    mock.patch.object(aggiornamenti, "versione", return_value="1.0.0"), \
                    mock.patch("aggiornamenti.urllib.request.urlopen", return_value=risposta):
                aggiornamenti._installa_app(voce)
            self.assertEqual((installata / "Contents/Resources/programma/app.py").read_text(), "nuova")
            self.assertTrue(os.access(installata / "Contents/MacOS/Vertov", os.X_OK))
            self.assertEqual((d / "precedenti/1.0.0/Vertov.app/Contents/Resources/programma/app.py").read_text(),
                             "vecchia")
            self.assertEqual(sorted(p.name for p in installata.parent.iterdir()), ["Vertov.app"])  # niente avanzi
            self.assertTrue(aggiornamenti.INSTALLAZIONE["riavvia"])

    def test_impronta_sbagliata_non_tocca_niente(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            installata = d / "Applications" / "Vertov.app"
            (installata / "Contents/Resources/programma").mkdir(parents=True)
            (installata / "Contents/Resources/programma/app.py").write_text("vecchia")
            zip_ = self.pacchetto(d, "nuova")
            risposta = mock.MagicMock()
            risposta.__enter__.return_value = risposta
            risposta.headers = {}
            risposta.read.side_effect = [zip_, b""]
            voce = {"nuova": "9.9.9", "pacchetto": "https://esempio.prova/vertov.zip", "sha256": "0" * 64}
            with mock.patch.object(aggiornamenti, "APP", installata), \
                    mock.patch.object(aggiornamenti, "PRECEDENTI", d / "precedenti"), \
                    mock.patch("aggiornamenti.urllib.request.urlopen", return_value=risposta):
                with self.assertRaises(RuntimeError):
                    aggiornamenti._installa_app(voce)
            self.assertEqual((installata / "Contents/Resources/programma/app.py").read_text(), "vecchia")

    def test_pacchetto_senza_vertov(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            installata = d / "Applications" / "Vertov.app"
            installata.mkdir(parents=True)
            zip_ = d / "altro.zip"
            with zipfile.ZipFile(zip_, "w") as z:
                z.writestr("app.py", "un pacchetto del vecchio tipo")
            with mock.patch.object(aggiornamenti, "APP", installata):
                with self.assertRaises(RuntimeError):
                    aggiornamenti.scambia_app(zip_, d / "estratti")
            self.assertTrue(installata.is_dir())

    def test_dal_codice_sorgente_non_si_installa(self):
        self.assertIsNone(aggiornamenti.APP)  # questi test girano dal codice sorgente
        with self.assertRaises(RuntimeError) as e:
            aggiornamenti._installa_app({"nuova": "9.9.9"})
        self.assertIn("compila.sh --installa", str(e.exception))


class ProvaProcessi(unittest.TestCase):
    """Le analisi delle app della famiglia si riconoscono dal nome del file, con o senza percorso."""

    def test_con_e_senza_percorso(self):
        for riga in ["/Users/x/.venvs/descrittore/bin/python -u "
                     "/Users/x/Applications/Vertov.app/Contents/Resources/programma/descrivi.py /film/RULLO 1.mov --out x",
                     "/Users/x/.venvs/descrittore/bin/python -u /Users/x/My Drive/CLAUDE/vertov/descrivi.py film.mov",
                     "python descrivi.py film.mov --edl film.edl",
                     "python descrivi.py"]:
            self.assertTrue(coordinamento._esegue(riga, "descrivi.py"), riga)

    def test_nomi_che_lo_contengono_non_valgono(self):
        for riga in ["python ridescrivi.py film.mov", "vim descrivi.py.bak", "python descrivi.pyc"]:
            self.assertFalse(coordinamento._esegue(riga, "descrivi.py"), riga)

    def test_le_app_sorelle(self):
        self.assertTrue(coordinamento._esegue(
            "/x/python /Users/x/Applications/Murnau.app/Contents/Resources/programma/murnau.py film.mov", "murnau.py"))
        self.assertTrue(coordinamento._esegue("python traduci.py film.srt", "traduci.py"))
        self.assertFalse(coordinamento._esegue("python murnau.py film.mov", "descrivi.py"))

    def test_analisi_in_corso_dalla_app_installata(self):
        righe = ["501 01:02 /Users/x/.venvs/descrittore/bin/python -u "
                 "/Users/x/Applications/Vertov.app/Contents/Resources/programma/descrivi.py film.mov",
                 "502 00:10 python descrivi.py altro.mov",
                 "503 00:05 python /Users/x/Applications/Murnau.app/Contents/Resources/programma/murnau.py film.mov"]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(coordinamento, "LAVORI", Path(d)), \
                mock.patch.object(coordinamento, "_processi", return_value=righe):
            self.assertEqual(sorted(a["pid"] for a in coordinamento.analisi_in_corso()), [501, 502])
            self.assertTrue(coordinamento.e_vertov(501))
            self.assertFalse(coordinamento.e_vertov(503))


class ProvaModelliProtetti(unittest.TestCase):
    """Libera spazio non propone mai i modelli delle app sorelle: dal codice sorgente e dalle app installate."""

    def test_codice_e_app_installate(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "codice" / "ozu").mkdir(parents=True)
            (d / "codice" / "ozu" / "revisione.py").write_text('REVISORE = "qwen3.8:27b-dflash2-nvfp4"\n')
            programma = d / "app" / "Murnau.app" / "Contents" / "Resources" / "programma"
            programma.mkdir(parents=True)
            (programma / "rilevatori.py").write_text('MODELLO_ESAME = "qwen3-vl:30b-a3b-instruct"\n'
                                                      'OLLAMA = "http://localhost:11434"\n')
            with mock.patch.object(pulizia, "CODICE_FAMIGLIA", d / "codice"), \
                    mock.patch.object(pulizia, "APP_INSTALLATE", d / "app"):
                protetti = pulizia.modelli_protetti("gemma4:26b")
        self.assertEqual(protetti["gemma4:26b"], {"Vertov"})
        self.assertEqual(protetti["qwen3.8:27b-dflash2-nvfp4"], {"Ozu"})
        self.assertEqual(protetti["qwen3-vl:30b-a3b-instruct"], {"Murnau"})
        self.assertFalse(any("//" in m for m in protetti))

    def test_modelli_veri_della_famiglia(self):
        # Sul Mac di lavoro: Vertov usa Gemma 4, e i modelli di Murnau e Ozu restano protetti.
        protetti = pulizia.modelli_protetti(app.MODELLO)
        self.assertEqual(app.MODELLO, "gemma4:26b")
        self.assertIn("Vertov", protetti["gemma4:26b"])


if __name__ == "__main__":
    unittest.main()
