# Descrittore di inquadrature

Descrive ogni inquadratura di un film (immagine e sonoro) usando solo modelli locali e gratuiti:

- **Immagini:** Gemma 4 26B (`gemma4:26b`) tramite Ollama; il modello si sceglie in `modello.py`
- **Personaggi:** riconoscimento dei volti dal film stesso (YuNet e SFace di OpenCV), con i nomi dedotti da copione e ordini del giorno
- **Dialoghi:** Whisper large-v3-turbo (mlx-whisper), solo se non fornisci già una trascrizione; le battute vengono ricollegate al copione
- **Effetti e musica:** classificatore AST addestrato su AudioSet (riconosce 527 tipi di suono, compreso il carattere della musica)

Niente esce dal computer.

## L'app

Vertov è un'app del Mac: sta nella cartella Applicazioni dell'utente (`~/Applications/Vertov.app`), la trovi con Spotlight e Launchpad e la puoi tenere nel Dock. Questa cartella contiene solo il codice sorgente, da cui si costruisce e si installa l'app con `mac/compila.sh --installa`.

**Vertov.app è un'app nativa per Mac** (Swift, sorgente in `mac/Vertov.swift`, si compila con `mac/compila.sh`; bastano gli strumenti da riga di comando di Xcode). Si apre subito con il logo e «Avvio di Vertov…», ha menu e scorciatoie native (con *Vertov › Impostazioni…* ⌘,), finestre di scelta dei file del Mac, la percentuale dell'analisi sull'icona nel Dock, il report e la guida in finestre proprie (dal report, *Salva come PDF / stampa* apre la stampa del Mac), la conferma dell'uscita con il logo di Vertov, ricorda posizione e misura delle finestre e riceve video, montaggi, audio, documenti e cartelle trascinati sulla finestra o sull'icona nel Dock, o aperti dal Finder con *Apri con › Vertov* (Vertov non diventa l'app predefinita per quei file). Il motore resta in Python: l'app avvia `app.py --servizio`, cioè solo il server dell'interfaccia, e lo ferma all'uscita (SIGTERM, che ferma anche un'analisi in corso, e SIGKILL dopo 5 secondi: nessun processo resta acceso); se il motore si ferma da solo, l'app propone di riavviarlo. Con la porta 8765 già occupata (un altro Vertov aperto dal Terminale) il server esce con il codice 3 e l'app lo dice; dopo un aggiornamento esce con il codice 75 e l'app si chiude e si riapre senza chiedere. `python app.py` senza `--servizio` apre ancora la finestra di pywebview, come prima: resta come ripiego e per il Terminale.

**Vertov.app è completa:** dentro, in `Contents/Resources/programma`, c'è tutto il programma (i file che `.gitignore` ammette, tranne `mac/`); fuori restano solo l'ambiente Python (`~/.venvs/descrittore`, condiviso con Murnau), i dati (`~/Library/Application Support/Vertov`, con sessioni, file di lavoro e preferenze), le cache dei modelli e Ollama. `mac/compila.sh` la costruisce in `~/Library/Caches/Vertov/` (fuori da Drive, perché Spotlight non la trovi due volte) e con `--installa` la mette in `~/Applications`, scambiandola con quella che c'è. Il motore gira con `PYTHONDONTWRITEBYTECODE=1`, così non scrive file dentro l'app e la firma resta valida (`codesign --verify --deep --strict`). Al primo avvio su un Mac nuovo Vertov prepara da sé l'ambiente Python (PyTorch, Transformers, OpenCV, mlx-whisper, pyaaf2, pypdf, pywebview): ci vuole qualche minuto.

L'app e il motore scrivono nello stesso registro, `~/.cache/descrittore/vertov.log` (*Aiuto › Apri il registro di Vertov*).

Chiudendo Vertov compare un riquadro di conferma con **Non mostrare più**: spuntato, le volte successive l'app si chiude subito, tranne quando un'analisi è in corso. *Vertov › Impostazioni…* (⌘,) lo riattiva.

## Uso consigliato (in due passaggi)

**1. Volti.** Riconosce le persone e prepara la pagina di controllo (pochi minuti):

```bash
~/.venvs/descrittore/bin/python descrivi.py FILM.mov --edl FILM.edl \
  --sceneggiatura SCENEGGIATURA.pdf --dialoghi DIALOGHI.srt --odg "ORDINI DEL GIORNO" \
  --out risultati --solo-volti
```

