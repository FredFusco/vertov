"""Riconosce i materiali trascinati in Vertov e decide come usarli.

Per ogni file (o cartella) stabilisce il ruolo: video del film, EDL, stem
dialoghi/effetti/musica, mix, sceneggiatura, trascrizione dei dialoghi o
ordini del giorno. Guarda prima estensione e nome, poi il contenuto: le
pagine di un PDF, le tracce di un file video, e, per un audio dal nome poco
chiaro, cosa si sente (con il classificatore AudioSet).

    python riconosci.py --ascolta FILE.wav   # dialoghi | sfx | musica
"""

import json
import re
import subprocess
import sys
from pathlib import Path

CARTELLA = Path(__file__).resolve().parent

RUOLI = {
    "video": ("Video del film", "le immagini da descrivere, inquadratura per inquadratura"),
    "edl": ("Montaggio (EDL o AAF)", "i tagli esatti e i nomi clip, da cui si ricava il numero di scena"),
    "audio_dialoghi": ("Stem dialoghi", "trascrizione e attribuzione delle battute"),
    "audio_sfx": ("Stem effetti", "riconoscimento di rumori e ambienti"),
    "audio_musica": ("Stem musica", "carattere e strumenti della musica"),
    "audio_mix": ("Mix completo", "il sonoro, quando mancano gli stem separati"),
    "sceneggiatura": ("Sceneggiatura", "personaggi, scene e battute del copione"),
    "dialoghi": ("Trascrizione dialoghi", "le battute pronunciate, con i tempi"),
    "odg": ("Ordini del giorno", "chi era convocato in ogni scena"),
    "personaggi": ("Schede dei personaggi", "l'aspetto dei personaggi, per riconoscerli"),
}
ZONA = {"video": "video", "edl": "video", "audio_dialoghi": "audio", "audio_sfx": "audio",
        "audio_musica": "audio", "audio_mix": "audio", "sceneggiatura": "documenti",
        "dialoghi": "documenti", "odg": "documenti", "personaggi": "documenti"}

EST_VIDEO = {".mov", ".mp4", ".m4v", ".mxf", ".mkv", ".avi", ".webm"}
EST_AUDIO = {".wav", ".bwf", ".aif", ".aiff", ".mp3", ".m4a", ".flac", ".aac", ".ogg"}
EST_TESTO = {".pdf", ".fdx", ".fountain", ".txt"}
EST_NON_SUPPORTATE = {
    ".xml": "XML di montaggio non ancora supportato: esporta un AAF o un EDL (CMX3600).",
    ".fcpxml": "FCPXML non ancora supportato: esporta un AAF o un EDL (CMX3600).",
    ".otio": "OTIO non ancora supportato: esporta un AAF o un EDL (CMX3600).",
    ".docx": "Word non supportato: salva il documento in PDF.",
    ".doc": "Word non supportato: salva il documento in PDF.",
}

# Parole nel nome di un file audio che ne indicano il contenuto.
PAROLE_AUDIO = {
    "audio_dialoghi": {"dx", "dia", "dial", "dialog", "dialogue", "dialoghi", "pd", "presa", "diretta",
                       "vox", "voice", "voci", "voce", "vo", "adr"},
    "audio_sfx": {"sfx", "fx", "efx", "effetti", "effects", "amb", "ambient", "ambienti", "foley",
                  "rumori", "bg"},
    "audio_musica": {"mx", "mus", "music", "musica", "score", "colonna"},
    "audio_mix": {"mix", "pm", "printmaster", "full", "master", "stereo", "lr", "lt", "rt"},
}
INTESTAZIONE_SCENA = re.compile(r"^\s*\d*[A-Z]?\s*[.)]?\s*(INT|EST|EXT|INT/EST|EST/INT|INTERNO|ESTERNO)\b[ ./]",
                                re.M | re.I)


def _ffprobe(percorso: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:format_tags=timecode,time_reference:"
                          "stream=codec_type,width,height,r_frame_rate,channels,sample_rate:stream_tags=timecode",
                          "-of", "json", str(percorso)], capture_output=True, text=True).stdout
    try:
        return json.loads(out or "{}")
    except ValueError:
        return {}


def _durata(sec: float) -> str:
    sec = int(sec or 0)
    return f"{sec // 3600} h {sec % 3600 // 60:02d} min" if sec >= 3600 else f"{sec // 60} min {sec % 60:02d} s"


