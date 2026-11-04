#!/usr/bin/env python3
"""Descrittore di inquadrature.

Legge un film, i tagli da un EDL (o li rileva da solo), estrae alcuni
fotogrammi per ogni inquadratura e chiede a un modello di visione locale
(Ollama) di descrivere cosa accade, usando sceneggiatura e trascrizione
dei dialoghi come contesto.

Esempio:
    python3 descrivi.py film.mov --edl film.edl --sceneggiatura script.fdx \
        --dialoghi dialoghi.srt --out risultati

Con tracce audio separate (stem) e trascrizione automatica dei dialoghi:
    python3 descrivi.py film.mov --edl film.edl --audio-dialoghi DX.wav \
        --audio-sfx FX.wav --audio-musica MX.wav --trascrivi
"""

import argparse
import atexit
import base64
import csv
import json
import re
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import audio
import coordinamento
import modello as modulo_modello
import movimento as modulo_movimento
import odg
import progresso
import volti as modulo_volti

MODELLO_PREDEFINITO = modulo_modello.MODELLO
FALLIMENTI_DI_FILA = 3   # inquadrature fallite una dopo l'altra: vuol dire che il modello non risponde più


# --------------------------------------------------------------------------
# Timecode
# --------------------------------------------------------------------------

def tc_a_secondi(tc: str, fps: float) -> float:
    """Converte HH:MM:SS:FF (o HH:MM:SS;FF drop frame) in secondi."""
    drop = ";" in tc
    h, m, s, f = (int(x) for x in re.split(r"[:;.]", tc))
    base = round(fps)
    frame = ((h * 60 + m) * 60 + s) * base + f
    if drop and base in (30, 60):
        perdi = 2 if base == 30 else 4
        minuti = h * 60 + m
        frame -= perdi * (minuti - minuti // 10)
    return frame / fps


def secondi_a_tc(sec: float, fps: float) -> str:
    base = round(fps)
    frame = round(sec * fps)
    f = frame % base
    s = frame // base
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


# --------------------------------------------------------------------------
# Inquadrature: EDL oppure rilevamento automatico
# --------------------------------------------------------------------------

@dataclass
class Inquadratura:
    numero: int
    inizio: float          # secondi nel file video
    fine: float
    tc_in: str             # timecode di registrazione (timeline)
    tc_out: str
    clip: str = ""
    clip_audio: list = field(default_factory=list)
    dialoghi: list = field(default_factory=list)
    copione: list = field(default_factory=list)   # per ogni battuta: (personaggio, testo) o None
    scena: "Scena | None" = None
    montaggio: dict = field(default_factory=dict)  # metadati dall'AAF: bin, effetti, marker...


RIGA_EVENTO = re.compile(
    r"^(\d+)\s+(\S+)\s+(\S+)\s+(C|D|W\d*|K\s*B?|K\s*O?)\s+(\d+\s+)?"
    r"(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+"
    r"(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)\s+(\d\d[:;]\d\d[:;]\d\d[:;]\d\d)"
)


def leggi_edl(percorso: Path, fps: float, tc_inizio_video: float) -> list:
    """Legge un EDL CMX3600 e restituisce le inquadrature video in ordine.

    Le clip delle tracce audio (effetti, musica, presa diretta) vengono
    associate alle inquadrature che attraversano, come contesto per il sonoro.
    """
    tutti, corrente = [], None
    for riga in percorso.read_text(encoding="utf-8", errors="replace").splitlines():
        m = RIGA_EVENTO.match(riga.strip())
        if m:
            traccia = m.group(3).upper()
            corrente = {"tc_in": m.group(8), "tc_out": m.group(9), "clip": m.group(2),
                        "video": "V" in traccia or traccia == "B", "traccia": traccia}
            tutti.append(corrente)
            continue
        nome = re.match(r"^\*\s*FROM CLIP NAME:\s*(.+)$", riga.strip(), re.I)
        if nome and corrente:
            corrente["clip"] = nome.group(1).strip()
    return inquadrature_da_eventi(tutti, fps, tc_inizio_video)


def leggi_montaggio(percorso: Path, fps: float, tc_inizio_video: float) -> list:
    """Inquadrature da un EDL o da un AAF, secondo l'estensione."""
    if percorso.suffix.lower() != ".aaf":
        return leggi_edl(percorso, fps, tc_inizio_video)
    import aaf
    dati = aaf.leggi(percorso)
    seq = dati["sequenza"]
    SEQUENZA.update(seq, marker=len(dati["marker"]))
    print(f"AAF: sequenza «{seq['nome']}», inizia a {seq['inizio']}, {seq['tracce_video']} tracce video, "
          f"tracce audio: {', '.join(seq['tracce_audio'])}; {len(dati['marker'])} marker")
    inquadrature = inquadrature_da_eventi(dati["eventi"], fps, tc_inizio_video)

    def dentro(tc_in: str, tc_out: str = None) -> list:
        a = tc_a_secondi(tc_in, fps) - tc_inizio_video
        b = tc_a_secondi(tc_out, fps) - tc_inizio_video if tc_out else a
        return [q for q in inquadrature if (q.inizio <= a < q.fine) or (tc_out and min(b, q.fine) - max(a, q.inizio) > 0.2)]
    for s in dati["sovrimpressioni"]:
        for q in dentro(s["tc_in"], s["tc_out"]):
            q.montaggio.setdefault("sovrimpressioni", []).append(s["nome"] or ", ".join(s["effetti"]) or "grafica")
    for m in dati["marker"]:
        if m["commento"]:
            for q in dentro(m["tc"]):
                q.montaggio.setdefault("marker", []).append(m["commento"])
    return inquadrature


SEQUENZA = {}  # informazioni sulla sequenza dell'AAF, per il report


def numero_scena_clip(q: "Inquadratura") -> str:
    """Numero di scena dai metadati del bin (AAF) o, in mancanza, dal nome clip."""
    return (q.montaggio.get("scena") or "").strip() or scena_da_clip(q.clip)


def _fuoco(valore) -> str:
    """Distanza di messa a fuoco dei metadati ARRI (in mm) in metri."""
    try:
        return f"messa a fuoco a {float(valore) / 1000:.1f} m".replace(".", ",")
    except (TypeError, ValueError):
        return ""


def testo_montaggio(m: dict) -> str:
    """I metadati di montaggio di un'inquadratura, in italiano, per il prompt e il CSV."""
    righe = []
    ciak = [f"{n} {m[k]}" for k, n in (("scena", "scena"), ("inquadratura", "inquadratura"), ("ciak", "ciak")) if m.get(k)]
    if ciak:
        righe.append("Dal bin: " + ", ".join(ciak) + (" (ciak buono)" if str(m.get("buona", "")).lower() == "true" else ""))
    ripresa = [x for x in (m.get("modello"), f"camera {m['camera'].strip('_ ')}" if m.get("camera", "").strip("_ ") else "",
                           m.get("obiettivo"), f"focale {m['focale']}" if m.get("focale") else "",
                           m.get("diaframma"), f"{m['fps_ripresa']} fps" if m.get("fps_ripresa") else "",
                           _fuoco(m.get("messa_a_fuoco"))) if x]
    if ripresa:
        righe.append("Ripresa: " + ", ".join(ripresa))
    if m.get("effetti"):
        righe.append("Effetti di montaggio: " + ", ".join(m["effetti"]))
    if m.get("entrata"):
        righe.append(f"Entra con: {m['entrata']}")
    if m.get("sovrimpressioni"):
        righe.append("In sovrimpressione: " + ", ".join(dict.fromkeys(m["sovrimpressioni"])))
    if m.get("note"):
        righe.append("Note del bin: " + "; ".join(f"{k}: {v}" for k, v in m["note"].items() if not k.lower().startswith("unc")))
    if m.get("marker"):
        righe.append("Marker del montaggio: " + "; ".join(f"«{x}»" for x in m["marker"]))
    return "\n".join(righe)


def inquadrature_da_eventi(tutti: list, fps: float, tc_inizio_video: float) -> list:
    """Eventi di montaggio {tc_in, tc_out, clip, video} -> inquadrature in ordine."""
    eventi = [e for e in tutti if e["video"]]
    audio_edl = [(tc_a_secondi(e["tc_in"], fps) - tc_inizio_video,
                  tc_a_secondi(e["tc_out"], fps) - tc_inizio_video,
                  f'{e["nome_traccia"]}: {e["clip"]}' if e.get("nome_traccia") else e["clip"])
                 for e in tutti if not e["video"]]

    # Le dissolvenze producono eventi sovrapposti: tengo solo il tratto nuovo.
    eventi.sort(key=lambda e: tc_a_secondi(e["tc_in"], fps))
    inquadrature = []
    for e in eventi:
        inizio = tc_a_secondi(e["tc_in"], fps) - tc_inizio_video
        fine = tc_a_secondi(e["tc_out"], fps) - tc_inizio_video
        if inquadrature and inizio < inquadrature[-1].fine:
            inquadrature[-1].fine = inizio
        if fine - inizio < 1 / fps:
            continue
        inquadrature.append(Inquadratura(0, inizio, fine, e["tc_in"], e["tc_out"], e["clip"],
                                         montaggio=dict(e.get("meta") or {})))
    inquadrature = [q for q in inquadrature if q.fine - q.inizio >= 1 / fps]
    for i, q in enumerate(inquadrature, 1):
        q.numero = i
        nomi = []
        for a, b, nome in audio_edl:
            if min(b, q.fine) - max(a, q.inizio) > 0.5 and nome not in nomi:
                nomi.append(nome)
        q.clip_audio = nomi
    return inquadrature


def rileva_tagli(video: Path, durata: float, fps: float, tc_inizio_video: float,
                 soglia: float) -> list:
    """Rileva i tagli con il filtro scene di ffmpeg (quando manca l'EDL)."""
    print("Rilevo i tagli con ffmpeg (può richiedere qualche minuto)...")
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-an",
           "-vf", f"scale=320:-2,select='gt(scene,{soglia})',showinfo",
           "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
    tagli = [float(t) for t in re.findall(r"pts_time:([\d.]+)", out)]
    confini = [0.0] + [t for t in tagli if 0.2 < t < durata - 0.2] + [durata]
    inquadrature = []
    for i in range(len(confini) - 1):
        a, b = confini[i], confini[i + 1]
        inquadrature.append(Inquadratura(
            i + 1, a, b,
            secondi_a_tc(a + tc_inizio_video, fps),
            secondi_a_tc(b + tc_inizio_video, fps)))
    return inquadrature


# --------------------------------------------------------------------------
# Dialoghi (SRT / VTT)
# --------------------------------------------------------------------------

def _tempo_sub(t: str) -> float:
    t = t.replace(",", ".")
    parti = t.split(":")
    if len(parti) == 2:
        parti.insert(0, "0")
    return int(parti[0]) * 3600 + int(parti[1]) * 60 + float(parti[2])


def leggi_dialoghi(percorso: Path, offset: float) -> list:
    """Restituisce [(inizio, fine, testo)] in secondi rispetto al file video."""
    testo = percorso.read_text(encoding="utf-8-sig", errors="replace")
    battute = []
    for blocco in re.split(r"\n\s*\n", testo.replace("\r", "")):
        righe = [r for r in blocco.strip().split("\n") if r.strip()]
        for i, r in enumerate(righe):
            m = re.match(r"([\d:.,]+)\s*-->\s*([\d:.,]+)", r)
            if m:
                frase = " ".join(re.sub(r"<[^>]+>", "", x) for x in righe[i + 1:]).strip()
                if frase:
                    battute.append((_tempo_sub(m.group(1)) - offset,
                                    _tempo_sub(m.group(2)) - offset, frase))
                break
    return battute


# --------------------------------------------------------------------------
# Sceneggiatura
# --------------------------------------------------------------------------

@dataclass
class Scena:
    indice: int
    intestazione: str
    testo: str
    parole: set
    numero: str = ""
    righe: list = field(default_factory=list)


INTESTAZIONE = re.compile(
    r"^\s*(\d+[A-Z]?[.)]?\s+)?(INT|EXT|EST|INT\./EXT|I/E|INTERNO|ESTERNO)[.\s/]", re.I)


def _parole(testo: str) -> set:
    return {p for p in re.findall(r"\w+", testo.lower()) if len(p) > 3}


def leggi_sceneggiatura(percorso: Path) -> list:
    suff = percorso.suffix.lower()
    if suff == ".fdx":
        radice = ET.parse(percorso).getroot()
        righe = []
        for par in radice.iter("Paragraph"):
            t = "".join(x.text or "" for x in par.iter("Text")).strip()
            if par.get("Type") == "Scene Heading":
                t = "INT. " + t if not INTESTAZIONE.match(t) else t
            righe.append(t)
    elif suff == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            sys.exit("Per leggere un PDF serve pypdf:  python3 -m pip install pypdf")
        righe = []
        for pagina in PdfReader(str(percorso)).pages:
            righe += (pagina.extract_text() or "").splitlines()
    else:
        righe = percorso.read_text(encoding="utf-8", errors="replace").splitlines()

    scene, corrente = [], None
    for r in righe:
        if INTESTAZIONE.match(r):
            corrente = [r.strip(), []]
            scene.append(corrente)
        elif corrente is not None:
            corrente[1].append(r)
    risultato = []
    for i, (h, c) in enumerate(scene):
        # I numeri di scena stanno all'inizio o alla fine dell'intestazione
        # (spesso su entrambi i margini, es. "INT. CASA - GIORNO1 1").
        fine = re.search(r"(\d+[A-Z]?)\s*$", h)
        inizio = re.match(r"^\s*(\d+[A-Z]?)[.)]?\s", h)
        numero = (fine or inizio).group(1) if (fine or inizio) else ""
        pulita = re.sub(r"\s*\d+[A-Z]?(\s+\d+[A-Z]?)?\s*$", "", h).strip() if fine else h
        risultato.append(Scena(i, pulita, "\n".join(c).strip(),
                               _parole(h + " " + " ".join(c)), numero, c))
    return risultato


# --------------------------------------------------------------------------
# Personaggi e battute del copione
# --------------------------------------------------------------------------

def _nome_battuta(riga: str) -> str:
    """Nome del personaggio se la riga è un'attacco di battuta (es. "TERESA (CONT'D)")."""
    r = re.sub(r"\(.*?\)", "", riga).strip()
    if 2 <= len(r) <= 30 and r.upper() == r and re.fullmatch(r"[A-ZÀ-Ý'’ .]+", r) \
            and not INTESTAZIONE.match(r) and not r.endswith(":"):
        return r.strip(" .")
    return ""


def estrai_personaggi(scene: list) -> dict:
    """Descrizione di ogni personaggio alla sua prima apparizione nel copione.

    Riconosce le forme "TERESA (30), capelli corti..." e "PAOLO, un uomo
    alto...", dove il nome in maiuscolo è seguito da una virgola.
    """
    conteggio = {}
    for sc in scene:
        for r in sc.righe:
            nome = _nome_battuta(r)
            if nome:
                conteggio[nome] = conteggio.get(nome, 0) + 1
    testo = " ".join(" ".join(sc.testo.split()) for sc in scene)
    schede = {}
    for nome in conteggio:
        m = re.search(rf"\b{re.escape(nome)}\b\s*(\((?![^)]*CONT)[^)]*\))?\s*,\s*([^.]{{5,220}})", testo)
        if m and sum(c.islower() for c in m.group(2)) > len(m.group(2)) / 2:
            eta = (m.group(1) or "").strip("() ")
            schede[nome.title()] = (f"{eta}, " if eta else "") + m.group(2).strip(" ,")
    return schede


def leggi_personaggi(percorso: Path) -> dict:
    """File di testo con righe "Nome: descrizione"."""
    schede = {}
    for riga in percorso.read_text(encoding="utf-8").splitlines():
        if ":" in riga and not riga.lstrip().startswith("#"):
            nome, desc = riga.split(":", 1)
            schede[nome.strip().title()] = desc.strip()
    return schede


def battute_copione(scena: Scena) -> list:
    """[(personaggio, frase)] dalle battute della scena, frase per frase."""
    battute, chi, blocco = [], None, []

    def chiudi():
        if chi and blocco:
            testo = " ".join(" ".join(blocco).split())
            testo = re.sub(r"^\([^)]*\)\s*", "", testo)  # indicazioni tra parentesi
            for frase in re.split(r"(?<=[.!?…])\s+(?=[A-ZÀ-Ý«\"“])", testo):
                if frase.strip():
                    battute.append((chi, frase.strip()))

    for r in scena.righe:
        nome = _nome_battuta(r)
        if nome:
            chiudi()
            chi, blocco = nome.title(), []
        elif chi is not None and r.strip():
            blocco.append(r)
    chiudi()
    return battute


def _normalizza(t: str) -> str:
    t = t.lower().replace("’", "'")
    return " ".join(re.findall(r"[\w']+", t))


def allinea_copione(inquadrature: list, scene: list) -> None:
    """Abbina ogni battuta trascritta alla frase più simile del copione.

    Si cercano nella scena dell'inquadratura e in quelle adiacenti, anche
    unendo due frasi consecutive (la trascrizione spezza le battute in modo
    diverso dal copione).
    """
    from difflib import SequenceMatcher
    cache = {}
    for q in inquadrature:
        q.copione = []
        if not q.scena:
            q.copione = [None] * len(q.dialoghi)
            continue
        candidati = []
        for i in (q.scena.indice - 1, q.scena.indice, q.scena.indice + 1):
            if 0 <= i < len(scene):
                if i not in cache:
                    b = battute_copione(scene[i])
                    cache[i] = b + [(b[j][0], b[j][1] + " " + b[j + 1][1])
                                    for j in range(len(b) - 1) if b[j][0] == b[j + 1][0]]
                candidati += cache[i]
        for _, _, testo in q.dialoghi:
            n = _normalizza(testo)
            migliore, punteggio = None, 0.0
            for chi, frase in candidati:
                r = SequenceMatcher(None, n, _normalizza(frase)).ratio()
                if r > punteggio:
                    migliore, punteggio = (chi, frase), r
            q.copione.append(migliore if punteggio >= 0.6 else None)


def scena_da_clip(nome: str) -> str:
    """Numero di scena dal nome clip in formato scena/inquadratura/ciak (es. "16/1/6")."""
    m = re.match(r"^\s*(\d+[A-Z]?)\s*/\s*\w+\s*/\s*\w+", nome or "")
    return m.group(1) if m else ""


def abbina_scene(inquadrature: list, scene: list) -> None:
    """Assegna a ogni inquadratura la scena più probabile.

    Prima si usa il numero di scena nel nome clip (scena/inquadratura/ciak);
    altrimenti le inquadrature con dialoghi vengono abbinate per parole in
    comune e quelle mute ereditano la scena dell'inquadratura precedente.
    """
    if not scene:
        return
    per_numero = {s.numero: s for s in scene if s.numero}
    ultima = 0
    for q in inquadrature:
        if q.clip == "nero" or "countdown" in q.clip.lower() or "titolo" == q.clip:
            continue  # nero, coda di partenza e titoli non appartengono a una scena
        numero = numero_scena_clip(q)
        if numero in per_numero:
            ultima = per_numero[numero].indice
            q.scena = per_numero[numero]
            continue
        parole = _parole(" ".join(t for _, _, t in q.dialoghi))
        if len(parole) >= 2:
            punteggi = [(len(parole & s.parole) / len(parole), s.indice) for s in scene]
            # Leggera preferenza per le scene vicine all'ultima riconosciuta.
            migliore = max(punteggi, key=lambda p: p[0] - 0.02 * abs(p[1] - ultima) / 10)
            if migliore[0] >= 0.3:
                ultima = migliore[1]
        q.scena = scene[ultima]


# --------------------------------------------------------------------------
# Fotogrammi e modello
# --------------------------------------------------------------------------

def info_video(video: Path) -> tuple:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate:stream_tags=timecode:format=duration:format_tags=timecode",
         "-of", "json", str(video)], capture_output=True, text=True, check=True).stdout
    dati = json.loads(out)
    flusso = dati["streams"][0]
    num, den = flusso["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    durata = float(dati["format"]["duration"])
    tc = (flusso.get("tags", {}).get("timecode")
          or dati["format"].get("tags", {}).get("timecode"))
    return fps, durata, tc


def rileva_bande_nere(video: Path, durata: float):
    """Filtro crop che toglie le bande nere (letterbox), o None se non ce ne sono."""
    aree = []
    for frazione in (0.25, 0.5, 0.75):
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-ss", f"{durata * frazione:.1f}", "-t", "4", "-i", str(video),
             "-vf", "cropdetect=24:2:0", "-an", "-f", "null", "-"],
            capture_output=True, text=True).stderr
        trovati = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", out)
        if trovati:
            aree.append(tuple(int(x) for x in trovati[-1]))
    if not aree:
        return None
    # Il ritaglio più ampio tra i campioni: meglio una banda residua che tagliare l'immagine.
    w, h, x, y = max(aree, key=lambda a: a[0] * a[1])
    pieno = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(video)],
                           capture_output=True, text=True).stdout.strip().split(",")
    if [str(w), str(h)] == pieno[:2] or h < 100:
        return None
    return f"crop={w}:{h}:{x}:{y}"


