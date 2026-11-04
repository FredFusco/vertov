"""Report di lavorazione di Vertov: un documento HTML autosufficiente.

Raccoglie in un'unica pagina, stampabile in PDF:
- i materiali usati e come sono stati sfruttati (o cosa mancava);
- il montaggio (sequenza, marker) e le scene, ognuna con una breve sinossi
  scritta dal modello a partire dalle descrizioni delle sue inquadrature;
- i personaggi con il tempo in scena e i volti riconosciuti;
- dialoghi e sonoro, correzioni del montatore, cose da controllare, tempi.

    python report.py CARTELLA_RISULTATI
"""

import base64
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path

import modello as modulo_modello
import progresso

SCHEMA_SINOSSI = {"type": "object", "properties": {"sinossi": {"type": "string"}}, "required": ["sinossi"]}
NOMI_RUOLI = {
    "video": "Video del film", "edl": "Montaggio (EDL o AAF)", "audio_dialoghi": "Stem dialoghi",
    "audio_sfx": "Stem effetti", "audio_musica": "Stem musica", "audio_mix": "Mix completo",
    "sceneggiatura": "Sceneggiatura", "dialoghi": "Trascrizione dialoghi", "odg": "Ordini del giorno",
}
SE_MANCA = {
    "edl": "tagli rilevati automaticamente dalle immagini, senza nomi clip",
    "sceneggiatura": "personaggi senza nome e nessuna battuta del copione",
    "dialoghi": "dialoghi trascritti con Whisper" ,
    "odg": "volti nominati solo da copione e immagini",
    "audio_dialoghi": "sonoro analizzato dal mix", "audio_sfx": "", "audio_musica": "", "audio_mix": "",
}


