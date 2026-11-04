"""Il modello di visione di Vertov e l'unico modo in cui Vertov gli parla.

Descrizioni (descrivi.py), nomi dei volti (volti.py) e sinossi del report (report.py)
passano tutti da chiedi(): stessa gestione degli errori, stessa pulizia della risposta,
stesso controllo dei campi.

- Formato: i modelli GGUF di Ollama accettano lo schema JSON ("format"); quelli che girano
  sul motore MLX (come gemma4:26b) lo rifiutano con l'errore 501 "structured output is
  unavailable". Per loro lo schema va nel testo della richiesta e la risposta si controlla qui.
- Errori: qualunque errore di rete (Ollama che si riavvia, connessione chiusa, tempo scaduto)
  o una risposta non valida vengono ritentati; dopo l'ultimo tentativo si solleva ErroreModello,
  che chi chiama decide come gestire (saltare l'inquadratura, lasciare il volto senza nome...).
- Contesto: se la richiesta riempie quasi tutto il contesto, il modello potrebbe perdere
  l'inizio delle istruzioni; lo si segnala tra le cose da verificare.
"""

import json
import re
import time
import urllib.error
import urllib.request

import coordinamento
import progresso

OLLAMA = "http://localhost:11434"
# Scelto nel confronto del 4 novembre 2026 (10 inquadrature con dialoghi, giudicate sui fotogrammi):
# il più preciso su azioni e battute e quasi tre volte più veloce di qwen3-vl. Lo usano anche Ozu e Murnau.
MODELLO = "gemma4:26b"

_FORMATI = {}            # modello -> "gguf" | "safetensors" (MLX), letto una volta da /api/show
_CONTESTO_SEGNALATO = set()


class ErroreModello(RuntimeError):
    """Il modello non ha dato una risposta utilizzabile, nemmeno ritentando."""


def _formato(modello: str) -> str:
    if modello not in _FORMATI:
        try:
            corpo = json.dumps({"model": modello}).encode()
            with urllib.request.urlopen(urllib.request.Request(f"{OLLAMA}/api/show", data=corpo), timeout=10) as r:
                _FORMATI[modello] = json.load(r).get("details", {}).get("format", "gguf")
        except (OSError, ValueError):
            return "gguf"  # non lo si sa: si prova con lo schema, e un 501 farà passare all'altro modo
    return _FORMATI[modello]


def _estrai_json(testo: str) -> str:
    """Il JSON dentro la risposta: senza ```json, segni di fine (<|eot|>) o frasi attorno."""
    t = re.sub(r"<\|[^|>]*\|>", "", testo)
    t = re.sub(r"^\s*```(?:json)?|```\s*$", "", t.strip(), flags=re.M).strip()
    inizio, fine = t.find("{"), t.rfind("}")
    return t[inizio:fine + 1] if inizio >= 0 and fine > inizio else t


def _conforma(dati, schema: dict) -> dict:
    """Ogni campo richiesto dallo schema, del tipo giusto. Solleva ValueError se manca qualcosa di essenziale."""
    if isinstance(dati, list) and len(dati) == 1:
        dati = dati[0]
    if isinstance(dati, dict) and "properties" in dati and dati.get("type") == "object":
        raise ValueError("il modello ha riscritto lo schema invece di compilarlo")
    if not isinstance(dati, dict):
        raise ValueError("la risposta non è un oggetto JSON")
    # Alcuni modelli rinominano i campi con spazi o maiuscole (" descrizione", "Descrizione").
    dati = {str(k).strip().lower(): v for k, v in dati.items()}
    # ...o li avvolgono in un oggetto in più ({"inquadratura": {...}}).
    richiesti = set(schema.get("required", []))
    if len(dati) == 1 and isinstance(next(iter(dati.values())), dict) and not richiesti & set(dati):
        dati = {str(k).strip().lower(): v for k, v in next(iter(dati.values())).items()}
    proprieta = schema.get("properties", {})
    risultato = {}
    for campo, tipo in proprieta.items():
        v = dati.get(campo)
        if tipo.get("type") == "array":
            if v is None:
                v = []
            elif isinstance(v, str):
                v = [x.strip() for x in v.split(",") if x.strip()]
            elif not isinstance(v, list):
                v = [v]
            v = [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in v]
        else:
            if isinstance(v, list):  # testo spezzato in un elenco: si ricompone
                v = " ".join(x if isinstance(x, str) else (x.get("testo") or json.dumps(x, ensure_ascii=False))
                             if isinstance(x, dict) else str(x) for x in v)
            elif isinstance(v, dict):
                v = v.get("testo") or json.dumps(v, ensure_ascii=False)
            v = "" if v is None else str(v).strip()
            if "enum" in tipo and v not in tipo["enum"]:
                v = tipo["enum"][-1]  # il valore più prudente (es. sicurezza "bassa")
        risultato[campo] = v
    # Un campo richiesto e vuoto va bene (es. nessun dialogo), un campo richiesto assente no:
    # vuol dire che il modello ha risposto ad altro.
    assenti = [c for c in schema.get("required", []) if c not in dati]
    if len(assenti) > len(schema.get("required", [])) / 2:
        raise ValueError(f"mancano i campi {', '.join(assenti)}")
    return risultato


