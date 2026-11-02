"""Riconoscimento dei personaggi dai volti, usando solo il film stesso.

1. In ogni fotogramma si trovano i volti (YuNet) e se ne calcola l'impronta
   (SFace). Entrambi i modelli sono di OpenCV, gratuiti e locali.
2. I volti della stessa persona vengono raggruppati.
3. A ogni gruppo viene proposto un nome usando chi parla nelle battute
   abbinate al copione e chi è convocato nella scena secondo gli ODG.
4. Una pagina HTML mostra i gruppi; i nomi si confermano o correggono nel
   file volti/nomi.txt, che viene riletto alle esecuzioni successive.
"""

import html
import json
import os
import urllib.request
from collections import defaultdict
from pathlib import Path

import coordinamento
import progresso

CARTELLA_MODELLI = Path(os.path.expanduser("~/.cache/descrittore/modelli"))
URL_MODELLI = {
    "yunet.onnx": "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
                  "face_detection_yunet_2023mar.onnx",
    "sface.onnx": "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
                  "face_recognition_sface_2021dec.onnx",
}
ALTEZZA_MINIMA = 40        # pixel: volti più piccoli sono comparse lontane
SOGLIA_STESSA_PERSONA = 0.45   # somiglianza minima (coseno) per riconoscere un gruppo già noto
DISTANZA_GRUPPI = 0.58         # raggruppamento gerarchico: distanza media massima nel gruppo
FRONTALITA_MINIMA = 0.28       # distanza tra gli occhi / larghezza del volto: sotto è di profilo
MINIMO_VOLTI_GRUPPO = 3


def _modelli():
    CARTELLA_MODELLI.mkdir(parents=True, exist_ok=True)
    for nome, url in URL_MODELLI.items():
        dest = CARTELLA_MODELLI / nome
        if not dest.exists():
            print(f"Scarico il modello {nome}...")
            urllib.request.urlretrieve(url, dest)
    return CARTELLA_MODELLI / "yunet.onnx", CARTELLA_MODELLI / "sface.onnx"


class Riconoscitore:
    def __init__(self):
        import cv2
        self.cv2 = cv2
        rilevatore, riconoscitore = _modelli()
        self.rilevatore = cv2.FaceDetectorYN.create(str(rilevatore), "", (320, 320), 0.8)
        self.riconoscitore = cv2.FaceRecognizerSF.create(str(riconoscitore), "")

    def volti(self, percorso: Path) -> list:
        import numpy as np
        img = self.cv2.imread(str(percorso))
        if img is None:
            return []
        h, w = img.shape[:2]
        self.rilevatore.setInputSize((w, h))
        _, trovati = self.rilevatore.detect(img)
        risultato = []
        for f in (trovati if trovati is not None else []):
            x, y, bw, bh = (float(v) for v in f[:4])
            if bh < ALTEZZA_MINIMA:
                continue
            # I volti di profilo danno impronte poco affidabili e confondono i gruppi.
            occhi = float(np.hypot(f[4] - f[6], f[5] - f[7]))
            if occhi / bw < FRONTALITA_MINIMA:
                continue
            allineato = self.riconoscitore.alignCrop(img, f)
            imp = self.riconoscitore.feature(allineato).flatten()
            imp = imp / (np.linalg.norm(imp) + 1e-9)
            x0, y0 = max(int(x - bw * 0.25), 0), max(int(y - bh * 0.25), 0)
            ritaglio = img[y0:int(y + bh * 1.25), x0:int(x + bw * 1.25)]
            risultato.append({"impronta": imp, "x": (x + bw / 2) / w, "altezza": bh / h,
                              "ritaglio": ritaglio})
        return risultato


