# Descrittore di inquadrature

Descrive ogni inquadratura di un film (immagine e sonoro) usando solo modelli locali e gratuiti:

- **Immagini:** Qwen3-VL 30B tramite Ollama
- **Personaggi:** riconoscimento dei volti dal film stesso (YuNet e SFace di OpenCV), con i nomi dedotti da copione e ordini del giorno
- **Dialoghi:** Whisper large-v3-turbo (mlx-whisper), solo se non fornisci già una trascrizione; le battute vengono ricollegate al copione
- **Effetti e musica:** classificatore AST addestrato su AudioSet (riconosce 527 tipi di suono, compreso il carattere della musica)

Niente esce dal computer.

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
| `--modello` | Un altro modello Ollama (es. `qwen3-vl:8b` per andare più veloce) |

Se interrompi l'elaborazione, rilanciando lo stesso comando riparte da dove si era fermata. Per rifare le descrizioni da capo, cancella `inquadrature.jsonl` dalla cartella dei risultati.

## Risultati (nella cartella `--out`)

- `inquadrature.csv`: si apre in Excel. Una riga per inquadratura con piano, movimento di macchina, personaggi, azione, ambiente, luce, dialoghi, battute del copione, sonoro, suoni rilevati e descrizione
- `inquadrature.jsonl`: gli stessi dati in formato completo, compresi volti riconosciuti e livelli audio
- `marker_resolve.edl`: in DaVinci Resolve, *Timeline > Import > Timeline Markers from EDL* mette ogni descrizione come marker sulla timeline
- `volti/volti.html` e `volti/nomi.txt`: controllo e correzione dei personaggi
- `personaggi.txt`: le schede estratte dalla sceneggiatura, da copiare e correggere per `--personaggi`
- `dialoghi_trascritti.srt`: creato solo con `--trascrivi`

## Tempi

Su M5 Max servono 12–25 secondi per inquadratura, secondo la durata. Per un film di 2.000 inquadrature sono 7–12 ore: conviene lanciarlo di notte. Il passaggio dei volti richiede pochi minuti.

## Insieme a Murnau e Ozu

Le tre app usano lo stesso modello, Qwen3-VL, con impostazioni diverse: se lavorano insieme, Ollama deve ricaricarlo (20 GB) a ogni cambio. Le regole stanno in `coordinamento.py`:

- **Precedenza a Vertov**: Murnau e Ozu riconoscono un'analisi di Vertov dal programma `descrittore_inquadrature/descrivi.py` eseguito da Python, e aspettano. Non rinominare né spostare `descrivi.py` senza aggiornare anche loro.
- **Un solo contesto**: ogni richiesta di Vertov (descrizioni, volti, report) usa lo stesso contesto (`CONTESTO`, 16384), così Vertov non costringe Ollama a ricaricare il modello nemmeno da solo.
- **Una sola analisi di Vertov alla volta**; il modello resta in memoria 5 minuti (`TENUTA`) e a fine analisi viene tolto se è la copia di Vertov.
- **Chiusura vera**: Ferma (e la chiusura della finestra) manda SIGTERM a tutta l'analisi e, dopo 5 secondi, SIGKILL. Un'analisi lanciata dall'app controlla ogni secondo che l'app esista ancora (`VERTOV_APP`) e altrimenti si spegne.
- **Registro**: ogni analisi scrive `~/Library/Application Support/Vertov/lavori/<pid>.json`; l'app mostra quelle partite da Terminale con un pulsante Ferma.

## Aggiornamenti e spazio

- `aggiornamenti.py` controlla Vertov (annuncio all'indirizzo in `aggiornamenti.json`), ffmpeg e Ollama (Homebrew), le librerie Python dell'ambiente e i modelli (Ollama, Hugging Face). L'app chiede prima di installare; le librerie tornano alla versione precedente se non si caricano più.
- Vertov è pubblicato su GitHub (https://github.com/FredFusco/vertov): ogni versione è una release, e `aggiornamenti.json` punta a `releases/latest/download/vertov.json`.
- Per pubblicare una versione nuova: `git commit` e `git push`, poi `python prepara_aggiornamento.py 1.1.0 "Novità…" --pubblica`.
- Su GitHub finiscono solo i file del programma ammessi da `.gitignore`: mai film, audio, documenti o risultati.
- `pulizia.py` misura e cancella fotogrammi temporanei, registri, cache di pip e modelli non usati (File › Libera spazio…).
