"""Movimento della macchina da presa, misurato sui fotogrammi invece che indovinato dal modello.

Per ogni inquadratura si estraggono fotogrammi piccoli e fitti (6 al secondo, 320 pixel, in
grigi) e tra un fotogramma e il successivo si stima come si sposta l'immagine: punti
caratteristici (ORB) abbinati e una trasformazione robusta (RANSAC) che segue lo sfondo, cioè
la maggioranza dei punti, e non chi si muove in campo. Sommando gli spostamenti si ottiene:

- orizzontale / verticale: di quanto si è spostata l'immagine (in frazioni della larghezza);
- scala: se l'immagine si ingrandisce (macchina che avanza o zoom) o si rimpicciolisce;
- tremolio: le piccole oscillazioni irregolari tipiche della macchina a mano.

È una misura indicativa: un soggetto che riempie il quadro può far sembrare mossa una macchina
ferma, e carrello laterale e panoramica spostano l'immagine allo stesso modo. Per questo al
modello si passa come dato da confrontare con i fotogrammi, non come verità.
"""

import json
import subprocess
from pathlib import Path

FPS = 6
LARGHEZZA = 320
SPOSTAMENTO = 0.12     # spostamento totale (frazione della larghezza) oltre cui l'immagine si muove davvero
SCALA = 0.06           # ingrandimento o riduzione totale oltre cui la macchina avanza o arretra (o zoom)
TREMOLIO = 0.004       # oscillazione media tra fotogrammi (frazione della larghezza) da macchina a mano
BANDA = 0.12           # alto e basso del quadro esclusi: spesso c'è il timecode impresso, che non si muove
VERSIONE = "2"         # cambia quando cambia il modo di misurare: le misure salvate si rifanno


def _dimensioni(video: Path, ritaglio: str = None) -> tuple:
    if ritaglio:
        w, h = (int(x) for x in ritaglio.split("=")[1].split(":")[:2])
    else:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height", "-of", "csv=p=0", str(video)],
                             capture_output=True, text=True).stdout.strip().split(",")
        w, h = int(out[0]), int(out[1])
    return LARGHEZZA, max(2, round(h * LARGHEZZA / w / 2) * 2)


def _fotogrammi(video: Path, inizio: float, durata: float, ritaglio: str = None):
    import numpy as np
    w, h = _dimensioni(video, ritaglio)
    filtri = (f"{ritaglio}," if ritaglio else "") + f"fps={FPS},scale={w}:{h},format=gray"
    grezzo = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{inizio:.3f}",
                             "-t", f"{durata:.3f}", "-i", str(video), "-an", "-vf", filtri,
                             "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(grezzo) // (w * h)
    return np.frombuffer(grezzo[:n * w * h], dtype=np.uint8).reshape(n, h, w)


def _maschera(forma):
    import numpy as np
    h, w = forma
    m = np.zeros((h, w), dtype=np.uint8)
    m[int(h * BANDA):int(h * (1 - BANDA)), :] = 255
    return m


def _passo(cv2, orb, a, b, maschera):
    """Spostamento (dx, dy in frazioni della larghezza) e scala dal fotogramma a al fotogramma b."""
    import numpy as np
    ka, da = orb.detectAndCompute(a, maschera)
    kb, db = orb.detectAndCompute(b, maschera)
    if da is None or db is None or len(ka) < 12 or len(kb) < 12:
        return None
    coppie = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(coppie) < 12:
        return None
    pa = np.float32([ka[c.queryIdx].pt for c in coppie])
    pb = np.float32([kb[c.trainIdx].pt for c in coppie])
    m, tenuti = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=2.0)
    if m is None or tenuti is None or tenuti.sum() < 10:
        return None
    scala = float(np.hypot(m[0, 0], m[1, 0]))
    return float(m[0, 2]) / a.shape[1], float(m[1, 2]) / a.shape[1], scala