def estrai_fotogrammi(video: Path, q: Inquadratura, cartella: Path, larghezza: int,
                      ritaglio: str = None) -> list:
    durata = q.fine - q.inizio
    # Circa un fotogramma ogni 5 secondi, da 3 a 8 (uno solo per gli stacchi brevissimi).
    n = 1 if durata < 1.5 else 3 if durata < 8 else min(8, max(5, round(durata / 5)))
    margine = min(0.15, durata * 0.1)
    if n == 1:
        istanti = [q.inizio + durata / 2]
    else:
        passo = (durata - 2 * margine) / (n - 1)
        istanti = [q.inizio + margine + i * passo for i in range(n)]
    file = []
    for i, t in enumerate(istanti):
        dest = cartella / f"inq{q.numero:05d}_{i}{'_r' if ritaglio else ''}.jpg"
        filtri = (f"{ritaglio}," if ritaglio else "") + f"scale={larghezza}:-2"
        if not dest.exists():
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}",
                 "-i", str(video), "-frames:v", "1", "-vf", filtri,
                 "-q:v", "3", "-y", str(dest)], check=True)
        if dest.exists():
            file.append(dest)
    return file


def estrai_fotogrammi_volti(video: Path, q: Inquadratura, cartella: Path, ritaglio: str = None,
                            al_secondo: float = 1.0, massimo: int = 30) -> list:
    """Fotogrammi fitti (circa uno al secondo) per il riconoscimento dei volti."""
    durata = q.fine - q.inizio
    fps = min(al_secondo, massimo / durata) if durata > 0 else al_secondo
    fps = max(fps, 2 / max(durata, 0.1))  # almeno due fotogrammi anche negli stacchi brevi
    schema = cartella / f"volti{q.numero:05d}_%03d.jpg"
    esistenti = sorted(cartella.glob(f"volti{q.numero:05d}_*.jpg"))
    if not esistenti:
        filtri = (f"{ritaglio}," if ritaglio else "") + f"fps={fps:.4f},scale=1280:-2"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{q.inizio:.3f}",
                        "-t", f"{durata:.3f}", "-i", str(video), "-vf", filtri, "-q:v", "3",
                        "-y", str(schema)], check=True)
        esistenti = sorted(cartella.glob(f"volti{q.numero:05d}_*.jpg"))
    return esistenti


