"""Lettura del montaggio da un AAF (Avid Media Composer, Premiere, Resolve).

Dalla sequenza principale si ricavano:
- i tagli, come in un EDL, ma dalla traccia video effettivamente visibile:
  le tracce superiori (V2, V3...) coprono quelle inferiori; titoli e
  composizioni sopra un'inquadratura restano "sovrimpressioni";
- per ogni clip i metadati del bin: scena, inquadratura, ciak, camera,
  obiettivo e focale, diaframma, ISO, data di ripresa, file originale, e le
  colonne di note (descrizione, note di regia, commenti);
- gli effetti applicati (ridimensionamento, cambio velocità, stabilizzazione,
  ribaltamento...) e le dissolvenze;
- le clip delle tracce audio, con il nome della traccia (es. PD, SFX, MUSIC);
- i marker con i loro commenti.

Basta un AAF "link to media": i media non servono. Si usa pyaaf2, in puro Python.
"""

from fractions import Fraction
from pathlib import Path

# Nomi degli effetti Avid -> descrizione; None = effetto tecnico da non riportare.
EFFETTI = {
    "paintresize": "ridimensionamento", "resize": "ridimensionamento", "3d warp": "ridimensionamento",
    "sblend": "composizione", "pip": "immagine nell'immagine", "picture-in-picture": "immagine nell'immagine",
    "motion control": "cambio di velocità", "timewarp": "cambio di velocità", "fluidmotion": "cambio di velocità",
    "freeze": "fermo immagine", "stabilize": "stabilizzazione", "fliphoriz": "ribaltamento orizzontale",
    "flipvert": "ribaltamento verticale", "flip-flop": "ribaltamento", "flop": "ribaltamento orizzontale",
    "titler": "titolo", "title": "titolo", "videodissolve": "dissolvenza", "dip to color": "dissolvenza a colore",
    "fade": "dissolvenza", "blur": "sfocatura", "mosaic": "mosaico", "matte": "mascherino",
    "mask": "mascherino", "color correction": None, "coloradapter": None, "audio": None,
}
# Colonne del bin: chiave Avid (minuscola) -> nome usato da Vertov.
COLONNE = {
    "scene": "scena", "shot": "inquadratura", "take": "ciak", "camera #": "camera", "camera": "camera",
    "camera type": "modello", "lens type": "obiettivo", "focal point (mm)": "focale", "camera aperture": "diaframma",
    "iso": "iso", "shutter angle": "otturatore", "camera fps": "fps_ripresa", "shoot date": "data",
    "shoot day": "giorno", "display name": "file", "camroll": "file", "reel": "reel", "good take": "buona",
    "source resolution": "risoluzione", "distance": "messa_a_fuoco",
}
# Colonne tecniche da ignorare; tutte le altre colonne non vuote sono note.
TECNICHE = {
    "unc", "unc path", "source file path", "manufacturer", "source bit depth", "audio sr", "audio bit depth", "sync audio",
    "sync audio offsetms", "white point (kelvin)", "white balance tint", "filter", "camera serial #",
    "camera firmware", "camera roll angle", "camera tilt angle", "lens number", "camera manufacturer",
    "clip number", "color space notes", "gamma notes", "aspect ratio notes", "asc_sop", "asc_sat",
    "resolve_sizing", "databaseid", "tape", "labroll", "soundroll", "track format", "video", "audio format",
}


