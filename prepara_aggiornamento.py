"""Prepara e pubblica un aggiornamento di Vertov.

Crea in dist/ il pacchetto (vertov-VERSIONE.zip) e l'annuncio (vertov.json). Con
--pubblica li carica come release su GitHub (serve gh, con l'accesso fatto): le
copie di Vertov li trovano all'indirizzo fisso scritto in aggiornamenti.json,
.../releases/latest/download/vertov.json, che punta sempre all'ultima release.

    python prepara_aggiornamento.py 1.1.0 "Novità uno" "Novità due" --pubblica
"""

import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

CARTELLA = Path(__file__).resolve().parent


def file_del_programma() -> list:
    """Solo i file che .gitignore ammette: mai materiali, documenti o risultati di un film."""
    r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=CARTELLA,
                       capture_output=True, text=True, check=True)
    return sorted(f for f in r.stdout.splitlines() if f != ".gitignore" and (CARTELLA / f).is_file())


def main() -> None:
    argomenti = [a for a in sys.argv[1:] if a != "--pubblica"]
    pubblica = "--pubblica" in sys.argv
    if not argomenti:
        sys.exit(__doc__)
    versione, novita = argomenti[0], argomenti[1:]
    # Il pacchetto porta con sé aggiornamenti.json: senza indirizzo, chi lo installa non riceverebbe i successivi.
    if not json.loads((CARTELLA / "aggiornamenti.json").read_text()).get("indirizzo"):
        sys.exit("Scrivi prima in aggiornamenti.json l'indirizzo dove pubblicherai vertov.json.")
    (CARTELLA / "VERSIONE").write_text(versione + "\n")
    dist = CARTELLA / "dist"
    dist.mkdir(exist_ok=True)
    pacchetto = dist / f"vertov-{versione}.zip"
    with zipfile.ZipFile(pacchetto, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in file_del_programma():
            z.write(CARTELLA / rel, rel)
    # Su GitHub il pacchetto si indica con l'indirizzo della sua release, non con "latest": per qualche
    # minuto dopo una pubblicazione "latest" può dare l'annuncio vecchio e il pacchetto nuovo, o viceversa.
    indirizzo = json.loads((CARTELLA / "aggiornamenti.json").read_text())["indirizzo"]
    if pubblica and "/releases/latest/download/" in indirizzo:
        dove = indirizzo.split("/releases/latest/download/")[0] + f"/releases/download/v{versione}/{pacchetto.name}"
    else:
        dove = pacchetto.name
    annuncio = {"versione": versione, "data": date.today().isoformat(), "novita": novita,
                "pacchetto": dove, "sha256": hashlib.sha256(pacchetto.read_bytes()).hexdigest()}
    (dist / "vertov.json").write_text(json.dumps(annuncio, ensure_ascii=False, indent=2))
    print(f"Pronti {pacchetto.name} e vertov.json in {dist}.")
    if pubblica:
        note = "\n".join(f"- {n}" for n in novita) or f"Vertov {versione}"
        subprocess.run(["gh", "release", "create", f"v{versione}", str(pacchetto), str(dist / "vertov.json"),
                        "--title", f"Vertov {versione}", "--notes", note], cwd=CARTELLA, check=True)
        print("Pubblicato: le copie di Vertov lo vedranno al prossimo controllo.")


if __name__ == "__main__":
    main()
