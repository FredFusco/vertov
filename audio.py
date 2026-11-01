"""Analisi del sonoro per il descrittore di inquadrature.

- Trascrizione dei dialoghi con Whisper (mlx-whisper, locale su Apple Silicon).
- Riconoscimento di effetti, ambienti e musica con AST, un classificatore
  addestrato su AudioSet (527 categorie: passi, porte, traffico, pioggia,
  pianoforte, archi, "musica triste", "musica di tensione"...).
- Livello sonoro di ogni traccia per inquadratura.

Tutto gira in locale. Le librerie pesanti vengono caricate solo se servono.
"""

import json
import math
import subprocess
from pathlib import Path

CAMPIONAMENTO = 16000
MODELLO_TAG = "MIT/ast-finetuned-audioset-10-10-0.4593"
MODELLO_WHISPER = "mlx-community/whisper-large-v3-turbo"
FINESTRA_S = 10.0       # AST lavora su finestre di circa 10 secondi
SILENZIO_DB = -55.0

# Etichette AudioSet da non riportare per ciascun tipo di traccia: sul canale
# effetti la voce residua è rumore, sul canale musica contano solo gli strumenti
# e il carattere del brano.
ESCLUSE = {
    "dialoghi": set(),
    "sfx": {"Speech", "Music", "Silence", "Inside, small room", "Inside, large room or hall"},
    "musica": {"Music", "Silence", "Speech"},
    "mix": {"Silence"},
}