SCHEMA = {
    "type": "object",
    "properties": {
        "piano": {"type": "string"},
        "movimento_macchina": {"type": "string"},
        "personaggi": {"type": "array", "items": {"type": "string"}},
        "azione": {"type": "string"},
        "ambiente": {"type": "string"},
        "luce_atmosfera": {"type": "string"},
        "sonoro": {"type": "string"},
        "dialoghi_riassunto": {"type": "string"},
        "descrizione": {"type": "string"},
    },
    "required": ["piano", "movimento_macchina", "personaggi", "azione",
                 "ambiente", "luce_atmosfera", "sonoro", "dialoghi_riassunto", "descrizione"],
}

ISTRUZIONI = """Sei un assistente al montaggio cinematografico. Ti mostro alcuni fotogrammi \
presi in ordine da UNA SOLA inquadratura di un film (inizio, centro, fine).
Descrivi in italiano, in modo preciso e sobrio, ciò che si vede.

Campi richiesti:
- piano: scala del piano (es. campo lunghissimo, campo lungo, figura intera, piano americano, \
mezza figura, primo piano, primissimo piano, dettaglio, inserto) ed eventuale angolazione \
(dal basso, dall'alto, soggettiva, over the shoulder).
- movimento_macchina: fissa, panoramica, carrello, dolly, zoom, macchina a mano, steadicam... \
deducilo confrontando i fotogrammi: se lo sfondo o il punto di vista cambiano mentre i soggetti \
restano inquadrati, la macchina si muove; un'immagine leggermente instabile indica macchina a \
mano. Se davvero non è deducibile scrivi "fissa o non determinabile".
- personaggi: i personaggi principali in campo (al massimo 5; le comparse riassumile, es. \
"studenti sullo sfondo"). Nomina un personaggio SOLO se è visibile nei fotogrammi, anche di \
spalle, di profilo o in ombra. Le persone riconosciute dai volti sono certamente in campo: \
usa quei nomi. Per le altre persone visibili confronta l'aspetto con la scheda dei personaggi \
e con chi c'è nelle inquadrature vicine: se corrisponde usa il nome, se è ambiguo descrivile \
(es. "ragazza con treccine"). Non aggiungere personaggi solo perché sono nel copione, negli \
ordini del giorno o nelle inquadrature vicine.
- azione: cosa succede nell'inquadratura, dall'inizio alla fine.
- ambiente: luogo, interno/esterno, giorno/notte.
- luce_atmosfera: luce, colori, tono.
- sonoro: cosa si sente (dialoghi, effetti, ambiente, musica e suo carattere), basandoti \
sull'analisi del sonoro e sulle battute se presenti; se non hai informazioni audio scrivi \
"non analizzato".
- dialoghi_riassunto: il contenuto di TUTTE le battute di questa inquadratura, nell'ordine in \
cui vengono dette, con parole tue: chi dice cosa e che cosa si dicono (domande, risposte, \
notizie, richieste, promesse). Non fermarti alla prima battuta e non ridurre uno scambio a un \
gesto: se ci sono più scambi riassumili tutti, anche in tre o quattro frasi se servono (es. \
"Teresa chiede a Paolo se ha già parlato con il padre; Paolo ammette di non averne il coraggio \
e le chiede di non dirlo a nessuno; lei promette di tacere"). RIASSUMI: non citare le battute \
tra virgolette, non scrivere "nel copione" e non riportare tra parentesi il testo del copione. \
Chi dice cosa: usa il personaggio indicato nel copione per ogni battuta ("nel copione TERESA: \
..."); per le battute senza corrispondenza nel copione non attribuirle a un personaggio preciso \
se non è evidente. La trascrizione automatica può contenere errori: una battuta senza senso e \
senza corrispondenza nel copione va tralasciata, non interpretata. Se nessuno parla scrivi \
stringa vuota.
- descrizione: due o tre frasi riassuntive, come in un découpage: cosa succede e, se i \
personaggi parlano, di cosa parlano (riprendendo dialoghi_riassunto).

REGOLA PRINCIPALE: descrivi solo ciò che è visibile nei fotogrammi. La sceneggiatura serve \
a capire chi sono i personaggi e di cosa parlano, NON è una fonte per l'azione: un gesto o \
un evento scritto nel copione ma non visibile nei fotogrammi non va descritto. Un personaggio \
che parla ma non si vede va indicato come "fuori campo".
Per le battute, quando c'è la corrispondente battuta del copione, riportala come battuta \
detta (la trascrizione automatica può contenere errori); se la trascrizione è molto diversa \
dal copione, l'attore potrebbe aver cambiato le parole: riporta la trascrizione."""


