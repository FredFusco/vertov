"""Convivenza di Vertov con sé stesso e con le altre app della famiglia (Murnau, Ozu).

- Registro delle analisi in corso: ogni analisi (lanciata dall'app o da Terminale) scrive
  un file in ~/Library/Application Support/Vertov/lavori/ e lo toglie quando finisce. Così
  l'app vede anche le analisi partite fuori da lei, e nessuna resta accesa per ore di nascosto.
- Sorveglianza: un'analisi lanciata dall'app si spegne da sola se l'app non c'è più (chiusa,
  forzata, andata in crash).
- Turni sul modello: Vertov usa Gemma 4 (gemma4:26b), che usano anche Murnau e Ozu con contesti diversi
  (Murnau usa anche Qwen3-VL, solo suo), e Ollama ricarica il modello (circa 17 GB) ogni volta che il
  contesto cambia. Vertov ha la precedenza:
  Murnau e Ozu riconoscono un'analisi di Vertov dal programma descrivi.py e aspettano.
  Per questo ogni richiesta di Vertov usa lo stesso contesto (CONTESTO).
- Memoria: a fine analisi Vertov toglie il modello dalla memoria, se è la copia caricata da
  lui e nessun'altra analisi di Vertov lo sta usando.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

DATI = Path.home() / "Library" / "Application Support" / "Vertov"
LAVORI = DATI / "lavori"
OLLAMA = "http://localhost:11434"
CONTESTO = 16384      # token di contesto chiesti a Ollama: lo stesso in ogni richiesta di Vertov
TENUTA = "5m"         # quanto Ollama tiene il modello in memoria dopo l'ultima richiesta di Vertov
VARIABILE_GENITORE = "VERTOV_APP_PID"   # pid del server dell'app che ha lanciato l'analisi
# (VERTOV_APP invece è il percorso di Vertov.app, che il server usa per riaprirla dopo un aggiornamento)
# Le analisi delle app della famiglia si riconoscono dal nome del file del programma, con o senza percorso:
# girano da dentro le app (~/Applications/Vertov.app/Contents/Resources/programma/descrivi.py), dal codice
# sorgente (vertov/descrivi.py) o dalla loro cartella (descrivi.py). Murnau cerca murnau.py, Ozu traduci.py,
# e tutte e due riconoscono un'analisi di Vertov da descrivi.py.
PROGRAMMA = "descrivi.py"


def _processi() -> list:
    """Righe "pid durata comando" dei soli processi il cui eseguibile è Python.

    Si guarda l'eseguibile vero (non il testo del comando): una shell o un altro programma che
    nomina nel comando sia "python" sia il percorso di un'analisi non deve sembrare un'analisi.
    """
    try:
        eseguibili = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=5).stdout
        comandi = subprocess.run(["ps", "-axo", "pid=,etime=,command="], capture_output=True, text=True,
                                 timeout=5).stdout
    except Exception:
        return []
    python = {r.split(None, 1)[0] for r in eseguibili.splitlines()
              if len(r.split(None, 1)) == 2 and "python" in Path(r.split(None, 1)[1]).name.lower()}
    return [r for r in comandi.splitlines() if r.split(None, 1)[:1] and r.split(None, 1)[0] in python]


def _esegue(riga: str, programma: str) -> bool:
    """Python che esegue proprio quel programma: il nome del file come argomento a sé, con o senza percorso
    («descrivi.py», «…/programma/descrivi.py»), non un nome che lo contiene («ridescrivi.py»)."""
    return re.search(r"(^|\s|/)" + re.escape(programma) + r"(\s|$)", riga) is not None


def e_vertov(pid: int) -> bool:
    """True se il pid è un'analisi di Vertov in esecuzione (prima di fermarla, per sicurezza)."""
    for riga in _processi():
        parti = riga.split(None, 2)
        if len(parti) == 3 and parti[0] == str(pid):
            return _esegue(parti[2], PROGRAMMA)
    return False


# --------------------------------------------------------------------------
# Registro delle analisi in corso
# --------------------------------------------------------------------------