def _raggruppa(volti: list, centri_noti: dict) -> None:
    """Assegna a ogni volto un gruppo (campo "gruppo").

    I volti molto simili a un gruppo già salvato (e magari già nominato)
    vanno in quel gruppo; gli altri vengono raggruppati tra loro con un
    raggruppamento gerarchico a legame medio, più robusto di un'assegnazione
    un volto alla volta.
    """
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage
    restanti = []
    for v in volti:
        migliore, sim = None, -1.0
        for g, c in centri_noti.items():
            s = float(v["impronta"] @ np.asarray(c))
            if s > sim:
                migliore, sim = g, s
        if migliore is not None and sim >= SOGLIA_STESSA_PERSONA:
            v["gruppo"] = migliore
        else:
            restanti.append(v)
    prossimo = max([int(g[1:]) for g in centri_noti] + [0]) + 1
    if len(restanti) == 1:
        restanti[0]["gruppo"] = f"G{prossimo:02d}"
    elif restanti:
        matrice = np.stack([v["impronta"] for v in restanti])
        etichette = fcluster(linkage(matrice, method="average", metric="cosine"),
                             t=DISTANZA_GRUPPI, criterion="distance")
        # Numeri di gruppo in ordine di grandezza: G01 è la persona più presente.
        ordine = sorted(set(etichette), key=lambda e: -int((etichette == e).sum()))
        nuovo = {e: f"G{prossimo + i:02d}" for i, e in enumerate(ordine)}
        for v, e in zip(restanti, etichette):
            v["gruppo"] = nuovo[e]


def _candidati(g: str, per_inquadratura: dict, inquadrature: list, presenze: dict,
               schede: dict) -> list:
    """Personaggi che potrebbero essere il gruppo g: i convocati delle sue scene."""
    nomi = set()
    for q in inquadrature:
        if g in per_inquadratura.get(q.numero, {}) and q.scena:
            convocati = presenze.get(q.scena.numero, {}).get("in_campo", set())
            if convocati:
                nomi |= convocati
            else:
                testo = q.scena.testo.upper()
                nomi |= {n for n in schede if n.upper() in testo}
    return sorted(nomi)


def _mosaico(ritagli: list, altezza: int = 200):
    import cv2
    import numpy as np
    foto = [cv2.resize(r, (max(1, int(r.shape[1] * altezza / r.shape[0])), altezza))
            for r in ritagli[:6] if r.size]
    if not foto:
        return None
    ok, jpg = cv2.imencode(".jpg", np.hstack(foto), [cv2.IMWRITE_JPEG_QUALITY, 90])
    return jpg.tobytes() if ok else None