def costruisci_prompt(q: Inquadratura, vicini: list, max_scena: int, sonoro: str = "",
                      personaggi: dict = None, convocati: dict = None, volti: str = "",
                      precedenti: list = (), correzioni: list = (), movimento: str = "") -> str:
    parti = [ISTRUZIONI, "",
             f"Inquadratura n. {q.numero}, timecode {q.tc_in} - {q.tc_out} "
             f"(durata {q.fine - q.inizio:.1f} s)."]
    if convocati and (convocati["in_campo"] or convocati["voce"]):
        parti += ["", "Personaggi convocati sul set per questa scena (dagli ordini del giorno; sono "
                  "solo i candidati possibili, NON vuol dire che siano in questa inquadratura: "
                  "elenca tra i personaggi solo chi si vede nei fotogrammi): "
                  + ", ".join(sorted(convocati["in_campo"]))
                  + (f"; solo voce: {', '.join(sorted(convocati['voce']))}" if convocati["voce"] else "")]
    if volti:
        parti += ["", volti]
    if personaggi:
        # Solo i personaggi della scena (convocati o nominati nel copione), per non confondere il modello.
        if convocati and convocati["in_campo"]:
            chi = {n.upper() for n in convocati["in_campo"] | convocati["voce"]}
            pertinenti = {n: d for n, d in personaggi.items() if n.upper() in chi}
        else:
            testo_scena = (q.scena.testo.upper() if q.scena else "")
            pertinenti = {n: d for n, d in personaggi.items() if not q.scena or n.upper() in testo_scena}
        if pertinenti:
            parti += ["", "Scheda dei personaggi (aspetto fisico, per riconoscerli):"]
            parti += [f"- {n}: {d}" for n, d in pertinenti.items()]
    if q.scena:
        testo = q.scena.testo
        if len(testo) > max_scena:
            testo = testo[:max_scena] + " [...]"
        parti += ["", f"Scena della sceneggiatura probabilmente corrispondente: {q.scena.intestazione}",
                  testo]
    if q.dialoghi:
        parti += ["", "Battute pronunciate durante questa inquadratura:"]
        for i, (_, _, t) in enumerate(q.dialoghi):
            c = q.copione[i] if i < len(q.copione) else None
            parti.append(f"- trascrizione: «{t}»" + (f" → nel copione {c[0].upper()}: «{c[1]}»" if c else ""))
    else:
        parti += ["", "Nessuna battuta durante questa inquadratura."]
    if q.montaggio:
        parti += ["", "Dati di montaggio (dall'AAF: affidabili su ripresa ed effetti; note e marker sono appunti "
                  "di lavorazione, servono a capire ma non vanno riportati come azione):", testo_montaggio(q.montaggio)]
        if q.montaggio.get("focale"):
            parti.append("La focale aiuta a stimare la scala del piano e la compressione prospettica.")
        try:
            girato = float(q.montaggio.get("fps_ripresa") or 0)
        except ValueError:
            girato = 0
        if girato > 30:
            parti.append(f"Girato a {girato:g} fps per una timeline a 24/25: in montaggio appare AL RALLENTATORE. "
                         "Scrivilo nella descrizione (es. \"al rallentatore\").")
        elif "cambio di velocità" in q.montaggio.get("effetti", []):
            parti.append("C'è un cambio di velocità: se il movimento appare rallentato o accelerato, dillo.")
        if q.clip == "nero":
            parti.append("Nel montaggio questo tratto è nero (nessuna immagine), salvo titoli in sovrimpressione.")
    if q.clip_audio:
        parti += ["", "Nomi delle clip audio montate durante l'inquadratura (dall'EDL; possono "
                  "indicare effetti, brani musicali o rulli di presa diretta): "
                  + "; ".join(q.clip_audio[:12])]
    if sonoro:
        parti += ["", sonoro]
    if movimento:
        parti += ["", movimento]
    if vicini:
        parti += ["", "Personaggi riconosciuti nelle inquadrature vicine della stessa scena: "
                  + ", ".join(vicini) + ". Ti servono per dare un nome a chi si vede di spalle, di "
                  "profilo o in ombra; non nominarli se non sono visibili in questi fotogrammi."]
    if precedenti:
        parti += ["", "La scena fin qui (inquadrature precedenti, già descritte, in ordine):"]
        parti += [f"- inq. {n}: {d}" for n, d in precedenti]
        parti += ["Questa inquadratura viene subito dopo: descrivila come il seguito del racconto, "
                  "coerente con quanto sopra (stessi nomi, stesso luogo, oggetti e gesti già "
                  "introdotti), senza ripetere ciò che è già stato detto. Il luogo e la luce sono "
                  "già noti: non ricominciare con \"In una scena...\" o descrivendo di nuovo "
                  "l'ambiente, parti direttamente da ciò che succede (cita il luogo solo se cambia). "
                  "Non copiare le frasi: descrivi ciò che si vede in QUESTI fotogrammi e, se "
                  "qualcosa è cambiato, dillo."]
    altre = [(n, t) for n, t in correzioni if n != q.numero]
    propria = next((t for n, t in correzioni if n == q.numero), None)
    if altre:
        parti += ["", "Correzioni del montatore per questa scena. Sono verificate guardando il film "
                  "e valgono più di qualsiasi tua deduzione su chi è chi, chi fa cosa e cosa "
                  "succede: la tua descrizione deve essere coerente con esse (chi tiene un oggetto "
                  "o ha fatto un gesto continua a farlo nelle inquadrature seguenti, se è visibile):"]
        parti += [f"- inq. {n}: «{t}»" for n, t in altre]
    if propria:
        parti += ["", f"Descrizione di QUESTA inquadratura corretta dal montatore: «{propria}». "
                  "È quella giusta: i campi personaggi, azione e dialoghi_riassunto devono "
                  "concordare con essa."]
    return "\n".join(parti)


