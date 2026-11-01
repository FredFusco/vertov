"""Presenze dei personaggi per scena, lette dagli ordini del giorno (ODG).

Dalla tabella "CONVOCAZIONE CAST" di ogni ODG si ricava, per ogni scena,
quali personaggi erano convocati. Le scene tra parentesi, come "(6)" o
"(84vo)", indicano una presenza solo come voce o parziale.

Si tengono solo i nomi dei personaggi: i nomi degli interpreti vengono scartati.
"""

import re
from pathlib import Path

RIGA_CAST = re.compile(r"^\s*(\d{1,3})\s+(\D+?)\s+([(\d].*?)\s+(?:SD|\d{1,2}[.:]\d\d)\s")


def _testo_pdf(percorso: Path) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(percorso)).pages)


def _personaggio(nomi: str, noti: list) -> str:
    """Separa il personaggio dall'interprete nella colonna dei nomi."""
    nomi = re.sub(r"\(.*?\)|\*", " ", nomi)
    parole = nomi.split()
    for noto in sorted(noti, key=len, reverse=True):
        if " ".join(parole).upper().startswith(noto.upper()):
            return noto.title()
    # Personaggio non presente in sceneggiatura: l'interprete occupa di solito
    # le ultime due parole (nome e cognome).
    return " ".join(parole[:-2] if len(parole) > 2 else parole[:1]).title()


def _scene(campo: str) -> list:
    """"31-52-16-(6)" -> [("31", False), ..., ("6", True)]  (True = solo voce/parziale)."""
    risultato = []
    for pezzo in campo.split("-"):
        pezzo = pezzo.strip()
        m = re.search(r"(\d+[A-Z]?)", pezzo)
        if m:
            solo_voce = pezzo.startswith("(") or "vo" in pezzo.lower() or "v.o" in pezzo.lower()
            risultato.append((m.group(1), solo_voce))
    return risultato


def leggi_presenze(percorsi, personaggi_noti: list) -> dict:
    """{numero scena: {"in_campo": set, "voce": set}} dai PDF indicati (file o cartelle)."""
    if isinstance(percorsi, (str, Path)):
        percorsi = [percorsi]
    pdf_tutti = []
    for p in map(Path, percorsi):
        pdf_tutti += sorted(p.glob("*.pdf")) if p.is_dir() else [p]
    presenze = {}
    for pdf in pdf_tutti:
        testo = _testo_pdf(pdf)
        inizio = testo.find("CONVOCAZIONE CAST")
        if inizio < 0:
            continue
        fine = min([i for i in (testo.find("CONVOCAZIONE FIGURAZIONI", inizio),
                                testo.find("CONVOCAZIONE STUNT", inizio),
                                testo.find("NOTE PER I REPARTI", inizio)) if i > 0] or [len(testo)])
        for riga in testo[inizio:fine].splitlines():
            m = RIGA_CAST.match(riga + " ")
            if not m:
                continue
            nome = _personaggio(m.group(2), personaggi_noti)
            for scena, solo_voce in _scene(m.group(3)):
                voce = presenze.setdefault(scena, {"in_campo": set(), "voce": set()})
                if solo_voce:
                    if nome not in voce["in_campo"]:
                        voce["voce"].add(nome)
                else:
                    voce["in_campo"].add(nome)
                    voce["voce"].discard(nome)
    return presenze