def misura(video: Path, inizio: float, fine: float, ritaglio: str = None) -> dict:
    """La misura del movimento di un'inquadratura (vuota se troppo breve o senza appigli nell'immagine)."""
    import cv2
    import numpy as np
    if fine - inizio < 0.6:
        return {}
    foto = _fotogrammi(video, inizio, fine - inizio, ritaglio)
    if len(foto) < 3:
        return {}
    orb = cv2.ORB_create(nfeatures=600)
    maschera = _maschera(foto[0].shape)
    passi = [p for p in (_passo(cv2, orb, foto[i], foto[i + 1], maschera) for i in range(len(foto) - 1)) if p]
    if len(passi) < max(2, (len(foto) - 1) // 2):
        return {"affidabile": False}  # immagine troppo scura, sfocata o uniforme
    dx = np.array([p[0] for p in passi])
    dy = np.array([p[1] for p in passi])
    ls = np.log([p[2] for p in passi])

    # Si conta solo il movimento costante: il passo tipico (mediana) per il numero di passi, e solo se
    # va nella stessa direzione in buona parte dell'inquadratura. Sommare tutti i passi sommerebbe
    # anche gli errori di ogni stima e i movimenti degli attori.
    def costante(v):
        tipico = float(np.median(v))
        concordi = float(np.mean(np.sign(v) == np.sign(tipico))) if tipico else 0.0
        return tipico * len(v) if concordi >= 0.65 else 0.0
    scala = float(np.exp(costante(ls)))
    # Il tremolio è ciò che resta togliendo il movimento regolare (media mobile su tre passi).
    def irregolare(v):
        if len(v) < 4:
            return 0.0
        liscio = np.convolve(v, np.ones(3) / 3, mode="same")
        return float(np.median(np.abs(v - liscio)[1:-1]))
    return {"affidabile": True, "orizzontale": round(costante(dx), 3), "verticale": round(costante(dy), 3),
            "scala": round(scala, 3), "tremolio": round(max(irregolare(dx), irregolare(dy)), 4)}


def descrivi_misura(m: dict) -> str:
    """La misura in parole: le stesse che si aspettano nel campo movimento_macchina."""
    if not m or not m.get("affidabile"):
        return ""
    parti = []
    # L'immagine che scorre verso destra vuol dire macchina che gira (o si sposta) verso sinistra.
    if abs(m["orizzontale"]) > SPOSTAMENTO:
        parti.append(f"l'immagine scorre in orizzontale di circa {abs(m['orizzontale']) * 100:.0f}% del quadro "
                     f"(macchina verso {'sinistra' if m['orizzontale'] > 0 else 'destra'}: panoramica o carrello laterale)")
    if abs(m["verticale"]) > SPOSTAMENTO:
        parti.append(f"scorre in verticale di circa {abs(m['verticale']) * 100:.0f}% "
                     f"(macchina verso {'l alto' if m['verticale'] > 0 else 'il basso'})".replace("l alto", "l'alto"))
    if m["scala"] > 1 + SCALA:
        parti.append(f"l'immagine si ingrandisce del {(m['scala'] - 1) * 100:.0f}% (la macchina avanza o zoom in avanti)")
    elif m["scala"] < 1 - SCALA:
        parti.append(f"l'immagine si rimpicciolisce del {(1 - m['scala']) * 100:.0f}% (la macchina arretra o zoom indietro)")
    mano = m["tremolio"] > TREMOLIO
    if not parti:
        return "macchina ferma" + (", con le piccole oscillazioni della macchina a mano" if mano else "")
    return "; ".join(parti) + ("; con le oscillazioni irregolari della macchina a mano" if mano else "")


def testo_per_prompt(m: dict) -> str:
    descrizione = descrivi_misura(m)
    if not descrizione:
        return ""
    return ("Movimento della macchina misurato confrontando i fotogrammi (misura automatica sullo sfondo, "
            "indicativa: un soggetto che riempie il quadro può falsarla; usala per il campo movimento_macchina "
            f"insieme a ciò che vedi): {descrizione}.")


def carica(file: Path) -> dict:
    try:
        dati = json.loads(file.read_text())
        return dati["misure"] if dati.get("versione") == VERSIONE else {}
    except (OSError, ValueError, KeyError):
        return {}


def salva(file: Path, misure: dict) -> None:
    file.write_text(json.dumps({"versione": VERSIONE, "misure": misure}, ensure_ascii=False))