def _nomi_dal_modello(gruppi: dict, candidati: dict, schede: dict, modello: str,
                      riferimenti: dict = None) -> dict:
    """Chiede al modello di visione a quale personaggio corrisponde ogni gruppo di volti.

    gruppi: {gruppo: [ritagli]}, dal più numeroso. riferimenti: {nome: [ritagli]}
    di personaggi già riconosciuti con certezza, mostrati come confronto; ogni
    riconoscimento sicuro diventa a sua volta un riferimento per i gruppi successivi.
    Restituisce {gruppo: (nome, sicurezza)}.
    """
    import base64
    schema = {"type": "object", "properties": {
        "nome": {"type": "string"}, "sicurezza": {"type": "string", "enum": ["alta", "media", "bassa"]},
        "motivo": {"type": "string"}}, "required": ["nome", "sicurezza", "motivo"]}
    riferimenti = dict(riferimenti or {})
    risultato = {}
    progresso.fase("confronto_volti")
    for n, (g, ritagli) in enumerate(gruppi.items(), 1):
        progresso.avanzamento("confronto_volti", n, len(gruppi))
        nomi = candidati.get(g, [])
        principale = _mosaico(ritagli)
        if not nomi or principale is None:
            continue
        immagini = [principale]
        righe_rif = []
        for n in nomi:
            if n in riferimenti:
                rif = _mosaico(riferimenti[n], 160)
                if rif is not None:
                    immagini.append(rif)
                    righe_rif.append(f"- immagine {len(immagini)}: volti sicuramente di {n}")
        elenco = "\n".join(f"- {n}: {schede.get(n, 'nessuna descrizione disponibile')}" for n in nomi)
        prompt = (
            "La PRIMA immagine contiene alcuni ritagli del volto di UNA STESSA persona, presi da "
            "inquadrature diverse di un film (le luci colorate possono cambiare i colori).\n"
            + ("Le immagini successive sono volti di riferimento di personaggi già riconosciuti:\n"
               + "\n".join(righe_rif) + "\n" if righe_rif else "")
            + f"Personaggi principali convocati in queste scene, con la descrizione dalla "
            f"sceneggiatura:\n{elenco}\n\n"
            "Nelle scene ci sono anche COMPARSE che non compaiono in questo elenco. "
            "Chi è la persona della prima immagine? Confronta genere, età apparente, capelli, "
            "occhiali e lineamenti con le descrizioni e con i volti di riferimento. Rispondi con "
            "il nome di un personaggio solo se corrisponde chiaramente; se è un'altra persona "
            "rispondi \"comparsa\", se non puoi deciderlo \"sconosciuto\". Rispondi in italiano.")
        corpo = {"model": modello, "stream": False, "think": False, "format": schema,
                 "keep_alive": coordinamento.TENUTA,
                 "options": {"temperature": 0.0, "num_ctx": coordinamento.CONTESTO},
                 "messages": [{"role": "user", "content": prompt,
                               "images": [base64.b64encode(i).decode() for i in immagini]}]}
        try:
            req = urllib.request.Request("http://localhost:11434/api/chat",
                                         data=json.dumps(corpo).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as r:
                risposta = json.loads(json.load(r)["message"]["content"])
        except Exception as e:  # il riconoscimento dei volti non deve fermare il lavoro
            print(f"  {g}: il modello non ha risposto ({e})")
            continue
        nome = next((n for n in nomi if n.lower() == risposta["nome"].strip().lower()), None)
        if nome:
            risultato[g] = (nome, risposta["sicurezza"])
            if risposta["sicurezza"] == "alta" and nome not in riferimenti:
                riferimenti[nome] = ritagli
    return risultato


def _proponi_nomi(per_inquadratura: dict, inquadrature: list, presenze: dict,
                  centri: dict = None, fissi: dict = None, visivi: dict = None) -> dict:
    """Nome più probabile per ogni gruppo.

    In ordine di affidabilità: i nomi confermati dall'utente (fissi), il
    confronto visivo dei volti con le schede dei personaggi (visivi), le
    deduzioni dagli ODG (unico convocato in scena, esclusione) e infine la
    somiglianza con gruppi già nominati.

    Chi parla NON viene usato come prova: nei controcampi e negli over the
    shoulder il volto ben visibile è spesso quello di chi ascolta.
    """
    voti = defaultdict(lambda: defaultdict(float))
    # Gruppi visti nello stesso fotogramma sono certamente persone diverse.
    # (Nella stessa inquadratura no: una persona può finire in due gruppi se la
    # luce cambia tra un fotogramma e l'altro.)
    insieme = defaultdict(set)
    for gruppi in per_inquadratura.values():
        per_fotogramma = defaultdict(set)
        for g, vv in gruppi.items():
            for v in vv:
                per_fotogramma[v["fotogramma"]].add(g)
        for presenti in per_fotogramma.values():
            for g in presenti:
                insieme[g] |= presenti - {g}
    for q in inquadrature:
        gruppi = per_inquadratura.get(q.numero, {})
        if not gruppi:
            continue
        numero_scena = q.scena.numero if q.scena else ""
        convocati = presenze.get(numero_scena, {}).get("in_campo", set())
        # Un solo convocato in scena e un solo volto: quasi certamente è lui.
        if len(convocati) == 1 and len(gruppi) == 1:
            voti[next(iter(gruppi))][next(iter(convocati))] += 2.0
    proposte = {g: n for g, n in (fissi or {}).items() if n}

    def assegna(voti_correnti):
        candidati = sorted(((p, g, n) for g, vn in voti_correnti.items() for n, p in vn.items()),
                           reverse=True)
        for punti, g, nome in candidati:
            if g in proposte or punti < 1.0:
                continue
            # Il nome deve prevalere chiaramente sugli altri candidati per lo stesso gruppo.
            secondo = max((p for n, p in voti_correnti[g].items() if n != nome), default=0.0)
            if punti < 1.5 * secondo:
                continue
            if any(proposte.get(altro) == nome for altro in insieme[g]):
                continue
            proposte[g] = nome

    # Prima il confronto visivo, dai gruppi più numerosi (impronte più affidabili).
    for g, (nome, sicurezza) in sorted((visivi or {}).items(),
                                       key=lambda x: -sum(len(v.get(x[0], [])) for v in per_inquadratura.values())):
        if g in proposte or sicurezza != "alta":
            continue
        if any(proposte.get(altro) == nome for altro in insieme[g]):
            continue
        proposte[g] = nome
    assegna(voti)
    # Esclusione: se in un'inquadratura gli altri volti hanno già un nome e tra i
    # convocati della scena resta una sola persona, l'ultimo volto è quella persona.
    for _ in range(2):
        extra = defaultdict(lambda: defaultdict(float))
        for q in inquadrature:
            gruppi = set(per_inquadratura.get(q.numero, {}))
            convocati = presenze.get(q.scena.numero if q.scena else "", {}).get("in_campo", set())
            senza = [g for g in gruppi if g not in proposte]
            restano = convocati - {proposte[g] for g in gruppi if g in proposte}
            if len(senza) == 1 and len(restano) == 1:
                extra[senza[0]][next(iter(restano))] += 1.0
        assegna(extra)
    # Somiglianza: un gruppo senza nome molto vicino a uno nominato (e mai nella
    # stessa inquadratura) è probabilmente la stessa persona con un'altra luce.
    if centri:
        import numpy as np
        for g in centri:
            if g in proposte:
                continue
            migliore, sim = None, 0.0
            for h, nome in proposte.items():
                s = float(np.dot(centri[g], centri[h]))
                if s > sim and h not in insieme[g] and \
                        not any(proposte.get(a) == nome for a in insieme[g]):
                    migliore, sim = nome, s
            if migliore and sim >= SOGLIA_STESSA_PERSONA:
                proposte[g] = migliore
    return {g: n for g, n in proposte.items() if g not in (fissi or {})}


def _posizione(x: float) -> str:
    return "a sinistra" if x < 0.38 else "a destra" if x > 0.62 else "al centro"


def analizza(inquadrature: list, foto: dict, cartella: Path, presenze: dict,
             schede: dict = None, modello: str = None) -> dict:
    """Riconosce i volti di tutte le inquadrature.

    Restituisce {numero inquadratura: [{"nome", "posizione", "fotogrammi"}]}.
    """
    import cv2
    import numpy as np
    cartella.mkdir(parents=True, exist_ok=True)
    file_gruppi = cartella / "gruppi.json"
    file_nomi = cartella / "nomi.txt"
    salvati = json.loads(file_gruppi.read_text()) if file_gruppi.exists() else {}

    print("Cerco i volti nei fotogrammi...")
    r = Riconoscitore()
    volti = []
    progresso.fase("volti")
    for n, q in enumerate(inquadrature, 1):
        for i, p in enumerate(foto.get(q.numero, [])):
            for v in r.volti(p):
                v["inquadratura"], v["fotogramma"] = q.numero, i
                volti.append(v)
        progresso.avanzamento("volti", n, len(inquadrature))
    _raggruppa(volti, {g: d["centro"] for g, d in salvati.items()})

    # Gruppi troppo piccoli (comparse, falsi rilevamenti) vengono ignorati.
    quanti = defaultdict(int)
    for v in volti:
        quanti[v["gruppo"]] += 1
    validi = {g for g, n in quanti.items() if n >= MINIMO_VOLTI_GRUPPO or g in salvati}

    per_inquadratura = defaultdict(lambda: defaultdict(list))
    for v in volti:
        if v["gruppo"] in validi:
            per_inquadratura[v["inquadratura"]][v["gruppo"]].append(v)

    centri = {}
    for g in validi:
        imp = np.mean([v["impronta"] for v in volti if v["gruppo"] == g], axis=0)
        centri[g] = imp / np.linalg.norm(imp)
    # Un nome nel file vale come scelta dell'utente se la riga non è più segnata
    # "proposto" o "senza nome" (cioè l'utente l'ha modificata). Un nome vuoto
    # su una riga modificata significa "non nominare".
    nomi_utente, proposte_precedenti = {}, {}
    if file_nomi.exists():
        for riga in file_nomi.read_text(encoding="utf-8").splitlines():
            if ":" in riga and not riga.lstrip().startswith("#"):
                g, resto = riga.split(":", 1)
                nome, _, nota = resto.partition("#")
                g, nome = g.strip(), nome.strip()
                if "proposto" in nota or "senza nome" in nota:
                    if nome:
                        proposte_precedenti[g] = nome
                    continue
                nomi_utente[g] = nome
    visivi = {}
    if modello:
        senza_nome = [g for g in validi if not nomi_utente.get(g)]
        if senza_nome:
            print(f"Confronto {len(senza_nome)} gruppi di volti con le schede dei personaggi...")
        # Dal gruppo più numeroso: le sue impronte sono le più affidabili e diventa riferimento.
        senza_nome.sort(key=lambda g: -quanti[g])
        riferimenti = {}
        for g, nome in nomi_utente.items():
            vv = sorted((v for v in volti if v["gruppo"] == g), key=lambda v: -v["altezza"])
            if nome and vv and nome not in riferimenti:
                riferimenti[nome] = [v["ritaglio"] for v in vv[:6]]
        ritagli = {}
        for g in senza_nome:
            vv = sorted((v for v in volti if v["gruppo"] == g), key=lambda v: -v["altezza"])
            scelti, viste = [], set()
            for v in vv:  # un ritaglio per inquadratura, per varietà
                if v["inquadratura"] not in viste:
                    scelti.append(v["ritaglio"])
                    viste.add(v["inquadratura"])
            ritagli[g] = scelti or [v["ritaglio"] for v in vv]
        candidati = {g: _candidati(g, per_inquadratura, inquadrature, presenze, schede or {})
                     for g in senza_nome}
        visivi = _nomi_dal_modello(ritagli, candidati, schede or {}, modello, riferimenti)
    proposte = _proponi_nomi(per_inquadratura, inquadrature, presenze, centri, nomi_utente, visivi)
    nomi = {g: nomi_utente[g] if g in nomi_utente else proposte.get(g, "") for g in validi}
    # Le proposte di gruppi non visti in questa esecuzione restano nel file.
    for g, nome in proposte_precedenti.items():
        if g not in validi:
            proposte.setdefault(g, nome)

    # Salvataggio dei gruppi, per ritrovarli identici alla prossima esecuzione.
    for g in validi:
        precedente = salvati.get(g, {}).get("centro")
        salvati[g] = {"centro": precedente or centri[g].tolist()}
    file_gruppi.write_text(json.dumps(salvati))

    _scrivi_nomi(file_nomi, validi, {**proposte, **nomi}, nomi_utente, quanti)
    _scrivi_pagina(cartella, volti, validi, nomi, nomi_utente, per_inquadratura, cv2)

    risultato = {}
    for q in inquadrature:
        n_foto = len(foto.get(q.numero, []))
        elenco = []
        for g, vv in sorted(per_inquadratura.get(q.numero, {}).items(), key=lambda x: -len(x[1])):
            nome = nomi.get(g) or f"persona non identificata ({g})"
            fotogrammi = len({v["fotogramma"] for v in vv})
            elenco.append({"nome": nome, "gruppo": g, "fotogrammi": f"{fotogrammi}/{n_foto}",
                           "posizione": _posizione(float(np.mean([v["x"] for v in vv])))})
        risultato[q.numero] = elenco
    nominati = sum(1 for g in validi if nomi.get(g))
    print(f"Volti: {len(volti)} trovati, {len(validi)} persone distinte, {nominati} con un nome. "
          f"Controlla {cartella / 'volti.html'}")
    return risultato


def _scrivi_nomi(file_nomi: Path, validi, nomi, nomi_utente, quanti) -> None:
    righe = ["# Un gruppo per riga: correggi o completa il nome dopo i due punti.",
             "# Più gruppi possono avere lo stesso nome (stessa persona con luci diverse).",
             "# Lascia vuoto per una persona da non nominare. Rilancia il programma per applicare."]
    # Si conservano anche i gruppi non visti in questa esecuzione (es. con --da/--a).
    for g in sorted(set(validi) | set(nomi_utente) | set(nomi)):
        nome = nomi.get(g, nomi_utente.get(g, ""))
        volti = f"{quanti[g]} volti" if quanti.get(g) else "non presente in questa esecuzione"
        if g in nomi_utente:
            nota = f"# confermato, {volti}"
        elif nome:
            nota = f"# proposto automaticamente, {volti}"
        else:
            nota = f"# senza nome, {volti}"
        righe.append(f"{g}: {nome}   {nota}")
    file_nomi.write_text("\n".join(righe) + "\n", encoding="utf-8")


def _scrivi_pagina(cartella: Path, volti, validi, nomi, nomi_utente, per_inquadratura, cv2) -> None:
    (cartella / "img").mkdir(exist_ok=True)
    blocchi = []
    info = []  # gli stessi dati in JSON, per l'app
    for g in sorted(validi, key=lambda g: (nomi.get(g) or "~", g)):
        vv = sorted((v for v in volti if v["gruppo"] == g), key=lambda v: -v["altezza"])
        # Esempi presi da inquadrature diverse, per vedere la persona in situazioni diverse.
        esempi, viste = [], set()
        for v in vv:
            if v["inquadratura"] not in viste:
                esempi.append(v)
                viste.add(v["inquadratura"])
            if len(esempi) == 8:
                break
        immagini = []
        for i, v in enumerate(esempi):
            nome_file = f"img/{g}_{i}.jpg"
            cv2.imwrite(str(cartella / nome_file), v["ritaglio"], [cv2.IMWRITE_JPEG_QUALITY, 85])
            immagini.append(f'<figure><img src="{nome_file}" alt=""><figcaption>inq. '
                            f'{v["inquadratura"]}</figcaption></figure>')
        nome = nomi.get(g)
        stato = ("confermato" if g in nomi_utente else "proposto") if nome else "senza nome"
        numeri = sorted(n for n, gg in per_inquadratura.items() if g in gg)
        inq = ", ".join(str(n) for n in numeri)
        info.append({"gruppo": g, "nome": nome or "", "confermato": g in nomi_utente,
                     "stato": stato, "volti": len(vv), "inquadrature": numeri,
                     "immagini": [{"file": f"img/{g}_{i}.jpg", "inquadratura": v["inquadratura"]}
                                  for i, v in enumerate(esempi)]})
        blocchi.append(
            f'<section><h2>{g} — {html.escape(nome or "?")} <span class="{stato.replace(" ", "-")}">'
            f'{stato}</span></h2><p>{len(vv)} volti · inquadrature {inq}</p>'
            f'<div class="griglia">{"".join(immagini)}</div></section>')
    pagina = f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Volti riconosciuti</title>
<style>
:root{{--sfondo:#f6f5f2;--testo:#1d1d1b;--tenue:#6b6a66;--bordo:#dcdad4;--ok:#2f6f3e;--prop:#8a5a00;--no:#a33}}
@media (prefers-color-scheme:dark){{:root{{--sfondo:#171716;--testo:#ecebe7;--tenue:#a3a29c;--bordo:#3a3936;--ok:#7fc18f;--prop:#e0b25a;--no:#e77}}}}
body{{background:var(--sfondo);color:var(--testo);font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1100px;margin-inline:auto}}
h1{{font-size:22px;margin:0 0 4px}} .intro{{color:var(--tenue);margin:0 0 24px}}
section{{border-top:1px solid var(--bordo);padding:16px 0}} h2{{font-size:17px;margin:0}}
section p{{color:var(--tenue);margin:2px 0 10px;font-size:13px}}
h2 span{{font-size:12px;font-weight:600;margin-left:8px;text-transform:uppercase;letter-spacing:.04em}}
.confermato{{color:var(--ok)}} .proposto{{color:var(--prop)}} .senza-nome{{color:var(--no)}}
.griglia{{display:flex;flex-wrap:wrap;gap:8px}} figure{{margin:0}}
img{{height:120px;width:auto;border-radius:6px;display:block}} figcaption{{font-size:11px;color:var(--tenue)}}
code{{background:var(--bordo);padding:1px 5px;border-radius:4px}}
</style></head><body><h1>Volti riconosciuti</h1>
<p class="intro">Ogni gruppo è una persona. Per confermare o correggere i nomi modifica <code>nomi.txt</code>
in questa cartella e rilancia il programma.</p>{"".join(blocchi)}</body></html>"""
    (cartella / "volti.html").write_text(pagina, encoding="utf-8")
    (cartella / "gruppi_info.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def nomi_confermati(file_nomi: Path) -> dict:
    """{gruppo: nome} delle righe di nomi.txt scelte dall'utente (non "proposto" né "senza nome")."""
    nomi = {}
    try:
        righe = Path(file_nomi).read_text(encoding="utf-8").splitlines()
    except OSError:
        return nomi
    for riga in righe:
        if ":" not in riga or riga.lstrip().startswith("#"):
            continue
        g, _, resto = riga.partition(":")
        nome, _, nota = resto.partition("#")
        if "proposto" in nota or "senza nome" in nota:
            continue
        nomi[g.strip()] = nome.strip()
    return nomi


def testo_per_prompt(elenco: list) -> str:
    if not elenco:
        return "Riconoscimento volti: nessun volto riconoscibile nei fotogrammi."
    righe = ["Persone riconosciute dai volti (riconoscimento facciale automatico, attendibile: "
             "usa questi nomi nel campo personaggi e nell'azione):"]
    for v in elenco:
        righe.append(f"- {v['nome']}: {v['posizione']}, visibile in {v['fotogrammi']} fotogrammi")
    return "\n".join(righe)