def chiedi(prompt: str, immagini: list = (), schema: dict = None, modello: str = MODELLO,
           temperatura: float = 0.2, tentativi: int = 3, timeout: int = 600, **opzioni) -> dict:
    """Chiede al modello una risposta JSON conforme allo schema. immagini: byte JPEG già codificati in base64."""
    mlx = _formato(modello) == "safetensors"
    testo = prompt
    if schema and mlx:
        # Un modulo da compilare, non lo schema tecnico: con lo schema i modelli a volte lo riscrivono
        # tale e quale invece di riempirlo.
        modulo = {c: (["…"] if t.get("type") == "array" else ("uno tra: " + ", ".join(t["enum"]) if "enum" in t else "…"))
                  for c, t in schema.get("properties", {}).items()}
        testo += ("\n\nRispondi SOLO con un oggetto JSON valido, senza altro testo prima o dopo, con esattamente "
                  "questi campi compilati con i tuoi valori (non ripetere questo modello):\n"
                  + json.dumps(modulo, ensure_ascii=False))
    corpo = {"model": modello, "stream": False, "think": False, "keep_alive": coordinamento.TENUTA,
             "messages": [{"role": "user", "content": testo, "images": list(immagini)}],
             "options": {"temperature": temperatura, "num_ctx": coordinamento.CONTESTO, **opzioni}}
    if schema and not mlx:
        corpo["format"] = schema
    errore, contenuto = None, ""
    for tentativo in range(1, tentativi + 1):
        try:
            richiesta = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(corpo).encode(),
                                               headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(richiesta, timeout=timeout) as r:
                risposta = json.load(r)
            _controlla_contesto(modello, risposta)
            contenuto = risposta["message"]["content"] or ""
            if not schema:
                return {"testo": contenuto.strip()}
            return _conforma(json.loads(_estrai_json(contenuto)), schema)
        except urllib.error.HTTPError as e:
            if e.code == 501 and "format" in corpo:  # motore MLX: lo schema va nel testo
                _FORMATI[modello] = "safetensors"
                return chiedi(prompt, immagini, schema, modello, temperatura, tentativi, timeout, **opzioni)
            errore = e
        except (OSError, ValueError, KeyError, TypeError) as e:
            # OSError comprende URLError, tempo scaduto, connessione chiusa o rifiutata.
            # Con una risposta non valida si riporta il suo inizio, per capire che cosa ha scritto il modello.
            errore = ValueError(f"{e} (risposta: {contenuto[:200]!r})") if isinstance(e, ValueError) and contenuto else e
        if tentativo < tentativi:
            time.sleep(3 * tentativo)
    raise ErroreModello(f"il modello non ha risposto correttamente: {errore}")


def _controlla_contesto(modello: str, risposta: dict) -> None:
    usati = risposta.get("prompt_eval_count") or 0
    if usati >= 0.95 * coordinamento.CONTESTO and modello not in _CONTESTO_SEGNALATO:
        _CONTESTO_SEGNALATO.add(modello)
        progresso.avviso(f"Alcune richieste al modello riempiono il contesto ({usati} token su "
                         f"{coordinamento.CONTESTO}): l'inizio delle istruzioni potrebbe essere stato ignorato.")