Apri `risultati/volti/volti.html`: ogni gruppo è una persona. In `risultati/volti/nomi.txt` correggi o completa i nomi. Una riga modificata (senza più la nota "proposto" o "senza nome") vale come confermata. Più gruppi possono avere lo stesso nome: è la stessa persona con luci diverse.

**2. Descrizioni.** Stesso comando, senza `--solo-volti` e con l'audio (questo passaggio richiede ore):

```bash
~/.venvs/descrittore/bin/python descrivi.py FILM.mov --edl FILM.edl \
  --sceneggiatura SCENEGGIATURA.pdf --dialoghi DIALOGHI.srt --odg "ORDINI DEL GIORNO" \
  --audio-dialoghi DX.wav --audio-sfx FX.wav --audio-musica MX.wav \
  --out risultati
```

## Parametri

Tutti i parametri sono facoltativi tranne il video.

| Parametro | Cosa fa |
|---|---|
| `--edl` | Tagli presi dall'EDL (CMX3600) o dall'AAF (Avid, Premiere, Resolve). Dall'AAF si leggono anche scena/inquadratura/ciak e colonne del bin, camera e ottica, effetti, sovrimpressioni e marker. Dai nomi clip in formato scena/inquadratura/ciak ricava la scena. Senza EDL, i tagli vengono rilevati automaticamente |
| `--sceneggiatura` | `.fdx` (Final Draft), `.fountain`, `.txt` o `.pdf`. Da qui vengono estratte anche le schede dei personaggi |
| `--odg` | Cartella con gli ordini del giorno in PDF: dice quali personaggi sono convocati in ogni scena. Vengono letti solo i nomi dei personaggi, non quelli degli interpreti |
| `--personaggi` | File con righe `Nome: aspetto` che integra o corregge le schede estratte (es. per personaggi mai descritti nel copione) |
| `--dialoghi` | Trascrizione `.srt` o `.vtt` |
| `--trascrivi` | Senza `--dialoghi`, trascrive con Whisper (dallo stem dialoghi o dal mix) |
| `--audio-dialoghi`, `--audio-sfx`, `--audio-musica` | Stem separati |
| `--audio-mix` | Mix completo come file separato. Se non indichi nessuno stem, viene usato l'audio del video |
| `--senza-audio` | Salta l'analisi del sonoro |
| `--solo-volti` | Riconosce i volti, prepara la pagina di controllo e si ferma |
| `--senza-volti` | Non usa il riconoscimento dei volti |
| `--senza-ritaglio` | Non toglie le bande nere dai fotogrammi |
| `--tc-inizio` | Timecode del primo fotogramma del video, se il file non lo dichiara |
| `--tc-inizio-audio` | Timecode di inizio degli stem, se non è scritto nei WAV (BWF) |
| `--offset-dialoghi` | Timecode a cui corrisponde lo 00:00:00 dei sottotitoli |
| `--da N --a M` | Elabora solo le inquadrature da N a M |
| `--modello` | Un altro modello Ollama (es. `qwen3.8:27b`, più ricco ma circa quattro volte più lento) |

Se interrompi l'elaborazione, rilanciando lo stesso comando riparte da dove si era fermata. Per rifare le descrizioni da capo, cancella `inquadrature.jsonl` dalla cartella dei risultati.

## Risultati (nella cartella `--out`)