def _testo(percorso: Path, pagine: int = 4) -> tuple:
    """(inizio del testo, numero di pagine) di un documento."""
    if percorso.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        lettore = PdfReader(str(percorso))
        return "\n".join(p.extract_text() or "" for p in lettore.pages[:pagine]), len(lettore.pages)
    testo = percorso.read_text(encoding="utf-8", errors="ignore")
    return testo[:20000], 0


def _e_odg(testo: str, nome: str) -> bool:
    return bool(re.search(r"\bODG\b|ORDINE DEL GIORNO|CONVOCAZIONE CAST|CALL ?SHEET", testo, re.I)) \
        or bool(re.match(r"(odg|ordine)", nome, re.I))


def ascolta(percorso: Path) -> str:
    """Ruolo di un audio dal nome poco chiaro, da cosa si sente in tre punti del file."""
    sys.path.insert(0, str(CARTELLA))
    import audio
    durata = float(_ffprobe(percorso).get("format", {}).get("duration") or 0)
    riconoscitore = audio.Riconoscitore()
    voce = musica = 0.0
    for frazione in (0.2, 0.5, 0.8):
        x = audio.leggi_segmento(percorso, durata * frazione, 10.0)
        if audio.livello_db(x) < audio.SILENZIO_DB:
            continue
        p = riconoscitore.probabilita(x)
        nomi = riconoscitore.etichette
        voce = max(voce, max(p[i] for i, n in nomi.items() if n in audio.ETICHETTE_VOCE))
        musica = max(musica, max(p[i] for i, n in nomi.items() if n == "Music"))
    if voce > 0.5 and musica > 0.5:
        return "audio_mix"
    if voce > 0.4 and voce >= musica:
        return "audio_dialoghi"
    if musica > 0.4:
        return "audio_musica"
    return "audio_sfx"


def _audio(percorso: Path, info: dict) -> dict:
    parole = {p.lower() for p in re.split(r"[^A-Za-z]+|(?<=[a-z])(?=[A-Z])", percorso.stem) if p}
    ruoli = [r for r, chiavi in PAROLE_AUDIO.items() if parole & chiavi]
    durata = float(info.get("format", {}).get("duration") or 0)
    canali = next((s.get("channels") for s in info.get("streams", []) if s.get("codec_type") == "audio"), 0)
    dettaglio = f"{_durata(durata)}, {canali} can."
    if "time_reference" in info.get("format", {}).get("tags", {}):
        dettaglio += ", con timecode (BWF)"
    if len(ruoli) == 1:
        return {"ruolo": ruoli[0], "dettaglio": dettaglio, "come": "dal nome del file"}
    try:
        ruolo = subprocess.run([sys.executable, str(Path(__file__)), "--ascolta", str(percorso)],
                               capture_output=True, text=True, timeout=300).stdout.strip().splitlines()[-1]
        if ruolo in RUOLI:
            return {"ruolo": ruolo, "dettaglio": dettaglio, "come": "ascoltando il contenuto"}
    except (subprocess.TimeoutExpired, IndexError):
        pass
    return {"ruolo": None, "dettaglio": dettaglio, "problema": "Non sono riuscito a capire che audio è."}