def chiedi_al_modello(modello: str, prompt: str, immagini: list) -> dict:
    """Descrizione di un'inquadratura (solleva modulo_modello.ErroreModello se non arriva)."""
    return modulo_modello.chiedi(prompt, [base64.b64encode(p.read_bytes()).decode() for p in immagini],
                                 SCHEMA, modello, temperatura=0.2, repeat_penalty=1.15)


# --------------------------------------------------------------------------
# Uscite
# --------------------------------------------------------------------------

CAMPI_CSV = ["numero", "numero_scena", "tc_in", "tc_out", "durata_s", "clip", "scena", "piano",
             "movimento_macchina", "personaggi", "azione", "ambiente",
             "luce_atmosfera", "dialoghi", "battute_copione", "dialoghi_riassunto", "sonoro",
             "sfx_rilevati", "musica_rilevata", "descrizione", "ciak", "ripresa", "effetti",
             "sovrimpressioni", "note_bin", "marker"]


def scrivi_csv(risultati: list, percorso: Path) -> None:
    # Separatore ";" e BOM: si apre correttamente in Excel con impostazioni italiane.
    with percorso.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CAMPI_CSV, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in risultati:
            riga = dict(r)
            riga["personaggi"] = ", ".join(r.get("personaggi", []))
            riga["dialoghi"] = " / ".join(r.get("dialoghi", []))
            riga["battute_copione"] = " / ".join(f"{c[0]}: {c[1]}" for c in r.get("copione", []) if c)
            analisi = r.get("audio", {})
            riga["sfx_rilevati"] = audio.riassunto_per_csv(analisi, "sfx" if "sfx" in analisi else "mix")
            riga["musica_rilevata"] = audio.riassunto_per_csv(analisi, "musica")
            m = r.get("montaggio") or {}
            riga["ciak"] = "/".join(m.get(k, "") for k in ("scena", "inquadratura", "ciak")) if m.get("scena") else ""
            riga["ripresa"] = ", ".join(x for x in (m.get("camera", "").strip("_ "), m.get("modello"), m.get("obiettivo"),
                                                    m.get("focale"), m.get("diaframma"), m.get("file")) if x)
            riga["effetti"] = ", ".join(m.get("effetti", []))
            riga["sovrimpressioni"] = ", ".join(m.get("sovrimpressioni", []))
            riga["note_bin"] = "; ".join(f"{k}: {v}" for k, v in (m.get("note") or {}).items() if not k.lower().startswith("unc"))
            riga["marker"] = " / ".join(m.get("marker", []))
            w.writerow(riga)


def scrivi_marker_resolve(risultati: list, percorso: Path, fps: float) -> None:
    """EDL di marker importabile in DaVinci Resolve
    (Timeline > Importa > Marker della timeline da EDL)."""
    righe = ["TITLE: Descrizioni inquadrature", "FCM: NON-DROP FRAME", ""]
    for r in risultati:
        tc = r["tc_in"]
        tc1 = secondi_a_tc(tc_a_secondi(tc, fps) + 1 / fps, fps)
        scena = f'Sc. {r["numero_scena"]} · ' if r.get("numero_scena") else ""
        nota = re.sub(r"\s+", " ", f'{scena}{r["piano"]} - {r["descrizione"]}').replace("|", "/")
        righe.append(f'{r["numero"]:03d}  001      V     C        {tc} {tc1} {tc} {tc1}  ')
        righe.append(f" |C:ResolveColorBlue |M:{nota} |D:1")
        righe.append("")
    percorso.write_text("\n".join(righe), encoding="utf-8")


def scrivi_marker_avid(risultati: list, percorso: Path) -> None:
    """Marker per Avid Media Composer (finestra Markers › menu › Import Markers).

    Lo stesso formato di testo che usa Murnau, separato da tabulazioni: utente, timecode, traccia,
    colore, commento, durata. Verde, per distinguerli a colpo d'occhio dai marker di Murnau."""
    righe = []
    for r in risultati:
        scena = f'Sc. {r["numero_scena"]} · ' if r.get("numero_scena") else ""
        nota = re.sub(r"\s+", " ", f'{scena}{r["piano"]} - {r["descrizione"]}').strip()
        righe.append(f'Vertov\t{r["tc_in"]}\tV1\tgreen\t{nota}\t1')
    percorso.write_text("\n".join(righe) + "\n", encoding="utf-8")


def prepara_tracce(args, fps: float, tc_inizio_s: float) -> list:
    """Raccoglie le tracce audio e calcola il loro allineamento al video."""
    richieste = [("dialoghi", args.audio_dialoghi), ("sfx", args.audio_sfx),
                 ("musica", args.audio_musica), ("mix", args.audio_mix)]
    tracce = []
    for tipo, percorso in richieste:
        if not percorso:
            continue
        if not percorso.exists():
            sys.exit(f"File audio non trovato: {percorso}")
        secondi, tc = audio.inizio_traccia(percorso)
        if args.tc_inizio_audio:
            inizio = tc_a_secondi(args.tc_inizio_audio, fps)
        elif secondi is not None:
            inizio = secondi
        elif tc:
            inizio = tc_a_secondi(tc, fps)
        else:
            inizio = tc_inizio_s
        tracce.append(audio.Traccia(tipo, percorso, inizio - tc_inizio_s))
    if not tracce and audio.ha_audio(args.video):
        tracce.append(audio.Traccia("mix", args.video, 0.0))
    return tracce


# --------------------------------------------------------------------------
# Programma principale
# --------------------------------------------------------------------------

def chiusura(registro, modello: str, video: Path) -> None:
    """All'uscita, normale o interrotta: il registro si toglie e il modello non resta in memoria.
    ffmpeg e gli altri programmi avviati con subprocess.run vengono chiusi da subprocess stesso."""
    registro.chiudi()
    if registro.dati["origine"] == "terminale":
        coordinamento.notifica(f"{'Analisi finita' if registro.finita else 'Analisi interrotta'}: {video.name}")
    coordinamento.libera_modello(modello)


