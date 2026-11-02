#!/usr/bin/env python3
"""Vertov: l'app del descrittore di inquadrature.

Apre una finestra propria (pywebview, WebKit di macOS) con l'interfaccia, servita
da un piccolo server interno raggiungibile solo da questo computer. L'interfaccia
fa scegliere i file, lancia il riconoscimento dei volti e le descrizioni
(descrivi.py), mostra l'avanzamento e i risultati e permette di correggerli.
Ogni lavoro è una sessione salvata, che si può riaprire in seguito.

    ~/.venvs/descrittore/bin/python app.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

import aggiornamenti
import coordinamento
import pulizia
import riconosci
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CARTELLA = Path(__file__).resolve().parent
PORTA = 8765
FILE_ULTIMO = Path.home() / ".cache" / "descrittore" / "ultimo_progetto.json"  # versioni precedenti
DATI = Path.home() / "Library" / "Application Support" / "Vertov"
SESSIONI = DATI / "sessioni"
FILE_CORRENTE = DATI / "sessione_corrente.json"
MODELLO = "qwen3-vl:30b-a3b-instruct"
# I file con cui Vertov lavora (descrizioni grezze, volti, sinossi, trascrizione...) stanno dentro la
# cartella di Vertov, una cartella per sessione (mai pubblicata: vedi .gitignore). Nella cartella di
# destinazione scelta dall'utente finisce solo ciò che esporta.
FILE_DI_LAVORO = CARTELLA / "file di lavoro"

CAMPI_FILE = ["video", "edl", "sceneggiatura", "dialoghi", "odg", "audio_dialoghi",
              "audio_sfx", "audio_musica", "audio_mix", "out"]
OPZIONI = {"video": "", "edl": "--edl", "sceneggiatura": "--sceneggiatura", "dialoghi": "--dialoghi",
           "audio_dialoghi": "--audio-dialoghi", "audio_sfx": "--audio-sfx",
           "audio_musica": "--audio-musica", "audio_mix": "--audio-mix"}


# --------------------------------------------------------------------------
# Sessioni
# --------------------------------------------------------------------------
# Una sessione è un progetto di lavoro: i file scelti, la cartella dei risultati
# e un nome. Ogni modifica viene salvata subito nella sessione corrente.

def _file_sessione(id_: str) -> Path:
    return SESSIONI / f"{id_}.json"


def _scrivi_sessione(p: dict) -> None:
    SESSIONI.mkdir(parents=True, exist_ok=True)
    p["modificata"] = time.strftime("%Y-%m-%d %H:%M")
    _file_sessione(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=1))
    FILE_CORRENTE.write_text(json.dumps({"id": p["id"]}))


def nuova_sessione(nome: str = "", dati: dict = None) -> dict:
    id_ = time.strftime("%Y%m%d-%H%M%S")
    while _file_sessione(id_).exists():
        id_ += "b"
    p = {**(dati or {}), "id": id_, "nome": nome.strip() or "Nuova sessione",
         "creata": time.strftime("%Y-%m-%d %H:%M")}
    _scrivi_sessione(p)
    return p


def carica_progetto() -> dict:
    try:
        id_ = json.loads(FILE_CORRENTE.read_text())["id"]
        return json.loads(_file_sessione(id_).read_text())
    except (OSError, ValueError, KeyError):
        pass
    # Primo avvio: l'ultimo progetto di Kinoglaz diventa la prima sessione.
    try:
        vecchio = json.loads(FILE_ULTIMO.read_text())
    except (OSError, ValueError):
        vecchio = {}
    nome = Path(vecchio["video"]).stem if vecchio.get("video") else ""
    return nuova_sessione(nome, vecchio)


def salva_progetto(p: dict) -> None:
    _scrivi_sessione(p)
    if p.get("cartella_lavoro"):
        (cartella_lavoro(p) / "progetto.json").write_text(json.dumps(p, ensure_ascii=False, indent=1))


def nome_per_file(testo: str) -> str:
    """Il nome della sessione reso adatto a un nome di file o cartella."""
    return re.sub(r'[/:\\\x00-\x1f]', "-", testo or "").strip(" .") or "Sessione"


def _lavoro(p: dict) -> Path:
    """La cartella di lavoro della sessione, senza crearla (una che non esiste se non è ancora assegnata)."""
    return Path(p["cartella_lavoro"]) if p.get("cartella_lavoro") else Path("/nonesiste")


def cartella_lavoro(p: dict) -> Path:
    """La cartella di lavoro della sessione: la assegna (dal nome della sessione) e la crea se serve."""
    if not p.get("cartella_lavoro"):
        usate = set()
        for f in SESSIONI.glob("*.json"):
            try:
                usate.add(json.loads(f.read_text()).get("cartella_lavoro"))
            except (OSError, ValueError):
                pass
        base = nome_per_file(p.get("nome"))
        c, n = FILE_DI_LAVORO / base, 2
        while c.exists() or str(c) in usate:
            c, n = FILE_DI_LAVORO / f"{base} {n}", n + 1
        p["cartella_lavoro"] = str(c)
        if _file_sessione(p["id"]).exists():  # senza cambiare la sessione aperta
            _file_sessione(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=1))
    c = Path(p["cartella_lavoro"])
    c.mkdir(parents=True, exist_ok=True)
    return c


def rinomina_cartella_lavoro(p: dict) -> None:
    """Dopo aver cambiato nome alla sessione, anche la sua cartella di lavoro prende il nuovo nome."""
    vecchia = _lavoro(p)
    nuova = FILE_DI_LAVORO / nome_per_file(p.get("nome"))
    if not vecchia.is_dir() or nuova == vecchia or nuova.exists() or LAVORO.attivo():
        return
    vecchia.rename(nuova)
    p["cartella_lavoro"] = str(nuova)


# File che Vertov creava nella cartella dei risultati, prima delle cartelle di lavoro.
FILE_DI_VERTOV = ["inquadrature.jsonl", "inquadrature.csv", "marker_resolve.edl", "volti", "personaggi.txt",
                  "personaggi_utente.txt", "progetto.json", "correzioni.json", "battute_scartate.txt",
                  "dialoghi_trascritti.srt", "inquadrature_prima_di_vertov.jsonl", "report_vertov.html",
                  "sinossi.json", "lavoro.json"]


def materiali_di(p: dict) -> set:
    return {str(Path(x).resolve()) for x in [p.get(r) for r in riconosci.RUOLI if r != "odg"] + lista_odg(p)
            + p.get("schede", []) + [x["percorso"] for x in p.get("scartati", [])] if x}


def riporta_cartella_lavoro(p: dict) -> bool:
    """Riporta in FILE_DI_LAVORO la cartella di lavoro della sessione se sta altrove: creata da una
    versione precedente (in My Drive/CLAUDE) o rimasta con il percorso di prima dopo che la cartella
    di Vertov ha cambiato nome. Restituisce True se la sessione è cambiata."""
    if not p.get("cartella_lavoro"):
        return False
    vecchia = Path(p["cartella_lavoro"])
    nuova = FILE_DI_LAVORO / vecchia.name
    if vecchia == nuova:
        return False
    if vecchia.is_dir() and not nuova.exists():
        FILE_DI_LAVORO.mkdir(parents=True, exist_ok=True)
        __import__("shutil").move(str(vecchia), str(nuova))
        if vecchia.parent.is_dir() and not any(x for x in vecchia.parent.iterdir() if x.name != ".DS_Store"):
            __import__("shutil").rmtree(vecchia.parent)  # la vecchia cartella generale, rimasta vuota
    elif not nuova.is_dir():
        return False  # né qui né là: si lascia com'è, la cartella verrà ricreata quando serve
    p["cartella_lavoro"] = str(nuova)
    _file_sessione(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=1))
    return True


def trasloca(p: dict) -> list:
    """Sposta nella cartella di lavoro i file che le versioni precedenti di Vertov lasciavano nella
    cartella dei risultati. Spostati, non cancellati; mai un file scelto come materiale."""
    spostati = []
    if riporta_cartella_lavoro(p):
        spostati.append(f"cartella di lavoro in {FILE_DI_LAVORO}")
    vecchio = SESSIONI / f"{p['id']}_personaggi.txt"
    if vecchio.exists():
        vecchio.replace(cartella_lavoro(p) / "personaggi_utente.txt")
    if not p.get("out") or not Path(p["out"]).is_dir():
        return spostati
    out, materiali = Path(p["out"]), materiali_di(p)
    for nome in FILE_DI_VERTOV:
        f = out / nome
        if f.exists() and str(f.resolve()) not in materiali:
            if nome == "marker_resolve.edl" and "Descrizioni inquadrature" not in f.read_text(errors="ignore")[:200]:
                continue  # stesso nome, ma è il file di Murnau (che usa la stessa cartella)
            dest = cartella_lavoro(p) / nome
            if dest.exists():
                continue  # c'è già una versione più recente nella cartella di lavoro
            f.rename(dest) if f.stat().st_dev == dest.parent.stat().st_dev else __import__("shutil").move(f, dest)
            spostati.append(nome)
    return spostati


def trasloca_tutte() -> None:
    for f in SESSIONI.glob("*.json"):
        try:
            p = json.loads(f.read_text())
            spostati = trasloca(p)
            if spostati:
                print(f"Sessione {p.get('nome')}: spostati nella cartella di lavoro {', '.join(spostati)}")
        except Exception as e:
            print(f"Sessione {f.stem}: file di lavoro non spostati ({e})")


def _nel_cestino(percorso: Path) -> None:
    """Sposta nel Cestino di macOS (recuperabile), senza cancellare definitivamente."""
    from Foundation import NSFileManager, NSURL
    ok, _, errore = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(percorso)), None, None)
    if not ok:
        raise RuntimeError(f"Non riesco a spostare nel Cestino {percorso.name}: {errore}")


def elimina_sessione(id_: str, risultati: bool) -> dict:
    """Elimina una sessione da Vertov. I materiali originali (video, audio, documenti) non
    vengono mai toccati; i risultati vanno nel Cestino solo se richiesto e se nessun'altra
    sessione li usa. Restituisce la sessione da mostrare dopo."""
    if LAVORO.attivo() and LAVORO.sessione == id_:
        raise RuntimeError("Questa sessione ha un'elaborazione in corso: fermala prima di eliminarla.")
    p = json.loads(_file_sessione(id_).read_text())
    altre = [json.loads(f.read_text()) for f in SESSIONI.glob("*.json") if f.stem != id_]
    spostati = []
    if risultati:
        materiali = materiali_di(p)
        if _lavoro(p).is_dir():
            _nel_cestino(_lavoro(p))
            spostati.append(_lavoro(p).name)
        for x in p.get("esportati", []):
            f = Path(x)
            if f.exists() and str(f.resolve()) not in materiali:  # mai un file scelto come materiale
                _nel_cestino(f)
                spostati.append(f.name)
        out = Path(p.get("out") or "/nonesiste")
        if out.is_dir() and not any(a.get("out") == p["out"] for a in altre) and \
                not any(x for x in out.iterdir() if x.name != ".DS_Store"):
            _nel_cestino(out)  # la cartella era solo di Vertov ed è rimasta vuota
    # Fotogrammi in cache (rigenerabili): si tolgono se nessun'altra sessione usa lo stesso video.
    if p.get("video") and not any(Path(a.get("video") or "").stem == Path(p["video"]).stem for a in altre):
        import shutil, tempfile
        shutil.rmtree(Path(tempfile.gettempdir()) / f"descrittore_{Path(p['video']).stem}", ignore_errors=True)
    (SESSIONI / f"{id_}_personaggi.txt").unlink(missing_ok=True)
    _file_sessione(id_).unlink()
    # Se era la sessione aperta, si passa alla più recente (o a una nuova).
    try:
        corrente = json.loads(FILE_CORRENTE.read_text()).get("id")
    except (OSError, ValueError):
        corrente = None
    if corrente == id_ or corrente is None:
        rimaste = elenco_sessioni()
        prossima = json.loads(_file_sessione(rimaste[0]["id"]).read_text()) if rimaste else nuova_sessione()
        _scrivi_sessione(prossima)
    return {"spostati": spostati}


def elenco_sessioni() -> list:
    elenco = []
    for f in SESSIONI.glob("*.json"):
        try:
            p = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        descritte = 0
        if (_lavoro(p) / "inquadrature.jsonl").exists():
            descritte = sum(1 for r in (_lavoro(p) / "inquadrature.jsonl").open(encoding="utf-8") if r.strip())
        elenco.append({"id": p["id"], "nome": p.get("nome", ""), "modificata": p.get("modificata", ""),
                       "video": Path(p["video"]).name if p.get("video") else "", "descritte": descritte})
    return sorted(elenco, key=lambda e: e["modificata"], reverse=True)


def file_personaggi(p: dict) -> Path:
    return cartella_lavoro(p) / "personaggi_utente.txt"


def lista_odg(p: dict) -> list:
    odg = p.get("odg") or []
    return [odg] if isinstance(odg, str) else odg


# --------------------------------------------------------------------------
# Materiali trascinati: riconoscimento e assegnazione dei ruoli
# --------------------------------------------------------------------------

def togli_materiale(p: dict, percorso: str) -> None:
    for ruolo in riconosci.RUOLI:
        if ruolo == "odg":
            p["odg"] = [x for x in lista_odg(p) if x != percorso]
        elif ruolo == "personaggi":
            p["schede"] = [x for x in p.get("schede", []) if x != percorso]
        elif p.get(ruolo) == percorso:
            p[ruolo] = ""
    p["scartati"] = [x for x in p.get("scartati", []) if x["percorso"] != percorso]


def assegna(p: dict, percorso: str, ruolo: str) -> None:
    """Mette un file in un ruolo, togliendolo da quello che aveva prima."""
    togli_materiale(p, percorso)
    if ruolo == "odg":
        p["odg"] = lista_odg(p) + [percorso]
    elif ruolo == "personaggi":
        f = file_personaggi(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        attuale = f.read_text(encoding="utf-8") if f.exists() else ""
        nuovo = Path(percorso).read_text(encoding="utf-8", errors="ignore").strip()
        if nuovo not in attuale:
            f.write_text((attuale.rstrip() + "\n" if attuale.strip() else "") + nuovo + "\n", encoding="utf-8")
        p["schede"] = p.get("schede", []) + [percorso]
    else:
        p[ruolo] = percorso
    if ruolo == "video":
        video = Path(percorso)
        if not p.get("out"):
            p["out"] = str(video.parent)  # le esportazioni, accanto al video
        if p.get("nome") in ("", "Nuova sessione"):
            p["nome"] = video.stem
        cartella_lavoro(p)
    if ruolo.startswith("audio_") and not p.get("dialoghi") and "trascrivi" not in p:
        p["trascrivi"] = True  # senza trascrizione, i dialoghi si trascrivono dallo stem


def aggiungi_materiali(p: dict, percorsi: list) -> list:
    """Riconosce i file e li assegna. Restituisce gli esiti da mostrare."""
    voci = riconosci.classifica([x.rstrip("/") for x in percorsi])
    # Se arrivano più video o più sceneggiature insieme, si usa il video più lungo e la
    # sceneggiatura con meno pagine (le versioni "una scena per pagina" sono più lunghe).
    for ruolo, migliore in (("video", lambda v: -v.get("durata", 0)), ("sceneggiatura", lambda v: v.get("pagine", 0))):
        stessi = sorted([v for v in voci if v.get("ruolo") == ruolo], key=migliore)
        for v in stessi[1:]:
            v["ruolo"], v["problema"] = None, f"Non usato: come {riconosci.RUOLI[ruolo][0].lower()} uso {stessi[0]['nome']}."
    info = p.setdefault("info", {})
    for v in voci:
        if v.get("ruolo"):
            assegna(p, v["percorso"], v["ruolo"])
            info[v["percorso"]] = {"dettaglio": v.get("dettaglio", ""), "come": v.get("come", "")}
        else:
            togli_materiale(p, v["percorso"])
            p.setdefault("scartati", []).append({k: v.get(k, "") for k in ("percorso", "nome", "problema", "dettaglio")})
    salva_progetto(p)
    return voci


def assenti(p: dict) -> list:
    """Materiali facoltativi che non si trovano (spostati, o su un disco scollegato): l'analisi va avanti senza."""
    scelti = [p[c] for c, opzione in OPZIONI.items() if opzione and p.get(c)] + lista_odg(p)
    return [x for x in scelti if not Path(x).exists()]


