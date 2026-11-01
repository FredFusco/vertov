"""Segnali di avanzamento leggibili dall'app (righe che iniziano con "@@").

Da riga di comando compaiono come normali righe di testo.
"""


def fase(nome: str) -> None:
    print(f"@@fase {nome}", flush=True)


def avanzamento(nome: str, fatti: int, totale: int) -> None:
    # Al massimo circa 200 aggiornamenti per fase, per non intasare l'uscita.
    if totale <= 200 or fatti % max(1, totale // 200) == 0 or fatti == totale:
        print(f"@@avanz {nome} {fatti} {totale}", flush=True)
