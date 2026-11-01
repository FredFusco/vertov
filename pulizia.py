"""Libera spazio: file temporanei e di cache creati da Vertov.

Ogni voce dice quanto occupa e se si può cancellare senza pensarci (si ricrea da
sola quando serve) o se conviene lasciarla. Non tocca mai i materiali, i risultati
delle analisi né le sessioni: solo cose che Vertov ha creato o scaricato.
"""

import json
import shutil
import tempfile
import urllib.request
from pathlib import Path

DATI = Path.home() / "Library" / "Application Support" / "Vertov"
CACHE = Path.home() / ".cache" / "descrittore"
HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"
PIP_CACHE = Path.home() / "Library" / "Caches" / "pip"
OLLAMA = "http://localhost:11434"
# Modelli Hugging Face che Vertov usa davvero (audio.py): gli altri sono resti di prove.
MODELLI_HF_USATI = {"mlx-community/whisper-large-v3-turbo", "MIT/ast-finetuned-audioset-10-10-0.4593"}


def _dimensione(percorso: Path) -> int:
    if percorso.is_file():
        return percorso.stat().st_size
    totale = 0
    for f in percorso.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                totale += f.stat().st_size
        except OSError:
            pass
    return totale


def _cartelle_fotogrammi() -> list:
    return sorted(Path(tempfile.gettempdir()).glob("descrittore_*"))


def _registri() -> list:
    # vertov.log si svuota (l'app ci sta scrivendo), gli altri sono resti delle versioni precedenti.
    return [p for p in [CACHE / "vertov.log", CACHE / "kinoglaz.log", CACHE / "ultimo_progetto.json"] if p.exists()]


def _modelli_hf_non_usati() -> list:
    """Cartelle di modelli Hugging Face non usati da Vertov, con la dimensione dei loro file."""
    def file_veri(cartella):
        # I file veri stanno in blobs/ (anche condivisi tra modelli); snapshots/ contiene solo collegamenti.
        return {f.resolve() for f in (cartella / "snapshots").rglob("*") if f.is_symlink()}

    cartelle = {c.name[len("models--"):].replace("--", "/", 1): c for c in HF_CACHE.glob("models--*")}
    usati = set().union(*(file_veri(c) for r, c in cartelle.items() if r in MODELLI_HF_USATI))
    esito = []
    for repo, cartella in cartelle.items():
        if repo in MODELLI_HF_USATI:
            continue
        blobs = file_veri(cartella) - usati
        esito.append((repo, cartella, sum(b.stat().st_size for b in blobs if b.exists()), blobs))
    return esito


def _modelli_ollama_non_usati(modello: str) -> list:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
            return [(m["name"], m.get("size", 0)) for m in json.load(r).get("models", []) if m["name"] != modello]
    except Exception:
        return []


def voci(modello: str) -> list:
    """Cosa si può cancellare, con la dimensione in byte."""
    v = []
    fotogrammi = _cartelle_fotogrammi()
    v.append({"chiave": "fotogrammi", "nome": "Fotogrammi estratti dai video",
              "spiega": "Le immagini delle inquadrature usate durante l'analisi. Se servono di nuovo, Vertov le ricrea.",
              "byte": sum(_dimensione(p) for p in fotogrammi), "consigliata": True})
    v.append({"chiave": "versioni", "nome": "Versioni precedenti di Vertov",
              "spiega": "Copie dei file sostituiti dagli aggiornamenti, per poter tornare indietro.",
              "byte": _dimensione(DATI / "versioni_precedenti") if (DATI / "versioni_precedenti").exists() else 0,
              "consigliata": True})
    v.append({"chiave": "registri", "nome": "Registri",
              "spiega": "Il diario tecnico delle elaborazioni, utile solo per capire un errore.",
              "byte": sum(_dimensione(p) for p in _registri()), "consigliata": True})
    v.append({"chiave": "pip", "nome": "File scaricati per installare le librerie",
              "spiega": "Copie dei pacchetti Python scaricati durante le installazioni. Non servono più.",
              "byte": _dimensione(PIP_CACHE) if PIP_CACHE.exists() else 0, "consigliata": True})
    hf = _modelli_hf_non_usati()
    v.append({"chiave": "modelli_hf", "nome": "Modelli di prova non usati",
              "spiega": "Modelli scaricati durante lo sviluppo e che Vertov non usa: " +
                        (", ".join(r for r, *_ in hf) or "nessuno") + ".",
              "byte": sum(b for _, _, b, _ in hf), "consigliata": True})
    ollama = _modelli_ollama_non_usati(modello)
    v.append({"chiave": "modelli_ollama", "nome": "Altri modelli in Ollama",
              "spiega": "Modelli di Ollama diversi da quello che usa Vertov (" + modello + "): " +
                        (", ".join(n for n, _ in ollama) or "nessuno") +
                        ". Lasciali se usi Ollama anche per altro.",
              "byte": sum(b for _, b in ollama), "consigliata": False})
    return [x for x in v if x["byte"] > 0]


def cancella(chiavi: list, modello: str) -> int:
    """Cancella le voci scelte e restituisce i byte liberati."""
    liberati = 0
    if "fotogrammi" in chiavi:
        for p in _cartelle_fotogrammi():
            liberati += _dimensione(p)
            shutil.rmtree(p, ignore_errors=True)
    if "versioni" in chiavi and (DATI / "versioni_precedenti").exists():
        liberati += _dimensione(DATI / "versioni_precedenti")
        shutil.rmtree(DATI / "versioni_precedenti", ignore_errors=True)
    if "registri" in chiavi:
        for p in _registri():
            liberati += _dimensione(p)
            if p.name == "vertov.log":
                p.open("w").close()
            else:
                p.unlink(missing_ok=True)
    if "pip" in chiavi and PIP_CACHE.exists():
        liberati += _dimensione(PIP_CACHE)
        shutil.rmtree(PIP_CACHE, ignore_errors=True)
    if "modelli_hf" in chiavi:
        for _, cartella, byte, blobs in _modelli_hf_non_usati():
            for b in blobs:
                b.unlink(missing_ok=True)
            shutil.rmtree(cartella, ignore_errors=True)
            liberati += byte
    if "modelli_ollama" in chiavi:
        for nome, byte in _modelli_ollama_non_usati(modello):
            richiesta = urllib.request.Request(f"{OLLAMA}/api/delete", method="DELETE",
                                               data=json.dumps({"model": nome}).encode(),
                                               headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(richiesta, timeout=30)
                liberati += byte
            except Exception:
                pass
    return liberati