class Registro:
    """Il file di questa analisi in LAVORI/<pid>.json, tolto all'uscita."""

    def __init__(self, video: str, out: str):
        self.file = LAVORI / f"{os.getpid()}.json"
        self.dati = {"pid": os.getpid(), "video": video, "out": out, "avviata": time.time(),
                     "origine": "app" if os.environ.get(VARIABILE_GENITORE) else "terminale",
                     "fase": "avvio", "fatti": 0, "totale": 0}
        self.finita = False
        self._ultima = 0.0
        LAVORI.mkdir(parents=True, exist_ok=True)
        self._scrivi()

    def _scrivi(self) -> None:
        try:
            self.file.write_text(json.dumps(self.dati, ensure_ascii=False))
        except OSError:
            pass

    def aggiorna(self, **valori) -> None:
        self.dati.update(valori)
        if "fase" in valori or time.time() - self._ultima > 5:
            self._ultima = time.time()
            self._scrivi()

    def chiudi(self) -> None:
        self.file.unlink(missing_ok=True)


def analisi_in_corso() -> list:
    """Le analisi di Vertov in esecuzione su questo Mac, comprese quelle partite da Terminale.

    I file di analisi finite male (processo non più vivo) vengono tolti.
    """
    vive = {}
    for riga in _processi():
        parti = riga.split(None, 2)
        if len(parti) == 3 and _esegue(parti[2], PROGRAMMA):
            vive[int(parti[0])] = parti[1]
    elenco = []
    for f in LAVORI.glob("*.json") if LAVORI.exists() else []:
        try:
            dati = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if dati.get("pid") in vive:
            elenco.append(dati)
        else:
            f.unlink(missing_ok=True)
    # Analisi lanciate da una versione precedente, senza registro: si vedono comunque.
    noti = {d["pid"] for d in elenco}
    for pid, durata in vive.items():
        if pid not in noti:
            elenco.append({"pid": pid, "video": "", "origine": "sconosciuta", "durata_ps": durata})
    return elenco


# --------------------------------------------------------------------------
# Sorveglianza dell'app
# --------------------------------------------------------------------------

def sorveglia_app() -> None:
    """Se l'analisi è stata lanciata dall'app, si ferma appena l'app non c'è più."""
    pid = os.environ.get(VARIABILE_GENITORE)
    if not pid:
        return

    def controlla():
        while True:
            time.sleep(1)
            try:
                os.kill(int(pid), 0)
            except (ProcessLookupError, ValueError):
                os.kill(os.getpid(), signal.SIGTERM)  # uscita ordinata: si toglie il registro, si libera il modello
                return
            except PermissionError:
                pass  # esiste ancora

    threading.Thread(target=controlla, daemon=True).start()


# --------------------------------------------------------------------------
# Modello
# --------------------------------------------------------------------------

def libera_modello(modello: str) -> None:
    """Toglie il modello dalla memoria se è la copia caricata da Vertov e nessun'altra sua analisi lo usa."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=3) as r:
            caricati = json.load(r).get("models", [])
        nostro = any(m.get("name") == modello and m.get("context_length") == CONTESTO for m in caricati)
        altre = [a for a in analisi_in_corso() if a["pid"] != os.getpid()]
        if not nostro or altre:
            return
        corpo = json.dumps({"model": modello, "keep_alive": 0}).encode()
        urllib.request.urlopen(urllib.request.Request(f"{OLLAMA}/api/generate", data=corpo,
                                                      headers={"Content-Type": "application/json"}), timeout=30)
    except Exception:
        pass  # Ollama spento o modello già scarico


def a_batteria() -> bool:
    """True se il Mac va a batteria: il processore grafico rallenta a circa metà velocità."""
    try:
        return "Battery Power" in subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True,
                                                 timeout=5).stdout.splitlines()[0]
    except Exception:
        return False


def notifica(testo: str) -> None:
    """Notifica di macOS: un'analisi lanciata da Terminale non passa inosservata."""
    try:
        subprocess.Popen(["osascript", "-e", f'display notification {json.dumps(testo)} with title "Vertov"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