def classifica_file(percorso: Path) -> dict:
    est = percorso.suffix.lower()
    voce = {"percorso": str(percorso), "nome": percorso.name}
    if est in EST_NON_SUPPORTATE:
        return {**voce, "ruolo": None, "problema": EST_NON_SUPPORTATE[est]}
    if est == ".aaf":
        try:
            sys.path.insert(0, str(CARTELLA))
            import aaf
            dati = aaf.leggi(percorso)
        except Exception as e:
            return {**voce, "ruolo": None, "problema": f"Non riesco a leggere l'AAF ({e}). Prova a esportare un EDL."}
        video = [e for e in dati["eventi"] if e["video"] and e["clip"] != "nero"]
        if not video:
            return {**voce, "ruolo": None, "problema": "Nella sequenza dell'AAF non ci sono clip sulle tracce video."}
        seq = dati["sequenza"]
        con_bin = sum(1 for e in video if e["meta"].get("scena"))
        parti = [f"AAF «{seq['nome']}»", f"{len(video)} clip video", f"{len(seq['tracce_audio'])} tracce audio",
                 f"da {seq['inizio']}"]
        if con_bin:
            parti.append(f"scena/ciak dal bin per {con_bin}")
        if dati["marker"]:
            parti.append(f"{len(dati['marker'])} marker")
        return {**voce, "ruolo": "edl", "dettaglio": ", ".join(parti)}
    if est == ".edl":
        eventi = sum(1 for r in percorso.read_text(errors="ignore").splitlines() if re.match(r"^\d{3,6}\s", r))
        return {**voce, "ruolo": "edl", "dettaglio": f"{eventi} eventi"}
    if est in (".srt", ".vtt"):
        battute = percorso.read_text(errors="ignore").count("-->")
        return {**voce, "ruolo": "dialoghi", "dettaglio": f"{battute} battute"}
    if est in EST_VIDEO or est in EST_AUDIO:
        info = _ffprobe(percorso)
        flussi = [s.get("codec_type") for s in info.get("streams", [])]
        if "video" in flussi and est in EST_VIDEO:
            s = next(s for s in info["streams"] if s.get("codec_type") == "video")
            num, den = (s.get("r_frame_rate") or "0/1").split("/")
            fps = float(num) / float(den or 1)
            durata = float(info.get("format", {}).get("duration") or 0)
            tc = info.get("format", {}).get("tags", {}).get("timecode") or \
                next((x.get("tags", {}).get("timecode") for x in info["streams"] if x.get("tags", {}).get("timecode")), None)
            dettaglio = f"{_durata(durata)}, {s.get('width')}×{s.get('height')}, {fps:.3g} fps"
            if tc:
                dettaglio += f", inizia a {tc}"
            if "audio" not in flussi:
                dettaglio += ", senza audio"
            return {**voce, "ruolo": "video", "dettaglio": dettaglio, "durata": durata}
        if "audio" in flussi:
            return {**voce, **_audio(percorso, info)}
        return {**voce, "ruolo": None, "problema": "Il file non contiene né immagini né suono leggibili."}
    if est in EST_TESTO:
        try:
            testo, pagine = _testo(percorso)
        except Exception:
            return {**voce, "ruolo": None, "problema": "Non riesco a leggere il documento."}
        if _e_odg(testo, percorso.name):
            return {**voce, "ruolo": "odg", "dettaglio": "1 ordine del giorno"}
        scene = len(INTESTAZIONE_SCENA.findall(testo))
        righe = [r for r in testo.splitlines() if r.strip() and not r.lstrip().startswith("#")]
        schede = [r for r in righe if re.match(r"^\s*[A-ZÀ-Ü][\w'À-ü .]{1,30}:\s+\S", r)]
        if righe and len(schede) >= max(2, 0.6 * len(righe)) and scene == 0:
            return {**voce, "ruolo": "personaggi", "dettaglio": f"{len(schede)} personaggi"}
        # Una sceneggiatura ha intestazioni di scena (INT./EST.); in quelle con una scena per
        # pagina le prime pagine ne contengono poche.
        if est in (".fdx", ".fountain") or scene >= 3 or (scene >= 1 and pagine >= 20):
            dettaglio = f"{pagine} pagine" if pagine else ""
            return {**voce, "ruolo": "sceneggiatura", "dettaglio": dettaglio, "pagine": pagine}
        return {**voce, "ruolo": None, "problema": "Documento non riconosciuto: non sembra né una sceneggiatura né un ordine del giorno."}
    return {**voce, "ruolo": None, "problema": "Tipo di file non usato da Vertov."}


def classifica(percorsi: list) -> list:
    """Classifica file e cartelle. Una cartella di ordini del giorno resta una voce sola."""
    risultati = []
    for p in map(Path, percorsi):
        if p.is_dir():
            figli = [f for f in sorted(p.iterdir()) if f.is_file() and not f.name.startswith(".")]
            interni = [classifica_file(f) for f in figli]
            odg = [r for r in interni if r.get("ruolo") == "odg"]
            if odg and len(odg) >= len([r for r in interni if r.get("ruolo")]) / 2:
                risultati.append({"percorso": str(p), "nome": p.name, "ruolo": "odg",
                                  "dettaglio": f"cartella con {len(odg)} ordini del giorno"})
                interni = [r for r in interni if r.get("ruolo") != "odg"]
            risultati += interni
        elif p.exists():
            risultati.append(classifica_file(p))
    return risultati


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--ascolta":
        print(ascolta(Path(sys.argv[2])))
    else:
        print(json.dumps(classifica(sys.argv[1:]), ensure_ascii=False, indent=1))