Dall'app, `--out` è la cartella di lavoro della sessione (`~/Library/Application Support/Vertov/file di lavoro/<sessione>`, mai dentro Vertov.app né nella cartella del codice, quindi mai pubblicata e mai toccata da un aggiornamento): l'utente non la vede, se non con *File › Mostra i file di lavoro*. Nella cartella di destinazione finiscono solo i file che sceglie in *Esporta*, chiamati `<sessione> - descrizioni.csv`, `<sessione> - marker Avid.txt`, `<sessione> - marker Resolve.edl`, `<sessione> - report.html`, `<sessione> - dialoghi.srt`, `<sessione> - dati.jsonl`. All'avvio l'app porta nella cartella dei file di lavoro le cartelle create dalle versioni precedenti (fino alla 1.0.9 stavano nella cartella del codice, in `file di lavoro/`; mai quella di un'analisi in corso) e sposta nella cartella di lavoro i file che le versioni ancora più vecchie lasciavano nella cartella dei risultati (non quelli di Murnau, che può usare la stessa cartella).

A fine lavoro l'analisi segnala le cose da verificare con righe `@@avviso` (vedi `progresso.py`); l'app le mostra nel riquadro dell'esito insieme all'eventuale errore.

- `inquadrature.csv`: si apre in Excel. Una riga per inquadratura con piano, movimento di macchina, personaggi, azione, ambiente, luce, dialoghi, battute del copione, sonoro, suoni rilevati e descrizione
- colonna `scala` del CSV: la scala del piano da un elenco fisso (`SCALE` in `descrivi.py`), per filtrare e ordinare; `piano` resta il testo libero con angolazioni e over the shoulder
- `inquadrature.jsonl`: gli stessi dati in formato completo, compresi volti riconosciuti e livelli audio
- `marker_resolve.edl`: in DaVinci Resolve, *Timeline > Import > Timeline Markers from EDL* mette ogni descrizione come marker sulla timeline
- `marker_avid.txt`: in Avid Media Composer, finestra *Markers* › menu › *Import Markers* (formato di testo con tabulazioni, come Murnau; marker verdi sulla traccia V1)
- `volti/volti.html` e `volti/nomi.txt`: controllo e correzione dei personaggi
- `personaggi.txt`: le schede estratte dalla sceneggiatura, da copiare e correggere per `--personaggi`
- `dialoghi_trascritti.srt`: creato solo con `--trascrivi`

## Tempi

Su M5 Max servono circa 6 secondi per inquadratura (descrizione 3–6 s, volti, sonoro e fotogrammi circa 2 s). Per un film di 2.000 inquadrature sono 3–4 ore, più la trascrizione con Whisper. A batteria il processore grafico va a metà velocità. Alla ripresa i volti già trovati si rileggono da `volti/rilevamenti/` e non si richiedono al modello i gruppi già esaminati.

## Volti e movimento

- `volti.py`: sui fotogrammi con luce colorata o scarsa (dominante di colore o luminosità bassa in Lab) i volti si cercano dopo bianco medio e CLAHE, con una soglia di rilevamento più alta (`PUNTEGGIO_CORRETTA`) per non creare falsi volti. Misurato su una festa con luci viola: +26% di volti riconosciuti, nessun falso; di giorno nulla cambia. I rilevamenti sono salvati per inquadratura in `volti/rilevamenti/` (con `VERSIONE_RILEVAMENTI`).
- `movimento.py`: per ogni inquadratura fotogrammi a 6 al secondo, 320 pixel, in grigi; tra fotogrammi successivi punti ORB e trasformazione RANSAC (bande alta e bassa escluse per il timecode impresso). Conta solo il movimento costante (passo tipico, direzione concorde nel 65% dei passi): panoramica o carrello laterale, verticale, avanzamento o zoom, tremolio da macchina a mano. Provato su movimenti costruiti (direzione e ampiezza giuste) e su un rullo vero. Le misure stanno in `movimento.json` e vanno nel prompt come dato indicativo.

## Il modello

Tutte le richieste al modello di visione (descrizioni, nomi dei volti, sinossi del report) passano da `modello.py`:

- `MODELLO` è l'unico posto dove si sceglie il modello (scelto il 4 novembre 2026 confrontando 15 modelli locali su inquadrature con dialoghi: Gemma 4 26B è stato il più preciso e circa tre volte più veloce di Qwen3-VL).
- I modelli del motore MLX di Ollama (come `gemma4:26b`) rifiutano lo schema JSON (`format`, errore 501): per loro lo schema va nel testo e la risposta viene ripulita (```json, `<|eot|>`) e controllata campo per campo.
- Gli errori di rete e le risposte non valide si ritentano; poi `ErroreModello`. Un'inquadratura che fallisce viene saltata e segnalata a fine lavoro; dopo tre di fila l'analisi si ferma (il modello non risponde più).
- Gli aggiornamenti dei modelli MLX si controllano con l'impronta del registro ricordata al download (`modelli_scaricati` in `aggiornamenti.json` dei dati), perché Ollama li converte e l'impronta locale è diversa.

## Insieme a Murnau e Ozu

Vertov usa Gemma 4, come Ozu e come Murnau per le verifiche, con impostazioni diverse: se lavorano insieme, Ollama deve ricaricarlo (16 GB) a ogni cambio. Le regole stanno in `coordinamento.py`:

- **Precedenza a Vertov**: Murnau e Ozu riconoscono un'analisi di Vertov dal nome del programma, `descrivi.py`, eseguito da Python con o senza percorso (da Vertov.app, dal codice sorgente o dalla sua cartella), e aspettano; Ozu chiede anche al server di Vertov se un'analisi è in corso. Non rinominare `descrivi.py` senza aggiornare anche loro (`ALTRI_PROGRAMMI` in murnau/coordinamento.py, `PROGRAMMI_DI_ANALISI` in ozu/motore_locale.py). Allo stesso modo, Vertov riconosce le proprie analisi da `descrivi.py` (`coordinamento._esegue`), e le app sorelle da `murnau.py` e `traduci.py`.
- **Un solo contesto**: ogni richiesta di Vertov (descrizioni, volti, report) usa lo stesso contesto (`CONTESTO`, 16384), così Vertov non costringe Ollama a ricaricare il modello nemmeno da solo.
- **Una sola analisi di Vertov alla volta**; il modello resta in memoria 5 minuti (`TENUTA`) e a fine analisi viene tolto se è la copia di Vertov.
- **Chiusura vera**: Ferma (e la chiusura della finestra) manda SIGTERM a tutta l'analisi e, dopo 5 secondi, SIGKILL. Un'analisi lanciata dall'app controlla ogni secondo che il server che l'ha lanciata esista ancora (`VERTOV_APP_PID`; `VERTOV_APP` invece è il percorso di Vertov.app, da riaprire dopo un aggiornamento) e altrimenti si spegne.
- **Registro**: ogni analisi scrive `~/Library/Application Support/Vertov/lavori/<pid>.json`; l'app mostra quelle partite da Terminale con un pulsante Ferma.

## Aggiornamenti e spazio

- `aggiornamenti.py` controlla Vertov (annuncio all'indirizzo in `aggiornamenti.json`), ffmpeg e Ollama (Homebrew), le librerie Python dell'ambiente e i modelli (Ollama, Hugging Face). L'app chiede prima di installare; le librerie tornano alla versione precedente se non si caricano più. Il controllo all'avvio si spegne con **Controlla all'avvio** o in *Impostazioni*.
- Un aggiornamento di Vertov scarica Vertov.app nuova completa (controllata con SHA-256), la prepara accanto a quella installata e le scambia con `os.replace`: il Vertov aperto continua a girare fino al riavvio, che avviene da solo, e i file di un'app in uso non vengono mai riscritti. La versione di prima va in `~/Library/Application Support/Vertov/versioni_precedenti/`, per poter tornare indietro. Dal codice sorgente gli aggiornamenti di Vertov non si installano: lì si usano git e `mac/compila.sh --installa`.
- Durante il lavoro Hugging Face resta fuori linea (`HF_HUB_OFFLINE`, impostato in `audio.py` quando Whisper e AST sono già sul Mac): solo Aggiornamenti… lo contatta, e senza statistiche d'uso.
- Vertov è pubblicato su GitHub (https://github.com/FredFusco/vertov): ogni versione è una release, e `aggiornamenti.json` punta a `releases/latest/download/vertov.json`.
- Per pubblicare una versione nuova: `git commit` e `git push`, poi `python prepara_aggiornamento.py 1.1.0 "Novità…" --pubblica`. Scrive VERSIONE, costruisce Vertov.app con `mac/compila.sh`, crea in `dist/` il pacchetto (lo zip con dentro l'app completa) e l'annuncio e li carica come release. Per avere la versione nuova anche su questo Mac: `mac/compila.sh --installa`.
- Su GitHub finiscono solo i file del programma ammessi da `.gitignore`: mai film, audio, documenti o risultati.
- `pulizia.py` misura e cancella fotogrammi temporanei, registri, cache di pip e modelli non usati (File › Libera spazio…). Non propone mai i modelli di Ollama delle app sorelle: li legge dalle costanti (`MODELLO = "…"`, `REVISORE = "…"`) del loro codice sorgente in `My Drive/CLAUDE` e del programma dentro le app installate in `~/Applications`.
- I test stanno in `tests/`: `~/.venvs/descrittore/bin/python -m unittest discover -s tests -v` (scelte dei file dall'app, `--servizio`, scambio dell'app durante un aggiornamento, riconoscimento delle analisi, modelli protetti).