def _tc(fotogrammi: int, base: int, drop: bool) -> str:
    """Numero di fotogrammi dalla mezzanotte -> HH:MM:SS:FF (HH:MM:SS;FF se drop frame)."""
    if drop and base in (30, 60):
        perdi = 2 if base == 30 else 4
        al_minuto = base * 60 - perdi
        ai_dieci = base * 600 - perdi * 9
        d, m = divmod(fotogrammi, ai_dieci)
        fotogrammi += perdi * 9 * d + (perdi * ((m - perdi) // al_minuto) if m > perdi else 0)
    f = fotogrammi % base
    s = fotogrammi // base
    sep = ";" if drop else ":"
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}{sep}{f:02d}"


def _tipo(x) -> str:
    return type(x).__name__


def _effetto(nome: str):
    n = (nome or "").lower()
    for chiave, descrizione in EFFETTI.items():
        if chiave in n:
            return descrizione
    return nome or None


def _commenti(mob) -> dict:
    try:
        return {t.name: t.value for t in mob["UserComments"].value or []}
    except Exception:
        return {}


class Lettore:
    def __init__(self, f):
        self.f = f
        self._meta = {}

    def metadati(self, mob) -> dict:
        """Colonne del bin lungo la catena sottoclip -> master clip (la più vicina vince)."""
        if mob is None:
            return {}
        chiave = str(mob.mob_id)
        if chiave in self._meta:
            return self._meta[chiave]
        catena, m = [], mob
        for _ in range(5):
            if m is None or m in catena:
                break
            catena.append(m)
            m = self._sotto(m)
        grezzi = {}
        for m in reversed(catena):  # prima la master clip, poi i sottoclip la sovrascrivono
            grezzi.update({k: v for k, v in _commenti(m).items() if str(v).strip()})
        meta, note = {}, {}
        for k, v in grezzi.items():
            v = str(v).strip()
            nome = COLONNE.get(k.lower())
            if nome:
                meta.setdefault(nome, v)
            elif k.lower() not in TECNICHE and not k.startswith("_"):
                note[k] = v
        if note:
            meta["note"] = note
        self._meta[chiave] = meta
        return meta

    @staticmethod
    def _sotto(mob):
        """La clip da cui deriva un mob (sottoclip -> master clip -> file)."""
        for slot in mob.slots:
            if slot.media_kind not in ("Picture", "Sound"):
                continue
            seg = slot.segment
            if _tipo(seg) == "Sequence":
                seg = next((c for c in seg.components if _tipo(c) == "SourceClip"), None)
            if _tipo(seg) == "SourceClip":
                return seg.mob
            return None
        return None

    def pezzi(self, seg, pos: Fraction, effetti: tuple = (), entrata: str = ""):
        """Clip foglia di un segmento: (inizio, durata, nome, mob, effetti, entrata)."""
        t = _tipo(seg)
        durata = Fraction(seg.length or 0)
        if t == "Sequence":
            p, prossima = pos, ""
            for c in seg.components:
                if _tipo(c) == "Transition":
                    p -= Fraction(c.length or 0)  # la clip seguente si sovrappone alla precedente
                    og = c["OperationGroup"].value if "OperationGroup" in c.keys() else None
                    prossima = _effetto(og.operation.name if og is not None and og.operation else "") or "dissolvenza"
                    continue
                yield from self.pezzi(c, p, effetti, prossima)
                prossima = ""
                p += Fraction(c.length or 0)
        elif t == "OperationGroup":
            nome_op = seg.operation.name if seg.operation else ""
            effetto = _effetto(nome_op)
            con_effetto = effetti + ((effetto,) if effetto else ())
            # Il primo ingresso con una clip vera: in una composizione (SBlend) il primo ingresso
            # è spesso "ciò che c'è sotto" (ScopeReference) e la clip sta nel secondo.
            for ingresso in seg.segments:
                trovati = list(self.pezzi(ingresso, pos, con_effetto, entrata))
                if trovati:
                    yield from trovati
                    return
            if effetto == "titolo" or not list(seg.segments):  # titolo o generatore senza clip sotto
                yield (pos, durata, "titolo" if effetto == "titolo" else (nome_op or "effetto"), None, con_effetto, entrata)
        elif t == "SourceClip":
            mob = seg.mob
            yield (pos, durata, (mob.name or "").strip() if mob else "", mob, effetti, entrata)
        elif t == "Selector":
            yield from self.pezzi(seg["Selected"].value, pos, effetti, entrata)
        elif t == "EssenceGroup":
            scelte = list(seg["Choices"].value or [])
            if scelte:
                yield from self.pezzi(scelte[0], pos, effetti, entrata)
        elif t == "NestedScope":
            strati = list(seg["Slots"].value or [])
            if strati:
                yield from self.pezzi(strati[-1], pos, effetti, entrata)
        # Filler, ScopeReference (= ciò che c'è sotto), Timecode...: nessuna clip


def _sequenza_principale(f):
    candidati = list(f.content.toplevel()) or list(f.content.compositionmobs())
    if not candidati:
        raise ValueError("Nell'AAF non c'è nessuna sequenza.")
    return max(candidati, key=lambda m: sum(Fraction(s.segment.length or 0) for s in m.slots
                                             if s.media_kind == "Picture" and s.segment is not None))


def _strati_video(slot) -> list:
    """Le tracce video V1, V2... (in Avid stanno dentro un NestedScope)."""
    seg = slot.segment
    if _tipo(seg) == "NestedScope":
        return list(seg["Slots"].value or [])
    return [seg]


def _sovrimpressione(p) -> bool:
    _, _, nome, mob, effetti, _ = p
    return mob is None or any(e in ("titolo", "composizione", "immagine nell'immagine") for e in effetti)


def leggi(percorso: Path) -> dict:
    """{"sequenza": info, "eventi": [...], "sovrimpressioni": [...], "marker": [...]}."""
    import aaf2
    with aaf2.open(str(percorso), "r") as f:
        seq = _sequenza_principale(f)
        lettore = Lettore(f)
        slots = list(seq.slots)
        video_slots = [s for s in slots if s.media_kind == "Picture"]
        ritmo = Fraction(str(video_slots[0].edit_rate)) if video_slots else Fraction(24)
        base, inizio, drop = round(float(ritmo)), Fraction(0), False
        for s in slots:
            seg = s.segment
            if _tipo(seg) == "Sequence":
                seg = next((c for c in seg.components if _tipo(c) == "Timecode"), seg)
            if _tipo(seg) == "Timecode":
                base, drop = int(seg.fps), bool(seg.drop)
                inizio = Fraction(int(seg.start)) * Fraction(str(s.edit_rate)) / ritmo
                break

        def tc(pos) -> str:
            return _tc(round(pos + inizio), base, drop)

        def meta_di(p) -> dict:
            _, _, nome, mob, effetti, entrata = p
            meta = dict(lettore.metadati(mob))
            eff = [e for e in dict.fromkeys(effetti) if e]
            if eff:
                meta["effetti"] = eff
            if entrata:
                meta["entrata"] = entrata
            return meta

        # Video: si appiattiscono le tracce tenendo, istante per istante, la più alta visibile.
        eventi, sovrimpressioni = [], []
        pezzi_video = []
        for vs in video_slots:
            scala = ritmo / Fraction(str(vs.edit_rate))
            for strato, seg in enumerate(_strati_video(vs)):
                for i, p in enumerate(lettore.pezzi(seg, Fraction(0))):
                    a, d = p[0] * scala, p[1] * scala
                    if d <= 0:
                        continue
                    if strato > 0 and _sovrimpressione(p):
                        sovrimpressioni.append({"tc_in": tc(a), "tc_out": tc(a + d),
                                                "nome": p[2] if p[3] is not None else "",
                                                "effetti": meta_di(p).get("effetti", []), "traccia": f"V{strato + 1}"})
                        continue
                    pezzi_video.append((a, a + d, strato, len(pezzi_video), p))
        tagli = sorted({x for a, b, *_ in pezzi_video for x in (a, b)})
        visibili = []  # (inizio, fine, indice del pezzo)
        for a, b in zip(tagli, tagli[1:]):
            sopra = [x for x in pezzi_video if x[0] <= a and x[1] >= b]
            if not sopra:
                continue
            # La traccia più alta; sulla stessa traccia (dissolvenza) la clip che entra.
            scelto = max(sopra, key=lambda x: (x[2], x[0]))
            if visibili and visibili[-1][2] == scelto[3] and visibili[-1][1] == a:
                visibili[-1] = (visibili[-1][0], b, scelto[3])
            else:
                visibili.append((a, b, scelto[3]))
        per_indice = {x[3]: x for x in pezzi_video}
        # I vuoti della sequenza sono nero: inquadrature anche loro (spesso con titoli o sonoro).
        fine_seq = max((Fraction(vs.segment.length or 0) * ritmo / Fraction(str(vs.edit_rate)) for vs in video_slots), default=0)
        con_neri, p0 = [], Fraction(0)
        for a, b, i in visibili:
            if a - p0 >= 1:
                con_neri.append((p0, a, None))
            con_neri.append((a, b, i))
            p0 = b
        if fine_seq - p0 >= 1:
            con_neri.append((p0, fine_seq, None))
        for a, b, i in con_neri:
            if i is None:
                eventi.append({"tc_in": tc(a), "tc_out": tc(b), "clip": "nero", "video": True, "traccia": "V1",
                               "meta": {"effetti": ["nero"]}})
                continue
            _, _, strato, _, p = per_indice[i]
            eventi.append({"tc_in": tc(a), "tc_out": tc(b), "clip": p[2], "video": True,
                           "traccia": f"V{strato + 1}", "meta": meta_di(p)})

        # Audio: ogni traccia con il suo nome (es. PD, SFX, MUSIC).
        tracce_audio, n = [], 0
        for s in slots:
            if s.media_kind != "Sound":
                continue
            n += 1
            numero = s["PhysicalTrackNumber"].value if "PhysicalTrackNumber" in s.keys() else n
            nome_traccia = f"A{numero}" + (f" {s.name.strip()}" if (s.name or "").strip() else "")
            tracce_audio.append(nome_traccia)
            scala = ritmo / Fraction(str(s.edit_rate))
            for p in lettore.pezzi(s.segment, Fraction(0)):
                if p[3] is None or p[1] <= 0:
                    continue
                a = p[0] * scala
                eventi.append({"tc_in": tc(a), "tc_out": tc(a + p[1] * scala), "clip": p[2],
                               "video": False, "traccia": nome_traccia, "nome_traccia": (s.name or "").strip()})

        # Marker con commento.
        marker = []
        for s in slots:
            if s.media_kind != "Descriptive Metadata" or _tipo(s.segment) != "Sequence":
                continue
            scala = ritmo / Fraction(str(s.edit_rate))
            for c in s.segment.components:
                chiavi = c.keys()
                if "Position" not in chiavi:
                    continue
                commento = c["Comment"].value if "Comment" in chiavi else ""
                attr = {}
                if "CommentMarkerAttributeList" in chiavi:
                    attr = {t.name: t.value for t in c["CommentMarkerAttributeList"].value or []}
                marker.append({"tc": tc(Fraction(c["Position"].value) * scala), "commento": (commento or "").strip(),
                               "colore": attr.get("_ATN_CRM_COLOR", ""), "autore": attr.get("_ATN_CRM_USER", "")})

        nomi_v = {e["traccia"] for e in eventi if e["video"]}
        info = {"nome": seq.name, "inizio": tc(Fraction(0)), "tracce_video": len(_strati_video(video_slots[0])) if video_slots else 0,
                "tracce_audio": tracce_audio, "fps": float(ritmo), "tracce_video_usate": sorted(nomi_v)}
    return {"sequenza": info, "eventi": eventi, "sovrimpressioni": sovrimpressioni, "marker": marker}


def leggi_eventi(percorso: Path) -> list:
    return leggi(percorso)["eventi"]


if __name__ == "__main__":
    import json
    import sys
    dati = leggi(Path(sys.argv[1]))
    print(json.dumps(dati["sequenza"], ensure_ascii=False))
    for e in dati["eventi"]:
        if e["video"]:
            print(e["traccia"], e["tc_in"], e["tc_out"], e["clip"], json.dumps(e["meta"], ensure_ascii=False)[:200])
    print("sovrimpressioni:", dati["sovrimpressioni"])
    print("marker:", dati["marker"])
    print("eventi audio:", sum(1 for e in dati["eventi"] if not e["video"]))