def argomenti() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Descrive ogni inquadratura di un film con un modello locale.")
    ap.add_argument("video", type=Path)
    ap.add_argument("--edl", type=Path, help="EDL CMX3600 o AAF della timeline (consigliato)")
    ap.add_argument("--sceneggiatura", type=Path, help=".fdx, .fountain, .txt o .pdf")
    ap.add_argument("--dialoghi", type=Path, help="trascrizione con timecode (.srt o .vtt)")
    ap.add_argument("--out", type=Path, default=Path("risultati"))
    ap.add_argument("--modello", default=MODELLO_PREDEFINITO)
    ap.add_argument("--tc-inizio", help="timecode del primo fotogramma del video "
                    "(predefinito: letto dal file, altrimenti 00:00:00:00)")
    ap.add_argument("--offset-dialoghi", help="timecode a cui corrisponde lo 00:00:00 dei "
                    "sottotitoli (predefinito: 00:00:00:00)")
    ap.add_argument("--da", type=int, default=1, help="prima inquadratura da elaborare")
    ap.add_argument("--a", type=int, help="ultima inquadratura da elaborare")
    ap.add_argument("--soglia-tagli", type=float, default=0.3,
                    help="sensibilità del rilevamento tagli senza EDL (0-1)")
    ap.add_argument("--larghezza", type=int, default=1024, help="larghezza dei fotogrammi in pixel")
    ap.add_argument("--fotogrammi", type=Path,
                    help="cartella per i fotogrammi (predefinito: cartella temporanea locale)")
    sonoro = ap.add_argument_group("sonoro")
    sonoro.add_argument("--audio-dialoghi", type=Path, help="stem dei dialoghi (DX)")
    sonoro.add_argument("--audio-sfx", type=Path, help="stem degli effetti e ambienti (FX)")
    sonoro.add_argument("--audio-musica", type=Path, help="stem della musica (MX)")
    sonoro.add_argument("--audio-mix", type=Path, help="mix completo separato "
                        "(predefinito: l'audio del video, se non si danno stem)")
    sonoro.add_argument("--tc-inizio-audio", help="timecode di inizio degli stem, se il file "
                        "non lo dichiara (predefinito: uguale al video)")
    sonoro.add_argument("--trascrivi", action="store_true", help="trascrive i dialoghi con "
                        "Whisper quando manca --dialoghi")
    sonoro.add_argument("--lingua", default="it", help="lingua dei dialoghi per Whisper")
    sonoro.add_argument("--senza-audio", action="store_true", help="non analizza il sonoro")
    ap.add_argument("--personaggi", type=Path, help="file con righe \"Nome: aspetto\" che integra "
                    "o corregge le schede estratte dalla sceneggiatura")
    ap.add_argument("--senza-ritaglio", action="store_true", help="non toglie le bande nere")
    ap.add_argument("--odg", type=Path, nargs="+", help="ordini del giorno in PDF (file o cartelle)")
    ap.add_argument("--senza-volti", action="store_true", help="non usa il riconoscimento dei volti")
    ap.add_argument("--solo-volti", action="store_true", help="riconosce i volti, prepara la pagina "
                    "di controllo e si ferma (per confermare i nomi prima delle descrizioni)")
    ap.add_argument("--rifai", help="rifà solo queste inquadrature (es. \"12,13,14\"), usando i volti "
                    "già riconosciuti e le correzioni in correzioni.json")
    return ap.parse_args()


def carica_inquadrature(args) -> tuple:
    """Video e montaggio: fps, durata, inizio del video in secondi e le inquadrature in ordine."""
    fps, durata, tc_file = info_video(args.video)
    tc_inizio = args.tc_inizio or tc_file or "00:00:00:00"
    tc_inizio_s = tc_a_secondi(tc_inizio, fps)
    print(f"Video: {durata / 60:.1f} min, {fps:.3f} fps, inizia a {tc_inizio}")

    if args.edl:
        inquadrature = leggi_montaggio(args.edl, fps, tc_inizio_s)
        fuori = [q for q in inquadrature if q.inizio < -0.5 or q.fine > durata + 0.5]
        if fuori:
            progresso.avviso(f"{len(fuori)} inquadrature del montaggio cadono fuori dal video e non sono state "
                             f"descritte: controlla che video e montaggio siano della stessa versione "
                             f"(inizio del video: {tc_inizio}).")
        inquadrature = [q for q in inquadrature if q not in fuori]
    else:
        inquadrature = rileva_tagli(args.video, durata, fps, tc_inizio_s, args.soglia_tagli)
    print(f"Inquadrature: {len(inquadrature)}")

    return fps, durata, tc_inizio_s, inquadrature


def prepara_dialoghi(args, tracce: list, inquadrature: list, scene: list, fps: float, tc_inizio_s: float):
    """Trascrizione (se serve), battute verificate sulla traccia dialoghi e assegnate alle inquadrature.

    Restituisce il riconoscitore dei suoni, se è stato caricato per verificare le battute."""
    riconoscitore = None
    if args.trascrivi and not args.dialoghi:
        sorgente = next((t for t in tracce if t.tipo == "dialoghi"), None) or \
            next((t for t in tracce if t.tipo == "mix"), None)
        if sorgente is None:
            sys.exit("Per --trascrivi serve un audio: --audio-dialoghi, --audio-mix o un video con audio.")
        srt = args.out / "dialoghi_trascritti.srt"
        if not srt.exists():
            nomi = sorted(estrai_personaggi(scene)) if scene else []
            progresso.fase("trascrizione")
            audio.trascrivi(sorgente, srt, args.lingua, nomi)
        else:
            print(f"Uso la trascrizione già fatta: {srt}")
        args.dialoghi = srt
        args.offset_dialoghi = None  # l'SRT trascritto è già allineato al video

    if args.dialoghi:
        # Lo 00:00:00 dei sottotitoli corrisponde a --offset-dialoghi sulla timeline.
        offset = tc_inizio_s - tc_a_secondi(args.offset_dialoghi, fps) if args.offset_dialoghi else 0.0
        battute = leggi_dialoghi(args.dialoghi, offset)
        voce = next((t for t in tracce if t.tipo == "dialoghi"), None) or \
            next((t for t in tracce if t.tipo == "mix"), None)
        if voce:
            riconoscitore = riconoscitore or audio.Riconoscitore()
            battute, scartate = audio.verifica_battute(battute, voce, riconoscitore)
            if scartate:
                (args.out / "battute_scartate.txt").write_text(
                    "# Battute della trascrizione scartate perché nella traccia non c'è voce\n"
                    "# (inizio s, fine s, livello dB, probabilità voce, testo)\n" + "\n".join(
                        f"{a:.2f}\t{b:.2f}\t{db}\t{v}\t{t}" for a, b, t, db, v in scartate) + "\n",
                    encoding="utf-8")
                print(f"Battute scartate (senza voce nella traccia {voce.tipo}): {len(scartate)} "
                      f"(vedi {args.out / 'battute_scartate.txt'})")
        for q in inquadrature:
            q.dialoghi = [b for b in battute
                          if min(b[1], q.fine) - max(b[0], q.inizio) > 0.25 * min(b[1] - b[0], q.fine - q.inizio)]
        print(f"Battute: {len(battute)}")

    return riconoscitore


