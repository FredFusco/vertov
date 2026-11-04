"""Prepara tutto il necessario e poi avvia l'analisi completa (descrivi.py).

Usato dall'app quando si preme "Descrivi il film":
- se Ollama non è in esecuzione lo avvia (l'app Ollama, o "ollama serve");
- se il modello di visione non è ancora scaricato lo scarica, mostrando l'avanzamento;
- poi passa la mano a descrivi.py con gli stessi argomenti.

    python prepara.py --modello NOME -- descrivi.py VIDEO --out CARTELLA ...
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import coordinamento
import progresso
from modello import MODELLO

OLLAMA = "http://localhost:11434"


def _modelli() -> list:
    with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
        return [m["name"] for m in json.load(r).get("models", [])]


def avvia_ollama() -> None:
    try:
        _modelli()
        return
    except Exception:
        pass
    progresso.fase("ollama")
    print("Ollama non è in esecuzione: lo avvio...", flush=True)
    if Path("/Applications/Ollama.app").exists():
        subprocess.run(["open", "-g", "-a", "Ollama"])
    else:
        eseguibile = shutil.which("ollama") or "/opt/homebrew/bin/ollama"
        subprocess.Popen([eseguibile, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(60):
        time.sleep(1)
        try:
            _modelli()
            print("Ollama pronto.", flush=True)
            return
        except Exception:
            pass
    sys.exit("Non riesco ad avviare Ollama. Aprilo a mano (app Ollama) e riprova.")


def scarica_modello(modello: str) -> None:
    if any(m == modello or m.startswith(modello + ":") for m in _modelli()):
        return
    progresso.fase("modello")
    print(f"Scarico il modello di visione {modello} (una volta sola, circa 16 GB)...", flush=True)
    corpo = json.dumps({"model": modello, "stream": True}).encode()
    richiesta = urllib.request.Request(f"{OLLAMA}/api/pull", data=corpo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(richiesta, timeout=None) as r:
        for riga in r:
            stato = json.loads(riga or b"{}")
            if stato.get("error"):
                sys.exit(f"Download del modello non riuscito: {stato['error']}")
            if stato.get("total") and stato.get("completed") is not None:
                # In MB, così i numeri restano interi e leggibili nell'app.
                progresso.avanzamento("modello", stato["completed"] // 2**20, stato["total"] // 2**20)
    print("Modello pronto.", flush=True)


def main() -> None:
    separatore = sys.argv.index("--")
    opzioni, resto = sys.argv[1:separatore], sys.argv[separatore + 1:]
    modello = opzioni[opzioni.index("--modello") + 1] if "--modello" in opzioni else MODELLO
    coordinamento.sorveglia_app()  # anche il download del modello si ferma se l'app non c'è più
    avvia_ollama()
    scarica_modello(modello)
    os.execv(sys.executable, [sys.executable, "-u", *resto])


if __name__ == "__main__":
    main()
