"""Segnali di avanzamento leggibili dall'app (righe che iniziano con "@@").

Da riga di comando compaiono come normali righe di testo. Se l'analisi si è registrata
(coordinamento.Registro), fase e avanzamento finiscono anche nel registro: così l'app li
vede pure per le analisi lanciate da Terminale.
"""

REGISTRO = None


def fase(nome: str) -> None:
    print(f"@@fase {nome}", flush=True)
    if REGISTRO:
        REGISTRO.aggiorna(fase=nome, fatti=0, totale=0)
        if nome == "fine":
            REGISTRO.finita = True


def avanzamento(nome: str, fatti: int, totale: int) -> None:
    # Al massimo circa 200 aggiornamenti per fase, per non intasare l'uscita.
    if totale <= 200 or fatti % max(1, totale // 200) == 0 or fatti == totale:
        print(f"@@avanz {nome} {fatti} {totale}", flush=True)
    if REGISTRO:
        REGISTRO.aggiorna(fatti=fatti, totale=totale)
