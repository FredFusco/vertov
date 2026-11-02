"""Controllo e installazione degli aggiornamenti di Vertov e dei suoi componenti.

Si controllano:
- Vertov stesso: un file di annuncio (JSON) pubblicato online, il cui indirizzo è
  in aggiornamenti.json accanto all'app, dice qual è l'ultima versione e dove
  scaricarla. Il pacchetto è uno zip con i file dell'app, verificato con SHA-256;
  i file sostituiti vengono conservati per poter tornare indietro.
- i programmi installati con Homebrew: ffmpeg (video e audio) e Ollama (che fa
  girare il modello di visione);
- le librerie Python dell'ambiente di Vertov (pywebview, OpenCV, mlx-whisper,
  Transformers…): dopo l'aggiornamento si verifica che si carichino tutte, e se
  qualcosa non va si torna alle versioni di prima;
- i modelli di intelligenza artificiale: il modello di visione (registro di Ollama),
  Whisper per la trascrizione e AST per i suoni (Hugging Face).

Annuncio (vertov.json):
    {"versione": "1.1.0", "data": "2026-11-20", "novita": ["…", "…"],
     "pacchetto": "vertov-1.1.0.zip", "sha256": "…"}
"pacchetto" può essere un indirizzo completo o relativo all'annuncio.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

CARTELLA = Path(__file__).resolve().parent
DATI = Path.home() / "Library" / "Application Support" / "Vertov"
STATO = DATI / "aggiornamenti.json"
PRECEDENTI = DATI / "versioni_precedenti"
OLLAMA = "http://localhost:11434"
REGISTRO = "https://registry.ollama.ai/v2"
HF_CACHE = Path.home() / ".cache" / "huggingface" / "hub"
OGNI = 24 * 3600  # controllo automatico al massimo una volta al giorno
# Cartelle dell'app che un aggiornamento non tocca mai.
INTOCCABILI = {"prova_reale", "dist", "__pycache__"}

PROGRAMMI = {"ffmpeg": "legge video e audio, estrae i fotogrammi",
             "ollama": "fa girare il modello di visione sul Mac"}
MODELLI_HF = {"mlx-community/whisper-large-v3-turbo": "Whisper, trascrive i dialoghi",
              "MIT/ast-finetuned-audioset-10-10-0.4593": "AST, riconosce effetti, ambienti e musica"}
# Moduli che devono caricarsi dopo un aggiornamento delle librerie.
MODULI = ["webview", "aaf2", "cv2", "numpy", "scipy", "pypdf", "torch", "transformers", "mlx_whisper"]
GRUPPI = {"app": "Vertov", "programma": "Programmi", "libreria": "Librerie Python", "modello": "Modelli di intelligenza artificiale"}


def versione() -> str:
    try:
        return (CARTELLA / "VERSIONE").read_text().strip()
    except OSError:
        return "0"


def _numeri(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))


def indirizzo_annuncio() -> str:
    try:
        return json.loads((CARTELLA / "aggiornamenti.json").read_text()).get("indirizzo", "").strip()
    except (OSError, ValueError):
        return ""


def _sicuro(indirizzo: str) -> bool:
    u = urlparse(indirizzo)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))


def leggi_stato() -> dict:
    try:
        return {"automatico": True, "saltati": {}, **json.loads(STATO.read_text())}
    except (OSError, ValueError):
        return {"automatico": True, "saltati": {}}


def scrivi_stato(**valori) -> dict:
    stato = {**leggi_stato(), **valori}
    DATI.mkdir(parents=True, exist_ok=True)
    STATO.write_text(json.dumps(stato, ensure_ascii=False, indent=2))
    return stato


def _scarica_json(indirizzo: str, intestazioni: dict = None):
    richiesta = urllib.request.Request(indirizzo, headers={"User-Agent": f"Vertov/{versione()}", **(intestazioni or {})})
    with urllib.request.urlopen(richiesta, timeout=15) as r:
        corpo = r.read()
        return json.loads(corpo), corpo


def brew() -> str:
    return shutil.which("brew") or "/opt/homebrew/bin/brew"


def _messaggio(e: Exception) -> str:
    if isinstance(e, urllib.error.URLError):
        return "non riesco a collegarmi (sei online?)"
    if isinstance(e, subprocess.TimeoutExpired):
        return "non ha risposto in tempo"
    return str(e)


# --------------------------------------------------------------------------
# Controllo: ogni aggiornamento trovato è una "voce"
#   {chiave, gruppo, nome, descrizione, attuale, nuova, impronta, …}
# "impronta" identifica la versione nuova, per poterla saltare.
# --------------------------------------------------------------------------

def controlla_app() -> tuple:
    """(configurato, voce o None)."""
    indirizzo = indirizzo_annuncio()
    if not indirizzo:
        return False, None
    if not _sicuro(indirizzo):
        raise RuntimeError("l'indirizzo degli aggiornamenti deve essere https")
    annuncio, _ = _scarica_json(indirizzo)
    nuova = str(annuncio.get("versione", ""))
    if not nuova or _numeri(nuova) <= _numeri(versione()):
        return True, None
    return True, {"chiave": "app", "gruppo": "app", "nome": "Vertov", "descrizione": "l'app",
                  "attuale": versione(), "nuova": nuova, "impronta": nuova, "data": annuncio.get("data", ""),
                  "novita": annuncio.get("novita") or [],
                  "pacchetto": urljoin(indirizzo, annuncio["pacchetto"]), "sha256": annuncio["sha256"].lower()}


def controlla_programmi() -> list:
    subprocess.run([brew(), "update", "--quiet"], capture_output=True, timeout=180)
    r = subprocess.run([brew(), "outdated", "--json=v2", *PROGRAMMI], capture_output=True, text=True, timeout=60)
    voci = []
    for f in json.loads(r.stdout or "{}").get("formulae", []):
        voci.append({"chiave": "programma:" + f["name"], "gruppo": "programma", "nome": f["name"],
                     "descrizione": PROGRAMMI.get(f["name"], ""), "attuale": f["installed_versions"][-1],
                     "nuova": f["current_version"], "impronta": f["current_version"]})
    return voci


def controlla_librerie() -> list:
    # Solo le librerie installate per Vertov (non quelle che si portano dietro): le altre seguono da sé.
    r = subprocess.run([sys.executable, "-m", "pip", "list", "--outdated", "--not-required", "--format=json",
                        "--disable-pip-version-check"], capture_output=True, text=True, timeout=180)
    return [{"chiave": "libreria:" + p["name"], "gruppo": "libreria", "nome": p["name"], "descrizione": "",
             "attuale": p["version"], "nuova": p["latest_version"], "impronta": p["latest_version"]}
            for p in json.loads(r.stdout or "[]") if p["name"] != "pip"]


def controlla_modello_visione(modello: str) -> list:
    """Confronta l'impronta del modello scaricato con quella del registro di Ollama."""
    with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=3) as r:
        locali = {m["name"]: m.get("digest", "") for m in json.load(r).get("models", [])}
    if modello not in locali:
        return []
    nome, _, etichetta = modello.partition(":")
    percorso = nome if "/" in nome else f"library/{nome}"
    _, corpo = _scarica_json(f"{REGISTRO}/{percorso}/manifests/{etichetta or 'latest'}",
                             {"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    remota = hashlib.sha256(corpo).hexdigest()
    if remota == locali[modello]:
        return []
    return [{"chiave": "ollama:" + modello, "gruppo": "modello", "nome": modello,
             "descrizione": "il modello di visione che descrive le immagini (download di circa 20 GB)",
             "attuale": locali[modello][:12], "nuova": remota[:12], "impronta": remota}]


def controlla_modelli_hf() -> list:
    voci = []
    for repo, descrizione in MODELLI_HF.items():
        ref = HF_CACHE / ("models--" + repo.replace("/", "--")) / "refs" / "main"
        if not ref.exists():
            continue  # non ancora scaricato: lo scarica Vertov la prima volta che serve
        locale = ref.read_text().strip()
        remoto, _ = _scarica_json(f"https://huggingface.co/api/models/{repo}")
        if remoto.get("sha") and remoto["sha"] != locale:
            voci.append({"chiave": "hf:" + repo, "gruppo": "modello", "nome": repo.split("/")[-1],
                         "descrizione": descrizione, "attuale": locale[:12], "nuova": remoto["sha"][:12],
                         "impronta": remoto["sha"], "repo": repo})
    return voci


def controlla(modello: str, manuale: bool = False) -> dict:
    """Cerca aggiornamenti di tutto. Se automatico, non ripropone quelli saltati."""
    stato = leggi_stato()
    esito = {"versione": versione(), "voci": [], "errori": [], "configurato": True,
             "automatico": stato.get("automatico", True)}
    try:
        esito["configurato"], voce = controlla_app()
        esito["voci"] += [voce] if voce else []
    except Exception as e:
        esito["errori"].append(f"Vertov: {_messaggio(e)}")
    for nome, funzione in [("programmi", controlla_programmi), ("librerie", controlla_librerie),
                           ("modello di visione", lambda: controlla_modello_visione(modello)),
                           ("Whisper e AST", controlla_modelli_hf)]:
        try:
            esito["voci"] += funzione()
        except Exception as e:
            esito["errori"].append(f"{nome}: {_messaggio(e)}")
    scrivi_stato(ultimo_controllo=time.time())
    if not manuale:
        saltati = stato.get("saltati", {})
        esito["voci"] = [v for v in esito["voci"] if saltati.get(v["chiave"]) != v["impronta"]]
    return esito


def salta(voci: list) -> None:
    saltati = leggi_stato().get("saltati", {})
    saltati.update({v["chiave"]: v["impronta"] for v in voci})
    scrivi_stato(saltati=saltati)


def da_controllare() -> bool:
    stato = leggi_stato()
    return stato.get("automatico", True) and time.time() - stato.get("ultimo_controllo", 0) > OGNI


# --------------------------------------------------------------------------
# Installazione (in un thread; l'interfaccia legge INSTALLAZIONE)
# --------------------------------------------------------------------------

INSTALLAZIONE = {"attiva": False, "fase": "", "fatti": 0, "totale": 0, "errore": "", "riavvia": False,
                 "fatto": False, "riusciti": [], "falliti": []}


def _fase(fase: str, fatti: int = 0, totale: int = 0) -> None:
    INSTALLAZIONE.update(fase=fase, fatti=fatti, totale=totale)


def _installa_app(voce: dict) -> None:
    _fase("Scarico Vertov " + voce["nuova"] + " (KB)")
    if not _sicuro(voce["pacchetto"]):
        raise RuntimeError("Il pacchetto deve essere scaricato via https.")
    with tempfile.TemporaryDirectory() as tmp:
        zip_ = Path(tmp) / "vertov.zip"
        impronta = hashlib.sha256()
        richiesta = urllib.request.Request(voce["pacchetto"], headers={"User-Agent": f"Vertov/{versione()}"})
        with urllib.request.urlopen(richiesta, timeout=60) as r, open(zip_, "wb") as f:
            totale = int(r.headers.get("Content-Length") or 0)
            while blocco := r.read(1 << 16):
                f.write(blocco)
                impronta.update(blocco)
                _fase(INSTALLAZIONE["fase"], f.tell() // 1024, totale // 1024)
        if impronta.hexdigest() != voce["sha256"]:
            raise RuntimeError("Il pacchetto scaricato non corrisponde all'impronta annunciata: non lo installo.")

        _fase("Controllo il pacchetto")
        estratti = Path(tmp) / "nuova"
        with zipfile.ZipFile(zip_) as z:
            for nome in z.namelist():
                destinazione = (estratti / nome).resolve()
                if estratti.resolve() not in destinazione.parents and destinazione != estratti.resolve():
                    raise RuntimeError("Il pacchetto contiene percorsi non validi.")
            z.extractall(estratti)
            # extractall non conserva i permessi: senza, il lanciatore di Vertov.app non sarebbe più eseguibile.
            for info in z.infolist():
                permessi = info.external_attr >> 16 & 0o777
                if permessi and not info.is_dir():
                    (estratti / info.filename).chmod(permessi)
        radice = estratti
        voci = [p for p in estratti.iterdir() if p.name != "__MACOSX"]
        if len(voci) == 1 and voci[0].is_dir():
            radice = voci[0]
        if not (radice / "app.py").exists() or not (radice / "descrivi.py").exists():
            raise RuntimeError("Il pacchetto non contiene Vertov.")

        _fase("Installo Vertov " + voce["nuova"])
        copia = PRECEDENTI / versione()
        for sorgente in sorted(radice.rglob("*")):
            rel = sorgente.relative_to(radice)
            if rel.parts[0] in INTOCCABILI or sorgente.name == ".DS_Store" or sorgente.is_dir():
                continue
            bersaglio = CARTELLA / rel
            if bersaglio.exists():
                (copia / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bersaglio, copia / rel)
            bersaglio.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sorgente, bersaglio)
    INSTALLAZIONE["riavvia"] = True


def _installa_programmi(nomi: list) -> None:
    _fase("Aggiorno " + ", ".join(nomi) + " con Homebrew")
    r = subprocess.run([brew(), "upgrade", *nomi], capture_output=True, text=True, timeout=3600)
    if r.returncode:
        raise RuntimeError("Homebrew: " + (r.stderr.strip().splitlines() or ["errore"])[-1])
    if "ollama" in nomi:
        # Ollama gira in sottofondo: va riavviato perché usi la versione nuova.
        _fase("Riavvio Ollama")
        servizi = subprocess.run([brew(), "services", "list"], capture_output=True, text=True).stdout
        if any(r.split()[:1] == ["ollama"] for r in servizi.splitlines()):
            subprocess.run([brew(), "services", "restart", "ollama"], capture_output=True)
        for _ in range(30):
            time.sleep(1)
            try:
                urllib.request.urlopen(f"{OLLAMA}/api/version", timeout=2)
                break
            except Exception:
                pass


def _installa_librerie(nomi: list) -> None:
    pip = [sys.executable, "-m", "pip", "--disable-pip-version-check"]
    prima = subprocess.run([*pip, "freeze"], capture_output=True, text=True).stdout
    _fase("Aggiorno le librerie Python")
    r = subprocess.run([*pip, "install", "-q", "-U", *nomi], capture_output=True, text=True, timeout=3600)
    prova = subprocess.run([sys.executable, "-c", "import " + ", ".join(MODULI)], capture_output=True, text=True,
                           timeout=600)
    if r.returncode or prova.returncode:
        _fase("Qualcosa non va: rimetto le librerie di prima")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(prima)
        subprocess.run([*pip, "install", "-q", "-r", f.name], capture_output=True, timeout=3600)
        Path(f.name).unlink(missing_ok=True)
        dettaglio = (r.stderr or prova.stderr).strip().splitlines()
        raise RuntimeError("le librerie nuove non funzionano con Vertov, ho rimesso quelle di prima"
                           + (f" ({dettaglio[-1]})" if dettaglio else ""))


def _installa_modello_visione(modello: str) -> None:
    _fase("Aggiorno il modello di visione (MB)")
    corpo = json.dumps({"model": modello, "stream": True}).encode()
    richiesta = urllib.request.Request(f"{OLLAMA}/api/pull", data=corpo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(richiesta, timeout=None) as r:
        for riga in r:
            stato = json.loads(riga or b"{}")
            if stato.get("error"):
                raise RuntimeError(f"download non riuscito: {stato['error']}")
            if stato.get("total") and stato.get("completed") is not None:
                _fase(INSTALLAZIONE["fase"], stato["completed"] // 2**20, stato["total"] // 2**20)


def _installa_modello_hf(repo: str) -> None:
    _fase(f"Aggiorno {repo.split('/')[-1]}")
    # Online di proposito: durante il lavoro audio.py tiene Hugging Face fuori linea (HF_HUB_OFFLINE).
    r = subprocess.run([sys.executable, "-c", "import sys; from huggingface_hub import snapshot_download; "
                        "snapshot_download(sys.argv[1])", repo], capture_output=True, text=True, timeout=3600,
                       env={**os.environ, "HF_HUB_OFFLINE": "0", "HF_HUB_DISABLE_TELEMETRY": "1"})
    if r.returncode:
        raise RuntimeError("download non riuscito: " + (r.stderr.strip().splitlines() or ["errore"])[-1])


def installa(voci: list, modello: str) -> None:
    if INSTALLAZIONE["attiva"]:
        raise RuntimeError("Un aggiornamento è già in corso.")
    INSTALLAZIONE.update(attiva=True, errore="", riavvia=False, fatto=False, fase="", fatti=0, totale=0,
                         riusciti=[], falliti=[])
    per_gruppo = {g: [v for v in voci if v["gruppo"] == g] for g in GRUPPI}

    # Prima i componenti, per ultima l'app (che chiede di riavviare). Un passo fallito non ferma gli altri.
    passi = []
    if per_gruppo["libreria"]:
        passi.append(("Librerie Python", lambda: _installa_librerie([v["nome"] for v in per_gruppo["libreria"]])))
    if per_gruppo["programma"]:
        passi.append((", ".join(v["nome"] for v in per_gruppo["programma"]),
                      lambda: _installa_programmi([v["nome"] for v in per_gruppo["programma"]])))
    for v in per_gruppo["modello"]:
        if v["chiave"].startswith("ollama:"):
            passi.append((v["nome"], lambda: _installa_modello_visione(modello)))
        else:
            passi.append((v["nome"], lambda repo=v["repo"]: _installa_modello_hf(repo)))
    for v in per_gruppo["app"]:
        passi.append((f"Vertov {v['nuova']}", lambda v=v: _installa_app(v)))

    def lavoro():
        try:
            for nome, passo in passi:
                try:
                    passo()
                    INSTALLAZIONE["riusciti"].append(nome)
                except Exception as e:
                    INSTALLAZIONE["falliti"].append(f"{nome}: {_messaggio(e)}")
            fatti = {v["chiave"] for v in voci}
            scrivi_stato(saltati={k: i for k, i in leggi_stato().get("saltati", {}).items() if k not in fatti})
            INSTALLAZIONE["errore"] = "; ".join(INSTALLAZIONE["falliti"])
            INSTALLAZIONE["fatto"] = True
        finally:
            INSTALLAZIONE["attiva"] = False

    threading.Thread(target=lavoro, daemon=True).start()


def riavvia(porta: int) -> None:
    """Riapre Vertov appena questa istanza ha liberato la porta del server."""
    bundle = CARTELLA / "Vertov.app"
    apri = ["open", str(bundle)] if bundle.exists() else [sys.executable, str(CARTELLA / "app.py")]
    script = (f"for i in $(seq 1 60); do nc -z 127.0.0.1 {porta} || break; sleep 0.5; done; "
              + " ".join(f"'{a}'" for a in apri))
    subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