def _leggi_json(percorso: Path, predefinito):
    try:
        return json.loads(percorso.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return predefinito


def _durata(sec: float) -> str:
    sec = int(round(sec or 0))
    if sec >= 3600:
        return f"{sec // 3600} h {sec % 3600 // 60:02d} min"
    return f"{sec // 60} min {sec % 60:02d} s" if sec >= 60 else f"{sec} s"


def _e(x) -> str:
    return html.escape(str(x or ""))


def _nomi(r: dict) -> list:
    """Personaggi nominati in un'inquadratura (senza comparse e descrizioni generiche)."""
    nomi = []
    for p in r.get("personaggi", []):
        nome = re.split(r"[(:,]", p)[0].strip()
        if nome and nome[0].isupper() and len(nome.split()) <= 2 and not nome.lower().startswith(("comparsa", "persona", "un ", "una ")):
            nomi.append(nome)
    return nomi


def sinossi_scene(scene: list, modello: str, cache_file: Path) -> dict:
    """Una sinossi di 2-3 frasi per scena, scritta dal modello dalle descrizioni (con cache)."""
    cache = _leggi_json(cache_file, {})
    risultato = {}
    da_fare = []
    for chiave, righe in scene:
        testo = "\n".join(f"- {r['descrizione']}" for r in righe if r.get("descrizione"))
        impronta = hashlib.sha1(testo.encode()).hexdigest()
        if cache.get(chiave, {}).get("impronta") == impronta:
            risultato[chiave] = cache[chiave]["sinossi"]
        elif testo:
            da_fare.append((chiave, testo, impronta))
    if da_fare:
        progresso.fase("report")
    for i, (chiave, testo, impronta) in enumerate(da_fare, 1):
        prompt = ("Ecco le descrizioni, in ordine, delle inquadrature di una scena di un film. "
                  "Scrivi in italiano una sinossi della scena in due o tre frasi: cosa succede e di cosa "
                  "parlano i personaggi, al presente, senza elencare le inquadrature e senza inventare "
                  "nulla che non sia nelle descrizioni.\n\n" + testo[:12000])
        try:
            sinossi = modulo_modello.chiedi(prompt, schema=SCHEMA_SINOSSI, modello=modello, timeout=300)["sinossi"]
        except modulo_modello.ErroreModello as e:
            print(f"Sinossi della scena {chiave} non riuscita: {e}")
            progresso.avviso(f"Sinossi della scena {chiave} non scritta: il modello non ha risposto.")
            sinossi = ""
        risultato[chiave] = sinossi
        if sinossi:
            cache[chiave] = {"impronta": impronta, "sinossi": sinossi}
        progresso.avanzamento("report", i, len(da_fare))
    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return risultato


def genera(out: Path, modello: str = None) -> Path:
    out = Path(out)
    righe = [json.loads(r) for r in (out / "inquadrature.jsonl").read_text(encoding="utf-8").splitlines() if r.strip()]
    per_numero = {}
    for r in righe:
        per_numero[r["numero"]] = r
    righe = [per_numero[n] for n in sorted(per_numero)]
    progetto = _leggi_json(out / "progetto.json", {})
    lavoro = _leggi_json(out / "lavoro.json", {})
    gruppi = _leggi_json(out / "volti" / "gruppi_info.json", [])
    correzioni = _leggi_json(out / "correzioni.json", {})
    modello = modello or modulo_modello.MODELLO

    # Scene, nell'ordine del montaggio (una scena può tornare: si tiene ogni blocco contiguo).
    scene = []
    for r in righe:
        chiave = r.get("numero_scena") or r.get("scena") or "—"
        if scene and scene[-1][0] == chiave:
            scene[-1][1].append(r)
        else:
            scene.append((chiave, [r]))
    chiavi_uniche = {}
    for chiave, rr in scene:  # sinossi per scena (unendo i blocchi della stessa scena)
        chiavi_uniche.setdefault(chiave, []).extend(rr)
    sinossi = sinossi_scene(list(chiavi_uniche.items()), modello, out / "sinossi.json")

    durata_tot = sum(r.get("durata_s", 0) for r in righe)
    tempo_personaggio, scene_personaggio, inq_personaggio = {}, {}, {}
    for r in righe:
        for n in set(_nomi(r)):
            tempo_personaggio[n] = tempo_personaggio.get(n, 0) + r.get("durata_s", 0)
            inq_personaggio[n] = inq_personaggio.get(n, 0) + 1
            scene_personaggio.setdefault(n, set()).add(r.get("numero_scena") or "")
    battute = sum(len(r.get("dialoghi", [])) for r in righe)
    nel_copione = sum(1 for r in righe for c in r.get("copione", []) if c)
    scartate = 0
    if (out / "battute_scartate.txt").exists():
        scartate = sum(1 for x in (out / "battute_scartate.txt").read_text(encoding="utf-8").splitlines()
                       if x.strip() and not x.startswith("#"))
    marker = [(r["numero"], r["tc_in"], m) for r in righe for m in (r.get("montaggio") or {}).get("marker", [])]
    proposti = [g for g in gruppi if g.get("nome") and not g.get("confermato") and g["nome"] != "comparsa"]
    senza_nome = [g for g in gruppi if not g.get("nome")]
    sconosciute = sum(1 for r in righe for v in r.get("volti", []) if v.get("nome", "").startswith("persona non"))

    # ---------- composizione della pagina ----------
    s = []
    titolo = progetto.get("nome") or Path(progetto.get("video", "")).stem or out.name
    s.append(f"<header><div class='marchio'>VE<span class='r'>R</span>TOV</div><h1>Report di lavorazione</h1>"
             f"<p class='sotto'>{_e(titolo)} · {time.strftime('%d/%m/%Y %H:%M')}</p></header>")
    tessere = [("Inquadrature", len(righe)), ("Scene", len(chiavi_uniche)), ("Durata", _durata(durata_tot)),
               ("Personaggi", len(tempo_personaggio)), ("Battute", battute),
               ("Elaborazione", _durata(lavoro.get("durata_totale_s", 0)) if lavoro.get("durata_totale_s") else "—")]
    s.append("<section class='tessere'>" + "".join(f"<div><b>{_e(v)}</b><span>{_e(k)}</span></div>" for k, v in tessere) + "</section>")

    # Materiali
    s.append("<section><h2>Materiali</h2><table><tr><th>Ruolo</th><th>File</th><th>Dettagli</th></tr>")
    info = progetto.get("info", {})
    for ruolo, nome in NOMI_RUOLI.items():
        valori = progetto.get(ruolo) or []
        valori = [valori] if isinstance(valori, str) else valori
        if valori:
            for v in valori:
                s.append(f"<tr><td>{nome}</td><td>{_e(Path(v).name)}</td><td>{_e(info.get(v, {}).get('dettaglio', ''))}</td></tr>")
        elif SE_MANCA.get(ruolo):
            s.append(f"<tr class='manca'><td>{nome}</td><td>non fornito</td><td>{_e(SE_MANCA[ruolo])}</td></tr>")
    s.append("</table></section>")

    # Montaggio
    seq = lavoro.get("sequenza") or {}
    s.append("<section><h2>Montaggio</h2><p>")
    if seq:
        s.append(f"Sequenza <b>{_e(seq.get('nome'))}</b>, inizio {_e(seq.get('inizio'))}, "
                 f"{_e(seq.get('tracce_video'))} tracce video, tracce audio: {_e(', '.join(seq.get('tracce_audio', [])))}. ")
    s.append(f"Fonte dei tagli: {_e(lavoro.get('fonte_tagli', 'non registrata'))}.</p>")
    if marker:
        s.append("<h3>Marker</h3><ul>" + "".join(f"<li><span class='tc'>{_e(tc)}</span> inq. {n} — {_e(m)}</li>"
                                               for n, tc, m in marker) + "</ul>")
    s.append("</section>")

    # Scene
    s.append("<section><h2>Scene</h2>")
    for chiave, rr in scene:
        nomi = sorted({n for r in rr for n in _nomi(r)})
        intest = rr[0].get("scena") or ""
        s.append(f"<article class='scena'><h3><span class='num'>{_e(chiave)}</span> {_e(intest)}</h3>"
                 f"<p class='meta'>{_e(rr[0]['tc_in'])} – {_e(rr[-1]['tc_out'])} · {len(rr)} inquadrature · "
                 f"{_durata(sum(r.get('durata_s', 0) for r in rr))}{' · ' + _e(', '.join(nomi)) if nomi else ''}</p>")
        if sinossi.get(chiave):
            s.append(f"<p class='sinossi'>{_e(sinossi[chiave])}</p>")
        s.append("<table class='inq'>")
        for r in rr:
            extra = " <span class='corr'>corretta</span>" if r.get("corretta") else ""
            s.append(f"<tr><td class='n'>{r['numero']}</td><td class='tc'>{_e(r['tc_in'])}</td>"
                     f"<td class='piano'>{_e(r.get('piano'))}</td><td>{_e(r.get('descrizione'))}{extra}</td></tr>")
        s.append("</table></article>")
    s.append("</section>")

    # Personaggi
    s.append("<section><h2>Personaggi</h2><table><tr><th>Personaggio</th><th>Inquadrature</th><th>Tempo in scena</th><th>Scene</th><th>Volti</th></tr>")
    for n in sorted(tempo_personaggio, key=lambda x: -tempo_personaggio[x]):
        facce = ""
        for g in [g for g in gruppi if g.get("nome") == n][:2]:
            for im in g.get("immagini", [])[:2]:
                f = out / "volti" / im["file"]
                if f.exists():
                    dati = base64.b64encode(f.read_bytes()).decode()
                    stato = "confermato" if g.get("confermato") else "proposto"
                    facce += f"<img src='data:image/jpeg;base64,{dati}' alt='' title='{g['gruppo']} · {stato}'>"
        sc = ", ".join(sorted((x for x in scene_personaggio[n] if x), key=lambda x: (len(x), x)))
        s.append(f"<tr><td><b>{_e(n)}</b></td><td>{inq_personaggio[n]}</td><td>{_durata(tempo_personaggio[n])}</td>"
                 f"<td>{_e(sc)}</td><td class='facce'>{facce}</td></tr>")
    s.append("</table></section>")

    # Dialoghi e sonoro
    s.append("<section><h2>Dialoghi e sonoro</h2><ul>")
    s.append(f"<li>{battute} battute assegnate alle inquadrature; {nel_copione} ritrovate nel copione"
             f" ({round(100 * nel_copione / battute) if battute else 0}%).</li>")
    if scartate:
        s.append(f"<li>{scartate} frasi della trascrizione scartate perché nella traccia non c'era voce "
                 f"(tipiche invenzioni di Whisper su silenzi e musica).</li>")
    tracce = [NOMI_RUOLI[r] for r in ("audio_dialoghi", "audio_sfx", "audio_musica", "audio_mix") if progetto.get(r)]
    s.append(f"<li>Tracce analizzate: {_e(', '.join(tracce) or 'audio del video')}.</li></ul></section>")

    # Correzioni e cose da controllare
    if correzioni:
        s.append("<section><h2>Correzioni del montatore</h2><ul>" + "".join(
            f"<li>inq. {_e(n)} — {_e(t)}</li>" for n, t in sorted(correzioni.items(), key=lambda x: int(x[0]))) + "</ul></section>")
    controlli = []
    if proposti:
        controlli.append(f"{len(proposti)} gruppi di volti hanno un nome proposto automaticamente e non confermato "
                         f"({_e(', '.join(sorted({g['nome'] for g in proposti})))}): controllali nella scheda Volti.")
    if senza_nome:
        controlli.append(f"{len(senza_nome)} gruppi di volti sono senza nome.")
    if sconosciute:
        controlli.append(f"{sconosciute} presenze di persone non identificate nelle inquadrature.")
    controlli.append("Le descrizioni nascono dai fotogrammi: i nomi di chi è in campo sono affidabili, "
                     "i dettagli su chi fa un gesto vanno verificati quando due personaggi sono vicini.")
    s.append("<section><h2>Da controllare</h2><ul>" + "".join(f"<li>{c}</li>" for c in controlli) + "</ul></section>")

    # Note tecniche
    s.append("<section><h2>Note tecniche</h2><ul>")
    s.append(f"<li>Modello di visione: {_e(modello)}, in locale con Ollama.</li>")
    for x in lavoro.get("elaborazioni", [])[-6:]:
        s.append(f"<li>{_e(x.get('quando'))}: {_e(x.get('tipo'))}, {x.get('inquadrature', 0)} inquadrature in {_durata(x.get('durata_s', 0))}.</li>")
    s.append("</ul></section>")

    pagina = PAGINA.replace("{{TITOLO}}", _e(f"Report Vertov — {titolo}")).replace("{{CORPO}}", "\n".join(s))
    dest = out / "report_vertov.html"
    dest.write_text(pagina, encoding="utf-8")
    print(f"Report salvato in {dest}")
    return dest


PAGINA = """<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITOLO}}</title>
<style>
:root { --carta:#f4ede0; --pannello:#fbf8f2; --inchiostro:#2b2a33; --tenue:#6b6670; --linea:#dcd2c0; --ocra:#d9a441; --rosso:#b5533c;
  --serif:"Iowan Old Style","Baskerville","Palatino",Georgia,serif; }
* { box-sizing:border-box; }
body { margin:0; background:var(--carta); color:var(--inchiostro); font:14px/1.55 -apple-system,"Helvetica Neue",sans-serif; }
main { max-width:960px; margin:0 auto; padding:28px 20px 60px; }
.barra { position:sticky; top:0; background:var(--inchiostro); color:var(--carta); padding:10px 20px; display:flex; gap:12px; align-items:center; justify-content:flex-end; z-index:2; }
.barra button { font:500 14px -apple-system,sans-serif; border:0; border-radius:8px; padding:8px 16px; background:var(--ocra); color:#2b2a33; cursor:pointer; }
header { border-bottom:1px solid var(--linea); padding-bottom:14px; margin-bottom:18px; }
.marchio { font:800 15px/1 "Helvetica Neue",sans-serif; letter-spacing:.3em; color:var(--tenue); }
.marchio .r { display:inline-block; transform:scaleX(-1); }
h1 { font:500 34px/1.15 var(--serif); margin:6px 0 2px; }
.sotto { color:var(--tenue); font-style:italic; font-family:var(--serif); margin:0; }
h2 { font:500 22px/1.2 var(--serif); margin:28px 0 10px; padding-top:8px; border-top:1px solid var(--linea); }
h3 { font:500 17px/1.3 var(--serif); margin:16px 0 4px; }
.tessere { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; }
.tessere div { background:var(--pannello); border:1px solid var(--linea); border-radius:12px; padding:12px; }
.tessere b { display:block; font:500 24px/1.1 var(--serif); }
.tessere span { color:var(--tenue); font-size:12.5px; }
table { width:100%; border-collapse:collapse; margin:6px 0; }
th { text-align:left; font:italic 500 13.5px var(--serif); color:var(--tenue); border-bottom:1px solid var(--tenue); padding:5px 6px; }
td { border-bottom:1px solid var(--linea); padding:6px; vertical-align:top; }
tr.manca td { color:var(--tenue); font-style:italic; }
.scena { break-inside:avoid-page; }
.num { display:inline-block; min-width:2.2em; text-align:center; background:var(--inchiostro); color:var(--carta); border-radius:10px; font:600 13px -apple-system,sans-serif; padding:2px 7px; margin-right:6px; vertical-align:2px; }
.meta { color:var(--tenue); font-size:12.5px; margin:0 0 6px; }
.sinossi { font-family:var(--serif); font-size:15.5px; border-left:3px solid var(--ocra); padding:2px 0 2px 12px; margin:6px 0 8px; }
table.inq td { font-size:12.5px; }
td.n, td.tc, .tc { color:var(--tenue); font-variant-numeric:tabular-nums; white-space:nowrap; }
td.piano { width:120px; color:var(--tenue); }
.corr { font-size:11px; color:#4f7a5a; font-weight:600; margin-left:4px; }
.facce img { height:46px; width:auto; border-radius:4px; margin-right:3px; }
ul { padding-left:18px; } li { margin:3px 0; }
@media print {
  .barra { display:none; } body { background:#fff; } main { padding:0; max-width:none; }
  .tessere div { background:#fff; } h2 { break-after:avoid; } tr { break-inside:avoid; }
  @page { margin:16mm 14mm; }
}
</style></head><body>
<div class="barra"><button onclick="window.print()">Salva come PDF / stampa</button></div>
<main>{{CORPO}}</main></body></html>"""


if __name__ == "__main__":
    genera(Path(sys.argv[1]))