def inizio_traccia(percorso: Path):
    """Secondi di timeline a cui inizia il file audio, se il file lo dichiara.

    Legge il time_reference dei WAV broadcast (BWF) o il tag timecode dei
    QuickTime. Restituisce (secondi, None) oppure (None, timecode) o (None, None).
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format_tags=time_reference,timecode:stream=sample_rate:stream_tags=timecode",
         "-of", "json", str(percorso)], capture_output=True, text=True).stdout
    dati = json.loads(out or "{}")
    tag = dati.get("format", {}).get("tags", {})
    if "time_reference" in tag:
        sr = next((int(s["sample_rate"]) for s in dati.get("streams", []) if s.get("sample_rate")), 48000)
        return int(tag["time_reference"]) / sr, None
    tc = tag.get("timecode") or next(
        (s.get("tags", {}).get("timecode") for s in dati.get("streams", []) if s.get("tags", {}).get("timecode")), None)
    return None, tc


def ha_audio(percorso: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(percorso)],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def leggi_segmento(percorso: Path, inizio: float, durata: float):
    import numpy as np
    if inizio < 0:
        durata += inizio
        inizio = 0.0
    if durata <= 0:
        return np.zeros(0, dtype=np.float32)
    grezzo = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{inizio:.3f}", "-t", f"{durata:.3f}",
         "-i", str(percorso), "-vn", "-ac", "1", "-ar", str(CAMPIONAMENTO), "-f", "f32le", "-"],
        capture_output=True).stdout
    return np.frombuffer(grezzo, dtype=np.float32)


def livello_db(x) -> float:
    import numpy as np
    if len(x) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return round(20 * math.log10(max(rms, 1e-6)), 1)


class Traccia:
    """Un file audio e la sua posizione rispetto al file video."""

    def __init__(self, tipo: str, percorso: Path, spostamento: float):
        self.tipo = tipo                  # dialoghi | sfx | musica | mix
        self.percorso = percorso
        self.spostamento = spostamento    # secondi video = secondi traccia + spostamento

    def segmento(self, inizio_video: float, fine_video: float):
        return leggi_segmento(self.percorso, inizio_video - self.spostamento, fine_video - inizio_video)


class Riconoscitore:
    """Classificatore AudioSet (AST) caricato una sola volta."""

    def __init__(self):
        import torch
        from transformers import ASTForAudioClassification, AutoFeatureExtractor
        self.torch = torch
        self.dispositivo = "mps" if torch.backends.mps.is_available() else "cpu"
        print(f"Carico il riconoscitore audio ({self.dispositivo})...")
        self.estrattore = AutoFeatureExtractor.from_pretrained(MODELLO_TAG)
        self.modello = ASTForAudioClassification.from_pretrained(MODELLO_TAG).to(self.dispositivo).eval()
        self.etichette = self.modello.config.id2label

    def probabilita(self, x):
        """Probabilità massime per etichetta sull'intero segmento."""
        import numpy as np
        passo = int(FINESTRA_S * CAMPIONAMENTO)
        if len(x) < CAMPIONAMENTO // 2:
            x = np.pad(x, (0, CAMPIONAMENTO // 2 - len(x)))
        finestre = [x[i:i + passo] for i in range(0, len(x), passo)]
        if len(finestre) > 1 and len(finestre[-1]) < CAMPIONAMENTO:
            finestre.pop()  # coda troppo corta per essere significativa
        ingressi = self.estrattore(finestre, sampling_rate=CAMPIONAMENTO, return_tensors="pt")
        with self.torch.no_grad():
            logit = self.modello(**{k: v.to(self.dispositivo) for k, v in ingressi.items()}).logits
        return self.torch.sigmoid(logit).max(dim=0).values.cpu().numpy()

    def etichetta(self, x, tipo: str, soglia: float = 0.12, massimo: int = 6) -> list:
        p = self.probabilita(x)
        ordine = p.argsort()[::-1]
        risultato = []
        for i in ordine:
            if p[i] < soglia or len(risultato) >= massimo:
                break
            nome = self.etichette[int(i)]
            if nome not in ESCLUSE[tipo]:
                risultato.append((nome, round(float(p[i]), 2)))
        return risultato


ETICHETTE_VOCE = {"Speech", "Male speech, man speaking", "Female speech, woman speaking",
                  "Child speech, kid speaking", "Conversation", "Narration, monologue",
                  "Whispering", "Babbling", "Shout", "Yell", "Screaming", "Crying, sobbing",
                  "Laughter", "Singing"}
# Frasi che Whisper tende a inventare su silenzi, musica e rumori.
FRASI_INVENTATE = ("sottotitoli", "amara.org", "grazie per la visione", "grazie a tutti",
                   "iscriviti al canale", "alla prossima")


def probabilita_voce(riconoscitore: "Riconoscitore", x) -> float:
    p = riconoscitore.probabilita(x)
    return float(max(p[i] for i, n in riconoscitore.etichette.items() if n in ETICHETTE_VOCE))


def verifica_battute(battute: list, traccia: Traccia, riconoscitore: "Riconoscitore") -> tuple:
    """Scarta le battute trascritte dove la traccia non contiene voce.

    Una battuta viene scartata se in quel tratto la traccia è quasi muta, se il
    classificatore non vi sente voce, o se è una tipica frase inventata da
    Whisper e la voce è comunque debole. Restituisce (tenute, scartate).
    """
    tenute, scartate = [], []
    for inizio, fine, testo in battute:
        x = traccia.segmento(inizio, max(fine, inizio + 0.3))
        db = livello_db(x)
        voce = probabilita_voce(riconoscitore, x) if db > SILENZIO_DB else 0.0
        sospetta = any(f in testo.lower() for f in FRASI_INVENTATE)
        if db < -50 or voce < 0.2 or (sospetta and voce < 0.5):
            scartate.append((inizio, fine, testo, db, round(voce, 2)))
        else:
            tenute.append((inizio, fine, testo))
    return tenute, scartate


def analizza_inquadratura(tracce: list, riconoscitore: Riconoscitore, inizio: float, fine: float) -> dict:
    risultato = {}
    for t in tracce:
        x = t.segmento(inizio, fine)
        db = livello_db(x)
        voce = {"livello_db": db}
        if db > SILENZIO_DB:
            voce["tag"] = riconoscitore.etichetta(x, t.tipo)
        else:
            voce["tag"] = []
            voce["silenzio"] = True
        risultato[t.tipo] = voce
    return risultato


NOMI_TRACCE = {"dialoghi": "Traccia dialoghi", "sfx": "Traccia effetti (SFX)",
               "musica": "Traccia musica", "mix": "Mix audio completo"}


def testo_per_prompt(analisi: dict) -> str:
    righe = ["Analisi automatica del sonoro (etichette AudioSet in inglese con probabilità 0-1; "
             "può contenere errori, usala con giudizio):"]
    for tipo, v in analisi.items():
        if v.get("silenzio"):
            righe.append(f"- {NOMI_TRACCE[tipo]}: silenzio")
            continue
        tag = ", ".join(f"{n} ({p})" for n, p in v["tag"]) or "nessun suono riconosciuto"
        righe.append(f"- {NOMI_TRACCE[tipo]}: livello {v['livello_db']} dBFS; {tag}")
    return "\n".join(righe)


def riassunto_per_csv(analisi: dict, tipo: str) -> str:
    v = analisi.get(tipo)
    if not v:
        return ""
    if v.get("silenzio"):
        return "silenzio"
    return ", ".join(n for n, _ in v["tag"])


# --------------------------------------------------------------------------
# Trascrizione
# --------------------------------------------------------------------------

def _tc_srt(s: float) -> str:
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def trascrivi(traccia: Traccia, destinazione: Path, lingua: str, nomi: list = ()) -> Path:
    """Trascrive i dialoghi con Whisper e salva un SRT allineato al video.

    I nomi dei personaggi vengono suggeriti a Whisper, e i tempi di ogni
    battuta sono ricavati dalle singole parole, più precisi di quelli dei
    segmenti (che tendono ad allargarsi sulle pause e sulla musica).
    """
    import mlx_whisper
    print(f"Trascrivo i dialoghi da {traccia.percorso.name} con Whisper (la prima volta scarica il modello)...")
    suggerimento = None
    if nomi:
        suggerimento = "Dialoghi di un film. Personaggi: " + ", ".join(nomi) + "."
    r = mlx_whisper.transcribe(str(traccia.percorso), path_or_hf_repo=MODELLO_WHISPER,
                               language=lingua or None, condition_on_previous_text=False,
                               initial_prompt=suggerimento, word_timestamps=True,
                               hallucination_silence_threshold=2.0)
    blocchi = []
    for i, s in enumerate((s for s in r["segments"] if s["text"].strip()), 1):
        parole = s.get("words") or []
        inizio = parole[0]["start"] if parole else s["start"]
        fine = parole[-1]["end"] if parole else s["end"]
        a, b = inizio + traccia.spostamento, fine + traccia.spostamento
        blocchi.append(f"{i}\n{_tc_srt(max(a, 0))} --> {_tc_srt(max(b, 0))}\n{s['text'].strip()}\n")
    destinazione.write_text("\n".join(blocchi), encoding="utf-8")
    print(f"Trascrizione salvata in {destinazione} ({len(blocchi)} battute)")
    return destinazione