def prepara_copione(args, inquadrature: list, scene: list) -> tuple:
    """Scena e battute del copione di ogni inquadratura, schede dei personaggi, presenze dagli ODG."""
    if scene:
        print(f"Scene in sceneggiatura: {len(scene)}")
        abbina_scene(inquadrature, scene)
        allinea_copione(inquadrature, scene)
        abbinate = sum(1 for q in inquadrature for c in q.copione if c)
        print(f"Battute ritrovate nel copione: {abbinate}/{sum(len(q.dialoghi) for q in inquadrature)}")

    personaggi = estrai_personaggi(scene) if scene else {}
    if args.personaggi:
        personaggi.update(leggi_personaggi(args.personaggi))
    if personaggi:
        (args.out / "personaggi.txt").write_text(
            "# Schede estratte dalla sceneggiatura. Copia e correggi in un file da passare con --personaggi.\n"
            + "\n".join(f"{n}: {d}" for n, d in personaggi.items()) + "\n", encoding="utf-8")
        print(f"Schede personaggi: {len(personaggi)} (vedi {args.out / 'personaggi.txt'})")
    presenze = {}
    if args.odg:
        noti = sorted({_nome_battuta(r).title() for sc in scene for r in sc.righe if _nome_battuta(r)}) \
            if scene else []
        presenze = odg.leggi_presenze(args.odg, noti)
        print(f"Ordini del giorno: presenze per {len(presenze)} scene")
    return personaggi, presenze


def volti_della_selezione(args, selezione: list, file_jsonl: Path, rifai: set, cartella_foto: Path,
                          ritaglio, presenze: dict, personaggi: dict) -> dict:
    """{numero inquadratura: volti riconosciuti}: nuovi, o quelli già salvati se si rifanno inquadrature."""
    volti_per_inq = {}
    if rifai:
        # Si rifanno inquadrature già descritte: i volti riconosciuti sono già nei risultati.
        precedenti_volti = {}
        for riga in file_jsonl.read_text(encoding="utf-8").splitlines():
            if riga.strip():
                r = json.loads(riga)
                precedenti_volti[r["numero"]] = r.get("volti", [])
        volti_per_inq = {n: v for n, v in precedenti_volti.items() if n in rifai}
        # I nomi confermati dopo l'analisi (finestra dei volti) sostituiscono quelli di allora.
        confermati = modulo_volti.nomi_confermati(args.out / "volti" / "nomi.txt")
        for elenco in volti_per_inq.values():
            for v in elenco:
                if v.get("gruppo") in confermati:
                    v["nome"] = confermati[v["gruppo"]] or f"persona non identificata ({v['gruppo']})"
    elif not args.senza_volti:
        progresso.fase("fotogrammi_volti")
        foto_volti = {}
        for i, q in enumerate(selezione, 1):
            foto_volti[q.numero] = estrai_fotogrammi_volti(args.video, q, cartella_foto, ritaglio)
            progresso.avanzamento("fotogrammi_volti", i, len(selezione))
        volti_per_inq = modulo_volti.analizza(selezione, foto_volti, args.out / "volti", presenze,
                                              personaggi, args.modello)
    return volti_per_inq


def descrivi_selezione(args, inquadrature: list, selezione: list, fatte: dict, foto_tutte: dict,
                       volti_per_inq: dict, personaggi: dict, presenze: dict, correzioni: dict,
                       tracce: list, riconoscitore, file_jsonl: Path, ritaglio: str = None) -> int:
    """Descrive le inquadrature non ancora fatte, scrivendole subito nel file. Restituisce quante sono nuove."""
    # Nomi noti dei personaggi, per riconoscerli nelle risposte delle inquadrature vicine.
    noti = set(personaggi) | {v["nome"] for vv in volti_per_inq.values() for v in vv
                              if not v["nome"].startswith(("persona non", "comparsa"))}

    def nomi_in(risultato: dict) -> set:
        testo = " ".join(risultato.get("personaggi", []))
        return {n for n in noti if re.search(rf"\b{re.escape(n)}\b", testo)}

    def chiave_scena(q: Inquadratura) -> str:
        return q.scena.intestazione if q.scena else numero_scena_clip(q)

    # Le inquadrature di ogni scena, in ordine: su un film di migliaia di inquadrature evita di
    # scorrerle tutte a ogni descrizione.
    per_scena = {}
    for a in inquadrature:
        if chiave_scena(a):
            per_scena.setdefault(chiave_scena(a), []).append(a)
    per_numero = {a.numero: a for a in inquadrature}

    def precedenti_di(q: Inquadratura) -> list:
        """Le ultime inquadrature già descritte della stessa scena, per un racconto coerente."""
        prima = [a for a in per_scena.get(chiave_scena(q), []) if a.numero < q.numero and a.numero in fatte]
        return [(a.numero, fatte[a.numero]["descrizione"]) for a in prima[-3:]]

    def correzioni_di(q: Inquadratura) -> list:
        if not chiave_scena(q):
            return [(q.numero, correzioni[q.numero])] if q.numero in correzioni else []
        return [(a.numero, correzioni[a.numero]) for a in per_scena.get(chiave_scena(q), [])
                if a.numero in correzioni]

    def vicini_di(q: Inquadratura) -> list:
        """Personaggi delle inquadrature vicine (±2) della stessa scena."""
        nomi = set()
        for n in range(q.numero - 2, q.numero + 3):
            altra = per_numero.get(n)
            if altra is None or altra is q or altra.scena is not q.scena:
                continue
            nomi |= {v["nome"] for v in volti_per_inq.get(altra.numero, [])} & noti
            if altra.numero in fatte:
                nomi |= nomi_in(fatte[altra.numero])
        return sorted(nomi)

    inizio_lavoro = time.time()
    nuove = 0
    senza_fotogrammi, fallite, di_fila = [], [], 0
    # Movimento della macchina misurato sui fotogrammi: salvato, così una ripresa non lo rifà.
    file_movimento = args.out / "movimento.json"
    misure = modulo_movimento.carica(file_movimento)
    fatte_qui = sum(1 for x in selezione if x.numero in fatte)
    progresso.fase("descrizioni")
    with file_jsonl.open("a", encoding="utf-8") as out:
        for q in selezione:
            if q.numero in fatte:
                continue
            foto = foto_tutte.get(q.numero)
            if not foto:
                print(f"[{q.numero}] nessun fotogramma estratto, salto")
                senza_fotogrammi.append(q.numero)
                continue
            analisi = audio.analizza_inquadratura(tracce, riconoscitore, q.inizio, q.fine) if tracce else {}
            chiave_mov = f"{q.numero}:{q.tc_in}"
            if chiave_mov not in misure:
                try:
                    misure[chiave_mov] = modulo_movimento.misura(args.video, q.inizio, q.fine, ritaglio)
                except Exception as e:  # la misura è un aiuto: se non riesce si descrive senza
                    print(f"[{q.numero}] movimento non misurato: {e}")
                    misure[chiave_mov] = {}
                modulo_movimento.salva(file_movimento, misure)
            mov = misure[chiave_mov]
            prompt = costruisci_prompt(q, vicini_di(q), max_scena=3000,
                                       sonoro=audio.testo_per_prompt(analisi) if analisi else "",
                                       personaggi=personaggi,
                                       convocati=presenze.get(q.scena.numero if q.scena else ""),
                                       volti=modulo_volti.testo_per_prompt(volti_per_inq[q.numero])
                                       if q.numero in volti_per_inq else "",
                                       precedenti=precedenti_di(q), correzioni=correzioni_di(q),
                                       movimento=modulo_movimento.testo_per_prompt(mov))
            t0 = time.time()
            try:
                desc = chiedi_al_modello(args.modello, prompt, foto)
            except modulo_modello.ErroreModello as e:
                # Una risposta mancata non ferma il film: l'inquadratura resta da descrivere e si
                # rifà alla prossima ripresa. Se però falliscono più inquadrature di fila, il modello
                # non risponde più (Ollama chiuso o bloccato) ed è inutile continuare.
                fallite.append(q.numero)
                di_fila += 1
                print(f"[{q.numero}] {e}")
                if di_fila >= FALLIMENTI_DI_FILA:
                    raise SystemExit(f"Il modello non risponde: {FALLIMENTI_DI_FILA} inquadrature di fila senza "
                                     f"descrizione. Il lavoro fatto è salvato: rilanciando riprende da qui.")
                continue
            di_fila = 0
            risultato = {
                "numero": q.numero, "tc_in": q.tc_in, "tc_out": q.tc_out,
                "durata_s": round(q.fine - q.inizio, 2), "clip": q.clip,
                "numero_scena": q.scena.numero if q.scena else numero_scena_clip(q),
                "montaggio": q.montaggio,
                "scena": q.scena.intestazione if q.scena else "",
                "dialoghi": [t for _, _, t in q.dialoghi],
                "copione": q.copione,
                "volti": volti_per_inq.get(q.numero, []),
                **desc,
                "audio": analisi,
                "movimento_misurato": mov,
            }
            if q.numero in correzioni:
                # La descrizione del montatore comprende già i dialoghi: il riassunto del
                # modello potrebbe contraddirla.
                risultato["descrizione"] = correzioni[q.numero]
                risultato["dialoghi_riassunto"] = ""
                risultato["corretta"] = True
            out.write(json.dumps(risultato, ensure_ascii=False) + "\n")
            out.flush()
            fatte[q.numero] = risultato
            nuove += 1
            fatte_qui += 1
            progresso.avanzamento("descrizioni", fatte_qui, len(selezione))
            media = (time.time() - inizio_lavoro) / nuove
            restanti = len(selezione) - fatte_qui
            print(f"[{q.numero}/{len(inquadrature)}] {q.tc_in}  {time.time() - t0:.0f}s  "
                  f"(mancano ~{restanti * media / 60:.0f} min)  {desc['descrizione'][:90]}")

    if fallite:
        progresso.avviso(f"{len(fallite)} inquadrature senza descrizione perché il modello non ha risposto "
                         f"correttamente: {', '.join(map(str, fallite[:20]))}{'…' if len(fallite) > 20 else ''}. "
                         f"Rilancia l'analisi per descriverle.")
    if senza_fotogrammi:
        progresso.avviso(f"{len(senza_fotogrammi)} inquadrature non descritte perché non se ne sono potuti estrarre "
                         f"i fotogrammi: {', '.join(map(str, senza_fotogrammi[:20]))}"
                         f"{'…' if len(senza_fotogrammi) > 20 else ''}.")
    return nuove