def comando(p: dict, solo_volti: bool = False, rifai: list = ()) -> list:
    # prepara.py avvia Ollama e scarica il modello se serve, poi passa a descrivi.py.
    modello = p.get("modello") or MODELLO
    cmd = [sys.executable, "-u", str(CARTELLA / "prepara.py"), "--modello", modello, "--",
           str(CARTELLA / "descrivi.py"), p["video"], "--out", str(cartella_lavoro(p)), "--modello", modello]
    mancano = assenti(p)
    for campo, opzione in OPZIONI.items():
        if opzione and p.get(campo) and p[campo] not in mancano:
            cmd += [opzione, p[campo]]
    odg = [x for x in lista_odg(p) if x not in mancano]
    if odg:
        cmd += ["--odg", *odg]
    if (not p.get("dialoghi") or p["dialoghi"] in mancano) and p.get("trascrivi", True):  # senza trascrizione: Whisper
        cmd.append("--trascrivi")
    if file_personaggi(p).exists() and file_personaggi(p).read_text(encoding="utf-8").strip():
        cmd += ["--personaggi", str(file_personaggi(p))]
    if solo_volti:
        cmd += ["--solo-volti", "--senza-audio"]
    if rifai:
        cmd += ["--rifai", ",".join(map(str, rifai))]
    return cmd


# --------------------------------------------------------------------------
# Lavoro in corso (un solo processo alla volta)
# --------------------------------------------------------------------------

# Stima del tempo totale: secondi per inquadratura di ogni fase. Partono da valori tipici
# e vengono sostituiti dai tempi misurati su questo Mac alla fine di ogni analisi.
FILE_TEMPI = DATI / "tempi.json"
TEMPI_TIPICI = {"fotogrammi": 0.4, "fotogrammi_volti": 0.6, "volti": 1.2, "confronto_volti": 0.2,
                "descrizioni": 11.0, "report": 0.6}
# Le fasi che restano, nell'ordine in cui arrivano, per ogni tipo di lavoro.
SEQUENZE = {"descrizioni": ["fotogrammi", "fotogrammi_volti", "volti", "confronto_volti", "descrizioni", "report"],
            "correzione": ["fotogrammi", "descrizioni", "report"],
            "volti": ["fotogrammi", "descrizioni", "report"]}


def tempi_per_inquadratura() -> dict:
    try:
        return {**TEMPI_TIPICI, **json.loads(FILE_TEMPI.read_text())}
    except (OSError, ValueError):
        return dict(TEMPI_TIPICI)


class Lavoro:
    def __init__(self):
        self.processo = None
        self.tipo = ""
        self.log = deque(maxlen=300)
        self.fase = ""
        self.avanz = {}
        self.inizio = 0.0
        self.inizio_fase = {}
        self.codice = None
        self.sessione = ""
        self.rifai = []
        self.avvisi = []      # cose da verificare segnalate durante il lavoro (righe "@@avviso")
        self.fermata = False  # fermata dall'utente (Ferma o chiusura della finestra), non un errore
        self.esito = None     # com'è andata, da mostrare finché l'utente non preme OK
        self.blocco = threading.Lock()

    def attivo(self) -> bool:
        return self.processo is not None and self.processo.poll() is None

    def avvia(self, cmd: list, tipo: str, sessione: str, rifai: list = (), assenti: list = ()) -> None:
        with self.blocco:
            if self.attivo():
                raise RuntimeError("C'è già un'elaborazione in corso.")
            if coordinamento.analisi_in_corso():
                raise RuntimeError("C'è già un'analisi di Vertov in corso, avviata fuori da questa finestra: due "
                                   "analisi insieme si rubano il modello a vicenda. Fermala o aspetta che finisca.")
            self.tipo, self.fase, self.avanz, self.codice = tipo, "avvio", {}, None
            self.sessione, self.rifai = sessione, list(rifai)
            self.inizio_fase = {}
            self.log.clear()
            self.avvisi, self.fermata, self.esito = [], False, None
            for x in assenti:
                avviso = f"Non trovo {Path(x).name}: vado avanti senza (il file è stato spostato o il disco è scollegato)."
                self.log.append(avviso)
                self.avvisi.append(avviso)
            self.inizio = time.time()
            self.processo = subprocess.Popen(
                cmd, cwd=CARTELLA, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, start_new_session=True,
                env={**os.environ, "PYTHONWARNINGS": "ignore", "TQDM_DISABLE": "1",
                     # l'analisi si spegne da sola se l'app non c'è più (chiusa, forzata, in crash)
                     coordinamento.VARIABILE_GENITORE: str(os.getpid())})
        threading.Thread(target=self._leggi, daemon=True).start()

    def _leggi(self) -> None:
        for riga in self.processo.stdout:
            riga = riga.rstrip()
            if riga.startswith("@@fase "):
                self.fase = riga.split(" ", 1)[1]
                self.inizio_fase[self.fase] = time.time()
            elif riga.startswith("@@avanz "):
                _, nome, fatti, totale = riga.split()
                precedente = self.avanz.get(nome, {})
                self.avanz[nome] = {"fatti": int(fatti), "totale": int(totale),
                                    "primo": precedente.get("primo", (int(fatti), time.time())),
                                    "ora": time.time()}
            elif riga.startswith("@@avviso "):
                self.avvisi.append(riga.split(" ", 1)[1])
                self.log.append(riga.split(" ", 1)[1])
            elif riga and "warn" not in riga.lower() and "%|" not in riga:
                self.log.append(riga)
        self.codice = self.processo.wait()
        if self.codice == 0:
            self.impara_tempi()
        if not self.fermata:
            self.esito = esito_del_lavoro(self)
            avvisa_fine(self.esito)

    def impara_tempi(self) -> None:
        """Salva quanto ha richiesto ogni fase per inquadratura, per stimare meglio le prossime volte."""
        fine = time.time()
        inizi = sorted(self.inizio_fase.items(), key=lambda x: x[1])
        tempi = tempi_per_inquadratura()
        for (fase, t0), seguente in zip(inizi, inizi[1:] + [("", fine)]):
            a = self.avanz.get(fase)
            if fase not in TEMPI_TIPICI or not a or a["totale"] < 5:
                continue
            fatti = a["fatti"] - a["primo"][0] if fase == "descrizioni" else a["totale"]
            if fatti >= 5:
                # Media con il valore precedente: un'analisi un po' più lenta non stravolge le stime.
                tempi[fase] = round(0.5 * tempi[fase] + 0.5 * (seguente[1] - t0) / fatti, 3)
        try:
            DATI.mkdir(parents=True, exist_ok=True)
            FILE_TEMPI.write_text(json.dumps(tempi, indent=1))
        except OSError:
            pass

    def stima(self) -> dict:
        """Percentuale complessiva e tempo che manca, sommando la fase in corso e quelle che seguono."""
        if not self.attivo():
            return {"percento": None, "restano": None}
        tempi = tempi_per_inquadratura()
        trascorso = time.time() - self.inizio
        # Quante inquadrature si lavorano: lo dice la prima fase che le conta.
        n = next((a["totale"] for f in ("fotogrammi", "descrizioni") if (a := self.avanz.get(f))), None)
        sequenza = SEQUENZE.get(self.tipo, [])
        if n is None or self.fase not in sequenza:
            return {"percento": None, "restano": None}  # avvio, download del modello, trascrizione
        restano = 0.0
        a = self.avanz.get(self.fase)
        if a and a["totale"]:
            fatti0, t0 = a["primo"]
            if a["fatti"] - fatti0 >= 2:
                passo = (a["ora"] - t0) / (a["fatti"] - fatti0)
            else:
                passo = tempi.get(self.fase, 0)
            restano += passo * (a["totale"] - a["fatti"])
            # Il tempo scende anche mentre lavora su un'inquadratura, senza scavalcare la successiva.
            if a["fatti"] < a["totale"]:
                restano -= min(time.time() - a["ora"], passo * 0.9)
        else:
            restano += tempi.get(self.fase, 0) * n
        da_descrivere = n
        if (d := self.avanz.get("descrizioni")):
            da_descrivere = d["totale"] - d["fatti"]
        for fase in sequenza[sequenza.index(self.fase) + 1:]:
            restano += tempi.get(fase, 0) * (da_descrivere if fase == "descrizioni" else n)
        restano = max(restano, 0.0)
        return {"percento": min(99, int(100 * trascorso / (trascorso + restano))) if trascorso + restano else 0,
                "restano": restano}

    def ferma(self) -> None:
        """Ferma l'analisi e tutto ciò che ha avviato; se entro 5 secondi non è uscita, la chiude d'ufficio."""
        if not self.attivo():
            return
        processo = self.processo
        self.fermata = True
        try:
            os.killpg(processo.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            processo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(processo.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stato(self) -> dict:
        avanz = {}
        for nome, a in self.avanz.items():
            fatti0, t0 = a["primo"]
            eta = None
            if a["fatti"] > fatti0 and a["fatti"] < a["totale"]:
                al_pezzo = (a["ora"] - t0) / (a["fatti"] - fatti0)
                eta = al_pezzo * (a["totale"] - a["fatti"])
            avanz[nome] = {"fatti": a["fatti"], "totale": a["totale"], "eta": eta}
        return {"attivo": self.attivo(), "tipo": self.tipo, "fase": self.fase, "avanzamento": avanz,
                "durata": time.time() - self.inizio if self.inizio else 0, "codice": self.codice,
                "fermata": self.fermata, "esito": self.esito,
                "sessione": self.sessione, "rifai": self.rifai, "log": list(self.log)[-60:], **self.stima()}


LAVORO = Lavoro()


def _ultimo_errore(log: list) -> str:
    """La riga che spiega l'errore: di solito l'ultima di un traceback di Python."""
    righe = [r.strip() for r in log if r.strip() and not r.startswith("@@")]
    riga = next((r for r in reversed(righe) if re.match(r"^[A-Za-z_.]*(Error|Exception)\b", r)),
                righe[-1] if righe else "")
    # Gli errori più comuni, detti in parole semplici (il testo tecnico resta nei dettagli).
    if m := re.search(r"Command \['(ffprobe|ffmpeg)'.*'([^']+)'\] returned non-zero", riga):
        return f"ffmpeg non riesce a leggere {Path(m.group(2)).name}: il file è danneggiato o non è un video."
    if m := re.match(r"FileNotFoundError: .*?'([^']+)'", riga):
        return f"Non trovo {Path(m.group(1)).name}: è stato spostato o il disco è scollegato."
    if "Connection refused" in riga or "URLError" in riga:
        return "Ollama non risponde: forse si è chiuso. Riaprilo (o riavvia il Mac) e rilancia."
    return re.sub(r"^[A-Za-z_.]*(Error|Exception):\s*", "", riga)


def esito_del_lavoro(l) -> dict:
    """Com'è andata un'elaborazione finita da sola: senza errori, con cose da verificare o interrotta."""
    titoli = {"descrizioni": "Analisi del film completata", "volti": "Descrizioni aggiornate con i nomi dei volti",
              "correzione": "Scena aggiornata con la tua correzione"}
    if l.codice != 0:
        motivo = _ultimo_errore(list(l.log))
        return {"stato": "errore", "titolo": "L'elaborazione si è interrotta per un errore",
                "punti": ([f"Motivo: {motivo}"] if motivo else []) +
                         ["Il lavoro fatto fin qui è salvato: rilanciandola riprende da dove era arrivata.",
                          "I dettagli sono in «Dettagli dell'elaborazione»."]}
    punti = list(dict.fromkeys(l.avvisi))
    if l.tipo == "descrizioni":
        try:
            p = json.loads(_file_sessione(l.sessione).read_text())
            proposti = sum(1 for g in leggi_gruppi(_lavoro(p))
                           if g.get("nome") and not g.get("confermato") and g["nome"] != "comparsa")
        except (OSError, ValueError):
            proposti = 0
        if proposti:
            punti.append(f"Vertov ha dato un nome a {proposti} persone da solo: controlla i volti per confermarli.")
    return {"stato": "verifica" if punti else "ok", "titolo": titoli.get(l.tipo, "Volti pronti: controlla i nomi"),
            "punti": punti}


def avvisa_fine(esito: dict) -> None:
    """Suono e notifica di macOS quando un'elaborazione finisce da sola (non se l'ha fermata l'utente).
    Nella finestra di Vertov compare poi il riquadro con l'esito, da chiudere con OK."""
    if esito["stato"] == "errore":
        suono = Path("/System/Library/Sounds/Basso.aiff")
    elif esito["titolo"].startswith("Analisi"):
        suono = CARTELLA / "suono_fine.wav"
    else:
        suono = Path("/System/Library/Sounds/Glass.aiff")
    testo = esito["titolo"] + (": ci sono cose da verificare." if esito["stato"] == "verifica" else ".")
    subprocess.Popen(["afplay", str(suono)])
    subprocess.Popen(["osascript", "-e", f'display notification {json.dumps(testo)} with title "Vertov"'])


# --------------------------------------------------------------------------
# Controlli e utilità
# --------------------------------------------------------------------------

def prerequisiti() -> dict:
    stato = {"ollama": False, "modello": False}
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            modelli = [m["name"] for m in json.load(r).get("models", [])]
        stato["ollama"] = True
        stato["modello"] = any(m.startswith(MODELLO) for m in modelli)
    except Exception:
        pass
    return stato


FINESTRA = None  # la finestra dell'app, per le finestre di scelta dei file


def scegli(tipo: str, messaggio: str):
    """Finestra di scelta nativa; restituisce il percorso o None se annullata."""
    import webview
    finestra_dialogo = getattr(webview, "FileDialog", None)
    if finestra_dialogo:
        modo = finestra_dialogo.FOLDER if tipo == "cartella" else finestra_dialogo.OPEN
    else:
        modo = webview.FOLDER_DIALOG if tipo == "cartella" else webview.OPEN_DIALOG
    scelti = FINESTRA.create_file_dialog(modo, allow_multiple=tipo != "cartella")
    if not scelti:
        return None
    scelti = list(scelti) if isinstance(scelti, (list, tuple)) else [scelti]
    if tipo == "file":
        return scelti
    return scelti[0].rstrip("/")


def leggi_gruppi(out: Path) -> list:
    try:
        return json.loads((out / "volti" / "gruppi_info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def salva_nomi(out: Path, nomi: dict) -> None:
    file_nomi = out / "volti" / "nomi.txt"
    righe = ["# Nomi confermati dall'app. Un nome vuoto significa: persona da non nominare."]
    gia = set()
    if file_nomi.exists():
        # Si conservano le righe dei gruppi che l'app non mostra (es. elaborazioni parziali).
        for riga in file_nomi.read_text(encoding="utf-8").splitlines():
            if ":" in riga and not riga.lstrip().startswith("#"):
                g = riga.split(":", 1)[0].strip()
                if g not in nomi:
                    righe.append(riga)
                    gia.add(g)
    for g, nome in sorted(nomi.items()):
        righe.append(f"{g}: {nome.strip()}")
    file_nomi.write_text("\n".join(righe) + "\n", encoding="utf-8")


def leggi_correzioni(out: Path) -> dict:
    try:
        return json.loads((out / "correzioni.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def scena_da_clip(clip: str) -> str:
    m = re.match(r"^\s*(\d+[A-Z]?)\s*/", clip or "")
    return m.group(1) if m else ""


def aggiorna_info_gruppi(out: Path, nomi: dict) -> None:
    """Riporta i nomi confermati in gruppi_info.json, così la finestra dei volti li mostra subito."""
    f = out / "volti" / "gruppi_info.json"
    gruppi = leggi_gruppi(out)
    for g in gruppi:
        if g["gruppo"] in nomi:
            g["nome"], g["confermato"] = nomi[g["gruppo"]].strip(), True
    f.write_text(json.dumps(gruppi, ensure_ascii=False), encoding="utf-8")


def apri_report() -> None:
    """Il report in una finestra di Vertov, con il pulsante per salvarlo in PDF."""
    import webview
    webview.create_window("Report di Vertov", f"http://127.0.0.1:{PORTA}/report", width=1000, height=860,
                          background_color="#f4ede0", text_select=True)


# Cosa si può esportare: file nella cartella di lavoro e come si chiama, una volta esportato,
# dopo il nome della sessione.
ESPORTAZIONI = {
    "csv": ("inquadrature.csv", "descrizioni.csv"),
    "resolve": ("marker_resolve.edl", "marker Resolve.edl"),
    "report": ("report_vertov.html", "report.html"),
    "dialoghi": ("dialoghi_trascritti.srt", "dialoghi.srt"),
    "dati": ("inquadrature.jsonl", "dati.jsonl"),
}


def esporta(p: dict, formati: list) -> list:
    """Copia nella cartella di destinazione i formati scelti, con il nome della sessione."""
    import shutil
    if not formati:
        raise RuntimeError("Scegli almeno un tipo di file da esportare.")
    if not p.get("out"):
        raise RuntimeError("Scegli prima la cartella in cui esportare.")
    dest = Path(p["out"])
    dest.mkdir(parents=True, exist_ok=True)
    materiali, creati = materiali_di(p), []
    for f in formati:
        sorgente, suffisso = ESPORTAZIONI[f]
        if not (_lavoro(p) / sorgente).exists():
            raise RuntimeError("Non c'è ancora niente da esportare: prima descrivi il film.")
        file = dest / f"{nome_per_file(p.get('nome'))} - {suffisso}"
        if str(file.resolve()) in materiali:  # mai sovrascrivere un materiale
            raise RuntimeError(f"{file.name} è uno dei materiali della sessione: scegli un'altra cartella.")
        shutil.copyfile(_lavoro(p) / sorgente, file)
        creati.append(str(file))
    return creati


def leggi_risultati(out: Path) -> list:
    risultati = {}
    try:
        for riga in (out / "inquadrature.jsonl").read_text(encoding="utf-8").splitlines():
            if riga.strip():
                r = json.loads(riga)
                riga_r = {k: r.get(k) for k in (
                    "numero", "numero_scena", "tc_in", "tc_out", "durata_s", "scena", "piano",
                    "movimento_macchina", "personaggi", "azione", "sonoro", "dialoghi_riassunto",
                    "descrizione", "dialoghi", "corretta", "montaggio", "clip")}
                if not riga_r["numero_scena"]:
                    # Risultati di versioni precedenti: scena dal nome clip (scena/inquadratura/ciak).
                    riga_r["numero_scena"] = scena_da_clip(r.get("clip"))
                risultati[riga_r["numero"]] = riga_r  # una riga rifatta sostituisce la precedente
    except OSError:
        pass
    return [risultati[n] for n in sorted(risultati)]


def inquadrature_della_scena(out: Path, numero: int) -> list:
    """Le inquadrature della stessa scena di quella indicata (per estendere una correzione)."""
    risultati = leggi_risultati(out)
    questa = next((r for r in risultati if r["numero"] == numero), None)
    if questa is None:
        raise RuntimeError(f"Inquadratura {numero} non trovata.")
    chiave = (questa["scena"] or questa["numero_scena"])
    if not chiave:
        return [numero]
    return [r["numero"] for r in risultati if (r["scena"] or r["numero_scena"]) == chiave]


def correggi(progetto: dict, numero: int, testo: str) -> list:
    out = _lavoro(progetto)
    correzioni = leggi_correzioni(out)
    if testo.strip():
        correzioni[str(numero)] = testo.strip()
    else:
        correzioni.pop(str(numero), None)
    (out / "correzioni.json").write_text(json.dumps(correzioni, ensure_ascii=False, indent=1), encoding="utf-8")
    scena = inquadrature_della_scena(out, numero)
    LAVORO.avvia(comando(progetto, rifai=scena), "correzione", progetto["id"], scena, assenti(progetto))
    return scena


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class Gestore(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, dati, codice=200):
        corpo = json.dumps(dati, ensure_ascii=False).encode()
        self.send_response(codice)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        progetto = carica_progetto()
        if url.path == "/":
            corpo = (CARTELLA / "interfaccia.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        elif url.path == "/api/stato":
            personaggi = ""
            if file_personaggi(progetto).exists():
                personaggi = file_personaggi(progetto).read_text(encoding="utf-8")
            materiali = [progetto.get(r) for r in riconosci.RUOLI if r not in ("odg", "personaggi")]
            materiali += lista_odg(progetto) + progetto.get("schede", [])
            mancanti = [m for m in materiali if m and not Path(m).exists()]
            out = _lavoro(progetto)
            nostra = LAVORO.processo.pid if LAVORO.attivo() else None
            esterne = [{"pid": a["pid"], "film": Path(a["video"]).name if a.get("video") else "",
                        "origine": a.get("origine", "sconosciuta"),
                        "da": time.time() - a["avviata"] if a.get("avviata") else None,
                        "fase": a.get("fase", ""), "fatti": a.get("fatti", 0), "totale": a.get("totale", 0),
                        "out": a.get("out", "")}
                       for a in coordinamento.analisi_in_corso() if a["pid"] != nostra]
            self._json({"progetto": progetto, "lavoro": LAVORO.stato(), "mancanti": mancanti, "esterne": esterne,
                        "volti_pronti": (out / "volti" / "gruppi_info.json").exists(),
                        "volti_proposti": sum(1 for g in leggi_gruppi(out) if g.get("nome") and not g.get("confermato")
                                              and g["nome"] != "comparsa"),
                        "report_pronto": (out / "report_vertov.html").exists(),
                        "esportabili": {k: (out / v[0]).exists() for k, v in ESPORTAZIONI.items()},
                        "prerequisiti": prerequisiti(), "personaggi": personaggi})
        elif url.path == "/api/sessioni":
            self._json({"corrente": progetto["id"], "sessioni": elenco_sessioni()})
        elif url.path == "/api/aggiornamenti":
            self._json({"installazione": aggiornamenti.INSTALLAZIONE, "versione": aggiornamenti.versione(),
                        "automatico": aggiornamenti.leggi_stato().get("automatico", True)})
        elif url.path == "/api/pulizia":
            self._json({"voci": pulizia.voci(MODELLO)})
        elif url.path == "/api/volti":
            self._json(leggi_gruppi(_lavoro(progetto)))
        elif url.path == "/api/risultati":
            self._json(leggi_risultati(_lavoro(progetto)))
        elif url.path == "/aiuto":
            corpo = (CARTELLA / "aiuto.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        elif url.path == "/report":
            report = _lavoro(progetto) / "report_vertov.html"
            if not report.exists():
                self.send_error(404)
                return
            corpo = report.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        elif url.path == "/img":
            # Solo immagini dentro la cartella di lavoro.
            rel = parse_qs(url.query).get("p", [""])[0]
            out = _lavoro(progetto).resolve()
            percorso = (out / "volti" / rel).resolve()
            if out not in percorso.parents or percorso.suffix != ".jpg" or not percorso.exists():
                self.send_error(404)
                return
            dati = percorso.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(dati)))
            self.end_headers()
            self.wfile.write(dati)
        else:
            self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        dati = self._corpo()
        progetto = carica_progetto()
        try:
            if url.path == "/api/scegli":
                self._json({"percorso": scegli(dati.get("tipo", "cartella"), dati.get("messaggio", "Scegli"))})
            elif url.path == "/api/sfoglia":
                # Finestra di scelta: i file scelti vengono riconosciuti come quelli trascinati.
                scelti = scegli(dati.get("tipo", "file"), "Materiali")
                if not scelti:
                    self._json({"esiti": []})
                    return
                scelti = scelti if isinstance(scelti, list) else [scelti]
                self._json({"esiti": aggiungi_materiali(progetto, scelti), "progetto": progetto})
            elif url.path == "/api/aggiungi":
                self._json({"esiti": aggiungi_materiali(progetto, dati.get("percorsi", [])), "progetto": progetto})
            elif url.path == "/api/ruolo":
                if dati.get("ruolo"):
                    assegna(progetto, dati["percorso"], dati["ruolo"])
                else:
                    togli_materiale(progetto, dati["percorso"])
                salva_progetto(progetto)
                self._json({"ok": True, "progetto": progetto})
            elif url.path == "/api/progetto":
                progetto.update({k: v for k, v in dati.items() if k in CAMPI_FILE + ["trascrivi", "modello", "nome"]})
                if dati.get("video") and not progetto.get("out"):
                    # Le esportazioni vanno, se non si sceglie altro, accanto al video.
                    progetto["out"] = str(Path(dati["video"]).parent)
                if dati.get("video") and progetto.get("nome") in ("", "Nuova sessione"):
                    progetto["nome"] = Path(dati["video"]).stem
                if dati.get("video"):
                    cartella_lavoro(progetto)
                salva_progetto(progetto)
                self._json({"ok": True, "progetto": progetto})
            elif url.path == "/api/sessione":
                azione = dati.get("azione")
                if azione in ("nuova", "apri") and LAVORO.attivo():
                    raise RuntimeError("C'è un'elaborazione in corso: fermala prima di cambiare sessione.")
                if azione == "nuova":
                    progetto = nuova_sessione(dati.get("nome", ""))
                elif azione == "apri":
                    progetto = json.loads(_file_sessione(dati["id"]).read_text())
                    _scrivi_sessione(progetto)
                elif azione == "elimina":
                    esito = elimina_sessione(dati["id"], bool(dati.get("risultati")))
                    self._json({"ok": True, "progetto": carica_progetto(), **esito})
                    return
                elif azione == "salva":
                    if dati.get("nome", "").strip():
                        progetto["nome"] = dati["nome"].strip()
                        rinomina_cartella_lavoro(progetto)
                    salva_progetto(progetto)
                self._json({"ok": True, "progetto": progetto})
            elif url.path == "/api/correggi":
                scena = correggi(progetto, int(dati["numero"]), dati.get("testo", ""))
                self._json({"ok": True, "inquadrature": scena})
            elif url.path == "/api/personaggi":
                file_personaggi(progetto).parent.mkdir(parents=True, exist_ok=True)
                file_personaggi(progetto).write_text(dati.get("testo", ""), encoding="utf-8")
                self._json({"ok": True})
            elif url.path == "/api/avvia":
                if not progetto.get("video"):
                    raise RuntimeError("Manca il video.")
                if not Path(progetto["video"]).exists():
                    raise RuntimeError("Il video non si trova più: trascinalo di nuovo in Materiali.")
                LAVORO.avvia(comando(progetto, dati.get("fase") == "volti"), dati.get("fase", ""), progetto["id"],
                             assenti=assenti(progetto))
                self._json({"ok": True})
            elif url.path == "/api/ferma":
                LAVORO.ferma()
                self._json({"ok": True})
            elif url.path == "/api/ferma-esterna":
                pid = int(dati["pid"])
                if not coordinamento.e_vertov(pid):  # mai fermare un processo che non sia un'analisi di Vertov
                    raise RuntimeError("Quell'analisi non è più in corso.")
                os.kill(pid, signal.SIGTERM)
                self._json({"ok": True})
            elif url.path == "/api/nomi":
                out = _lavoro(progetto)
                prima = {g["gruppo"]: g for g in leggi_gruppi(out)}
                salva_nomi(out, dati.get("nomi", {}))
                # Le inquadrature dove compare un volto rinominato vengono ridescritte con il nome giusto.
                cambiati = [g for g, n in dati.get("nomi", {}).items() if g in prima and n.strip() != prima[g]["nome"]]
                descritte = {r["numero"] for r in leggi_risultati(out)}
                da_rifare = sorted({n for g in cambiati for n in prima[g]["inquadrature"]} & descritte)
                aggiorna_info_gruppi(out, dati.get("nomi", {}))
                if da_rifare and dati.get("aggiorna", True):
                    LAVORO.avvia(comando(progetto, rifai=da_rifare), "volti", progetto["id"], da_rifare, assenti(progetto))
                self._json({"ok": True, "cambiati": cambiati, "inquadrature": da_rifare})
            elif url.path == "/api/report":
                if not (_lavoro(progetto) / "report_vertov.html").exists():
                    raise RuntimeError("Il report non c'è ancora: viene creato alla fine dell'analisi.")
                apri_report()
                self._json({"ok": True})
            elif url.path == "/api/esporta":
                if dati.get("cartella"):
                    progetto["out"] = dati["cartella"]
                progetto["formati"] = [f for f in dati.get("formati", []) if f in ESPORTAZIONI]
                creati = esporta(progetto, progetto["formati"])
                progetto["esportati"] = sorted(set(progetto.get("esportati", [])) | set(creati))
                salva_progetto(progetto)
                self._json({"ok": True, "creati": creati, "cartella": progetto["out"]})
            elif url.path == "/api/esito-visto":
                LAVORO.esito = None
                self._json({"ok": True})
            elif url.path == "/api/aggiornamenti":
                azione = dati.get("azione")
                if azione == "controlla":
                    manuale = bool(dati.get("manuale"))
                    if not manuale and not aggiornamenti.da_controllare():
                        self._json({"da_fare": False})
                    else:
                        self._json(aggiornamenti.controlla(MODELLO, manuale))
                elif azione == "installa":
                    if LAVORO.attivo():
                        raise RuntimeError("Aspetta che finisca l'elaborazione in corso, o fermala, prima di aggiornare.")
                    aggiornamenti.installa(dati.get("voci") or [], MODELLO)
                    self._json({"ok": True})
                elif azione == "salta":
                    aggiornamenti.salta(dati.get("voci") or [])
                    self._json({"ok": True})
                elif azione == "automatico":
                    aggiornamenti.scrivi_stato(automatico=bool(dati.get("valore")))
                    self._json({"ok": True})
                elif azione == "riavvia":
                    self._json({"ok": True})
                    aggiornamenti.riavvia(PORTA)
                    threading.Timer(0.3, FINESTRA.destroy).start()
                else:
                    self.send_error(404)
            elif url.path == "/api/pulizia":
                if LAVORO.attivo() or aggiornamenti.INSTALLAZIONE["attiva"]:
                    raise RuntimeError("Aspetta che finisca il lavoro in corso prima di liberare spazio.")
                self._json({"liberati": pulizia.cancella(dati.get("chiavi") or [], MODELLO)})
            elif url.path == "/api/apri":
                # La cartella di destinazione, con selezionati i file appena esportati, o quella di lavoro.
                creati = [x for x in dati.get("file", []) if x in progetto.get("esportati", []) and Path(x).exists()]
                if dati.get("cosa") == "lavoro":
                    subprocess.run(["open", str(cartella_lavoro(progetto))])
                elif creati:
                    subprocess.run(["open", "-R", *creati])
                elif progetto.get("out") and Path(progetto["out"]).is_dir():
                    subprocess.run(["open", progetto["out"]])
                else:
                    raise RuntimeError("La cartella di destinazione non esiste.")
                self._json({"ok": True})
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"errore": str(e)}, 400)


def identita_mac() -> None:
    """Nome e icona di Vertov nel Dock e nella barra dei menu (invece di "Python")."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "Vertov"
        info["CFBundleShortVersionString"] = aggiornamenti.versione()
        icona = NSImage.alloc().initWithContentsOfFile_(str(CARTELLA / "icona_vertov.png"))
        if icona:
            NSApplication.sharedApplication().setApplicationIconImage_(icona)
    except Exception:
        pass


def collega_trascinamento(finestra) -> None:
    """File e cartelle lasciati sulla finestra: percorsi completi dal lato nativo."""
    from webview.dom import DOMEventHandler

    def lasciati(evento):
        percorsi = [f.get("pywebviewFullPath") for f in evento.get("dataTransfer", {}).get("files", [])]
        percorsi = [x for x in percorsi if x]
        nomi = [f.get("name") for f in evento.get("dataTransfer", {}).get("files", [])]
        print(f"Trascinati {len(nomi)} elementi {nomi}: percorsi trovati {percorsi}", flush=True)
        if not percorsi:
            if nomi:
                finestra.evaluate_js("document.getElementById('esito-arrivo').textContent = "
                                     "'Non ho ricevuto il percorso dei file trascinati: riprova o usa «scegli i file…».'")
            return
        finestra.evaluate_js(f"materialiInArrivo({len(percorsi)})")
        try:
            esiti = aggiungi_materiali(carica_progetto(), percorsi)
        except Exception as e:
            esiti = [{"nome": "", "ruolo": None, "problema": str(e)}]
        finestra.evaluate_js(f"materialiAggiunti({json.dumps(esiti, ensure_ascii=False)})")

    finestra.dom.document.events.dragover += DOMEventHandler(lambda e: None, True, True, debounce=500)
    finestra.dom.document.events.drop += DOMEventHandler(lasciati, True, True)


# --------------------------------------------------------------------------
# Menu della barra di macOS
# --------------------------------------------------------------------------

def _js(comando: str):
    """Azione di menu che esegue un comando dell'interfaccia (vertovComando in interfaccia.html)."""
    def azione():
        if FINESTRA:
            FINESTRA.evaluate_js(f"vertovComando({json.dumps(comando)})")
    azione.__name__ = "menu_" + re.sub(r"\W", "_", comando)
    return azione


def apri_aiuto():
    import webview
    webview.create_window("Guida di Vertov", f"http://127.0.0.1:{PORTA}/aiuto", width=820, height=820,
                          background_color="#f4ede0", text_select=True)


def apri_registro():
    log = Path.home() / ".cache" / "descrittore" / "vertov.log"
    subprocess.run(["open", "-a", "Console", str(log)] if log.exists() else ["open", str(log.parent)])


def crea_menu() -> list:
    from webview.menu import Menu, MenuAction, MenuSeparator
    return [
        Menu("File", [
            MenuAction("Nuova sessione", _js("nuova")),
            MenuAction("Apri sessione…", _js("apri")),
            MenuAction("Salva sessione", _js("salva")),
            MenuAction("Elimina sessione…", _js("elimina")),
            MenuSeparator(),
            MenuAction("Aggiungi materiali…", _js("aggiungi-file")),
            MenuAction("Aggiungi una cartella…", _js("aggiungi-cartella")),
            MenuSeparator(),
            MenuAction("Apri il report", _js("report-apri")),
            MenuAction("Esporta…", _js("esporta")),
            MenuAction("Mostra i file di lavoro", _js("lavoro")),
            MenuSeparator(),
            MenuAction("Libera spazio…", _js("pulizia")),
        ]),
        Menu("Vista", [
            MenuAction("Materiali", _js("vista-materiali")),
            MenuAction("Risultato", _js("vista-risultato")),
            MenuSeparator(),
            MenuAction("Controlla i volti…", _js("volti")),
            MenuAction("Dettagli dell'elaborazione", _js("dettagli")),
        ]),
        Menu("Analisi", [
            MenuAction("Descrivi il film", _js("descrivi")),
            MenuAction("Ferma", _js("ferma")),
            MenuSeparator(),
            MenuAction("Controlla i volti…", _js("volti")),
        ]),
        Menu("Aiuto", [
            MenuAction("Guida di Vertov", apri_aiuto),
            MenuAction("Controlla aggiornamenti…", _js("aggiornamenti")),
            MenuAction("Apri il registro di Vertov", apri_registro),
        ]),
    ]


# Scorciatoie da tastiera dei menu: titolo -> (tasto, maiuscole)
SCORCIATOIE = {
    "Nuova sessione": ("n", False), "Apri sessione…": ("o", False), "Salva sessione": ("s", False),
    "Aggiungi materiali…": ("o", True), "Esporta…": ("e", False), "Materiali": ("1", False),
    "Risultato": ("2", False), "Controlla i volti…": ("3", False), "Descrivi il film": ("r", False),
    "Ferma": (".", False), "Guida di Vertov": ("?", False),
}


def sistema_menu() -> None:
    """Completa la barra dei menu con le parti native: Modifica, Finestra, scorciatoie, ricerca nell'Aiuto."""
    import AppKit
    app = AppKit.NSApplication.sharedApplication()
    barra = app.mainMenu()
    if barra is None:
        return

    def menu_nativo(titolo, voci):
        menu = AppKit.NSMenu.alloc().initWithTitle_(titolo)
        for voce in voci:
            if voce is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            nome, azione, tasto, *mod = voce
            item = menu.addItemWithTitle_action_keyEquivalent_(nome, azione, tasto)
            if mod:
                item.setKeyEquivalentModifierMask_(mod[0])
        contenitore = AppKit.NSMenuItem.alloc().init()
        contenitore.setTitle_(titolo)
        contenitore.setSubmenu_(menu)
        return contenitore, menu

    cmd, maiusc, ctrl = AppKit.NSCommandKeyMask, AppKit.NSShiftKeyMask, AppKit.NSControlKeyMask
    titoli = [barra.itemAtIndex_(i).title() for i in range(barra.numberOfItems())]
    if "Modifica" not in titoli:
        voce, _ = menu_nativo("Modifica", [
            ("Annulla", "undo:", "z"), ("Ripeti", "redo:", "z", cmd | maiusc), None,
            ("Taglia", "cut:", "x"), ("Copia", "copy:", "c"), ("Incolla", "paste:", "v"),
            ("Seleziona tutto", "selectAll:", "a")])
        barra.insertItem_atIndex_(voce, min(2, barra.numberOfItems()))
    if "Finestra" not in titoli:
        voce, menu = menu_nativo("Finestra", [
            ("Contrai", "performMiniaturize:", "m"), ("Ridimensiona", "performZoom:", ""), None,
            ("Attiva modalità a tutto schermo", "toggleFullScreen:", "f", cmd | ctrl), None,
            ("Porta tutto in primo piano", "arrangeInFront:", "")])
        indice_aiuto = next((i for i in range(barra.numberOfItems()) if barra.itemAtIndex_(i).title() == "Aiuto"),
                            barra.numberOfItems())
        barra.insertItem_atIndex_(voce, indice_aiuto)
        app.setWindowsMenu_(menu)
    # «Controlla aggiornamenti…» va nel menu Vertov, sotto «Informazioni su», come nelle altre app.
    for i in range(barra.numberOfItems()):
        sotto = barra.itemAtIndex_(i).submenu()
        voce = sotto.itemWithTitle_("Controlla aggiornamenti…") if sotto is not None and i > 0 else None
        if voce is not None and barra.itemAtIndex_(0).submenu() is not None:
            sotto.removeItem_(voce)
            barra.itemAtIndex_(0).submenu().insertItem_atIndex_(voce, 1)
    for i in range(barra.numberOfItems()):
        sotto = barra.itemAtIndex_(i).submenu()
        if sotto is None:
            continue
        if barra.itemAtIndex_(i).title() == "Aiuto":
            app.setHelpMenu_(sotto)
        visti = set()
        for j in range(sotto.numberOfItems()):
            item = sotto.itemAtIndex_(j)
            titolo = item.title()
            if titolo in SCORCIATOIE and titolo not in visti:
                tasto, con_maiusc = SCORCIATOIE[titolo]
                item.setKeyEquivalent_(tasto)
                item.setKeyEquivalentModifierMask_(cmd | (maiusc if con_maiusc else 0))
                visti.add(titolo)


def main():
    global FINESTRA
    import webview
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORTA), Gestore)
    except OSError:
        # Vertov è già aperto: si porta in primo piano la sua finestra.
        subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of '
                        '(first process whose name is "Vertov" or name is "Python") to true'],
                       capture_output=True)
        return
    trasloca_tutte()  # i file di lavoro lasciati dalle versioni precedenti nelle cartelle dei risultati
    threading.Thread(target=server.serve_forever, daemon=True).start()
    identita_mac()
    FINESTRA = webview.create_window(
        "Vertov", f"http://127.0.0.1:{PORTA}", width=1180, height=880, min_size=(720, 560),
        background_color="#f4ede0", confirm_close=True, text_select=True)
    # Chiudendo la finestra si ferma un'eventuale elaborazione: si potrà riprendere.
    FINESTRA.events.closed += LAVORO.ferma
    def all_avvio():
        identita_mac()
        collega_trascinamento(FINESTRA)
        from PyObjCTools import AppHelper
        AppHelper.callAfter(sistema_menu)

    webview.settings["SHOW_DEFAULT_MENUS"] = False
    webview.start(all_avvio, menu=crea_menu(), localization={
        "cocoa.menu.about": "Informazioni su", "cocoa.menu.services": "Servizi", "cocoa.menu.hide": "Nascondi",
        "cocoa.menu.hideOthers": "Nascondi altre", "cocoa.menu.showAll": "Mostra tutte", "cocoa.menu.quit": "Esci da",
        "global.quitConfirmation": "Chiudere Vertov? Se c'è un'elaborazione in corso verrà "
                                   "fermata: potrai riprenderla dal punto in cui si è interrotta.",
        "global.ok": "Chiudi", "global.cancel": "Annulla", "global.quit": "Esci da Vertov",
    })
    LAVORO.ferma()
    server.shutdown()


if __name__ == "__main__":
    main()