def scrivi_risultati(args, fatte: dict, fps: float, rifai: set, nuove: int, avvio: float) -> None:
    """File dei risultati, registro del lavoro e report."""
    file_jsonl = args.out / "inquadrature.jsonl"
    risultati = [fatte[n] for n in sorted(fatte)]
    # Le righe rifatte sono state aggiunte in coda: si riscrive il file con una riga per inquadratura.
    file_jsonl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in risultati),
                          encoding="utf-8")
    scrivi_csv(risultati, args.out / "inquadrature.csv")
    scrivi_marker_resolve(risultati, args.out / "marker_resolve.edl", fps)
    scrivi_marker_avid(risultati, args.out / "marker_avid.txt")

    # Registro del lavoro (per il report): fonte dei tagli, sequenza, tempi di ogni elaborazione.
    file_lavoro = args.out / "lavoro.json"
    try:
        lavoro = json.loads(file_lavoro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lavoro = {}
    if not rifai:
        lavoro["fonte_tagli"] = (f"AAF {args.edl.name}" if args.edl and args.edl.suffix.lower() == ".aaf"
                                 else f"EDL {args.edl.name}" if args.edl else "rilevamento automatico dei tagli")
        if SEQUENZA:
            lavoro["sequenza"] = SEQUENZA
    lavoro["modello"] = args.modello
    lavoro.setdefault("elaborazioni", []).append({
        "quando": time.strftime("%d/%m/%Y %H:%M"), "tipo": "aggiornamento di inquadrature" if rifai else "analisi",
        "inquadrature": nuove, "durata_s": round(time.time() - avvio)})
    lavoro["durata_totale_s"] = sum(x["durata_s"] for x in lavoro["elaborazioni"])
    file_lavoro.write_text(json.dumps(lavoro, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import report
        report.genera(args.out, args.modello)
    except Exception as e:
        print(f"Report non generato: {e}")
        progresso.avviso(f"Il report non è stato creato: {e}")
    progresso.fase("fine")
    print(f"\nFatto. File di lavoro in {args.out}/")


def main() -> None:
    args = argomenti()
    avvio = time.time()

    # Ogni analisi si registra, così l'app la vede anche se è partita da Terminale; all'uscita
    # (anche se interrotta con Ferma, ⌃C o dalla chiusura dell'app) si toglie e libera il modello.
    registro = progresso.REGISTRO = coordinamento.Registro(str(args.video), str(args.out))
    atexit.register(chiusura, registro, args.modello, args.video)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))
    coordinamento.sorveglia_app()
    if registro.dati["origine"] == "terminale":
        coordinamento.notifica(f"Analisi avviata da Terminale: {args.video.name}. La vedi e la puoi fermare dall'app.")
    rifai = {int(n) for n in re.split(r"[,\s]+", args.rifai) if n} if args.rifai else set()

    fps, durata, tc_inizio_s, inquadrature = carica_inquadrature(args)
    tracce = []
    if not args.senza_audio:
        tracce = prepara_tracce(args, fps, tc_inizio_s)
        for t in tracce:
            print(f"Audio {t.tipo}: {t.percorso.name} (spostamento {t.spostamento:+.2f} s)")

    args.out.mkdir(parents=True, exist_ok=True)
    scene = leggi_sceneggiatura(args.sceneggiatura) if args.sceneggiatura else []  # letta una volta sola
    riconoscitore = prepara_dialoghi(args, tracce, inquadrature, scene, fps, tc_inizio_s)
    personaggi, presenze = prepara_copione(args, inquadrature, scene)
    ritaglio = None if args.senza_ritaglio else rileva_bande_nere(args.video, durata)
    if ritaglio:
        print(f"Bande nere rilevate: ritaglio {ritaglio}")
    if tracce and riconoscitore is None:
        riconoscitore = audio.Riconoscitore()
    cartella_foto = args.fotogrammi or Path(tempfile.gettempdir()) / f"descrittore_{args.video.stem}"
    cartella_foto.mkdir(parents=True, exist_ok=True)
    file_jsonl = args.out / "inquadrature.jsonl"

    # Ripresa: le inquadrature già descritte non vengono rifatte.
    fatte = {}
    if file_jsonl.exists():
        for riga in file_jsonl.read_text(encoding="utf-8").splitlines():
            if riga.strip():
                r = json.loads(riga)
                fatte[r["numero"]] = r

    selezione = [q for q in inquadrature if q.numero >= args.da and (args.a is None or q.numero <= args.a)]
    if rifai:
        selezione = [q for q in selezione if q.numero in rifai]
        # Le descrizioni di prima si tengono da parte: se il rifacimento di un'inquadratura non
        # riesce, resta quella vecchia invece di sparire dai risultati.
        di_prima = {n: fatte.pop(n) for n in rifai if n in fatte}

    # Correzioni del montatore: {numero inquadratura: descrizione giusta}.
    file_correzioni = args.out / "correzioni.json"
    try:
        correzioni = {int(k): v for k, v in json.loads(file_correzioni.read_text(encoding="utf-8")).items() if v.strip()}
    except (OSError, ValueError):
        correzioni = {}

    print("Estraggo i fotogrammi...")
    progresso.fase("fotogrammi")
    foto_tutte = {}
    for i, q in enumerate(selezione, 1):
        foto_tutte[q.numero] = estrai_fotogrammi(args.video, q, cartella_foto, args.larghezza, ritaglio)
        progresso.avanzamento("fotogrammi", i, len(selezione))
    volti_per_inq = volti_della_selezione(args, selezione, file_jsonl, rifai, cartella_foto, ritaglio,
                                          presenze, personaggi)
    if args.solo_volti:
        print("Fermo qui (--solo-volti). Correggi volti/nomi.txt e rilancia senza --solo-volti.")
        progresso.fase("fine")
        return

    nuove = descrivi_selezione(args, inquadrature, selezione, fatte, foto_tutte, volti_per_inq, personaggi,
                               presenze, correzioni, tracce, riconoscitore, file_jsonl, ritaglio)
    if rifai:
        for n, r in di_prima.items():
            fatte.setdefault(n, r)
    scrivi_risultati(args, fatte, fps, rifai, nuove, avvio)


if __name__ == "__main__":
    main()
