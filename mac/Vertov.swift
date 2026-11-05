// Vertov.app: la finestra nativa per Mac di Vertov.
//
// Il motore resta in Python: questa app avvia `app.py --servizio` (il server dell'interfaccia, con le analisi,
// i volti, il report, l'esportazione e gli aggiornamenti) e mostra l'interfaccia in una finestra WebKit. Del Mac
// si occupa lei: si apre subito, ha menu e scorciatoie native, apre le finestre di scelta dei file, riceve i
// materiali trascinati sulla finestra o sull'icona nel Dock e quelli aperti dal Finder con «Apri con», mostra la
// percentuale dell'analisi sull'icona nel Dock, apre il report e la guida in finestre proprie, chiede conferma
// all'uscita con il logo di Vertov e chiude il server quando si esce.
//
// Si compila con mac/compila.sh (bastano gli strumenti da riga di comando di Xcode).

import AppKit
import WebKit

let PORTA = 8765  // la stessa di app.py (Murnau usa 8766, Ozu 8767)
let INDIRIZZO = URL(string: "http://127.0.0.1:\(PORTA)/")!
let CODICE_RIAVVIO: Int32 = 75  // app.py --servizio dopo un aggiornamento: ci si chiude senza chiedere
let CODICE_PORTA_OCCUPATA: Int32 = 3  // c'è già un altro Vertov aperto (per esempio dal Terminale)

let CASA = FileManager.default.homeDirectoryForCurrentUser
let DATI = CASA.appendingPathComponent("Library/Application Support/Vertov")
// Dove sta il programma (app.py e il resto): dentro l'app, in Contents/Resources/programma (così Vertov.app è
// completa e sta da sola nella cartella Applicazioni); oppure accanto all'app, per provare il codice sorgente.
let CARTELLA = trovaProgramma()
// Il registro di sempre (app.py e Libera spazio lo conoscono già): l'app e il motore ci scrivono insieme.
let REGISTRO = CASA.appendingPathComponent(".cache/descrittore/vertov.log")
let PREFERENZE = DATI.appendingPathComponent("preferenze.json")
// L'ambiente Python di Vertov (con PyTorch, OpenCV, mlx-whisper), condiviso con Murnau: fuori dall'app.
let AMBIENTE = CASA.appendingPathComponent(".venvs/descrittore")
let PYTHON = AMBIENTE.appendingPathComponent("bin/python")

func trovaProgramma() -> URL {
    let dentro = Bundle.main.resourceURL!.appendingPathComponent("programma")
    if FileManager.default.fileExists(atPath: dentro.appendingPathComponent("app.py").path) { return dentro }
    return Bundle.main.bundleURL.deletingLastPathComponent()
}

// I colori della carta dell'interfaccia (--carta in interfaccia.html), chiaro e scuro.
let CARTA = NSColor(name: nil) { aspetto in
    aspetto.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        ? NSColor(srgbRed: 0x1f / 255, green: 0x1e / 255, blue: 0x25 / 255, alpha: 1)
        : NSColor(srgbRed: 0xf4 / 255, green: 0xed / 255, blue: 0xe0 / 255, alpha: 1)
}

/// Il registro di Vertov aperto in aggiunta: l'app e il motore ci scrivono insieme senza sovrapporsi.
func registroInAggiunta() -> FileHandle? {
    try? FileManager.default.createDirectory(at: REGISTRO.deletingLastPathComponent(), withIntermediateDirectories: true)
    let descrittore = open(REGISTRO.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
    return descrittore < 0 ? nil : FileHandle(fileDescriptor: descrittore, closeOnDealloc: true)
}

func annota(_ testo: String) {
    let riga = "\(Date().formatted(date: .omitted, time: .standard)) Vertov.app: \(testo)\n"
    if let maniglia = registroInAggiunta() {
        maniglia.write(riga.data(using: .utf8)!)
        try? maniglia.close()
    }
}

// Le scelte dell'interfaccia, in comune con app.py (leggi_preferenze, scrivi_preferenza).
func leggiPreferenze() -> [String: Any] {
    guard let dati = try? Data(contentsOf: PREFERENZE),
          let valori = try? JSONSerialization.jsonObject(with: dati) as? [String: Any] else { return [:] }
    return valori
}

func scriviPreferenza(_ chiave: String, _ valore: Any) {
    var valori = leggiPreferenze()
    valori[chiave] = valore
    try? FileManager.default.createDirectory(at: DATI, withIntermediateDirectories: true)
    if let dati = try? JSONSerialization.data(withJSONObject: valori, options: [.prettyPrinted, .sortedKeys]) {
        try? dati.write(to: PREFERENZE, options: .atomic)
    }
}

func versione() -> String {
    (try? String(contentsOf: CARTELLA.appendingPathComponent("VERSIONE"), encoding: .utf8))?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? "?"
}

func iconaVertov() -> NSImage? {
    NSImage(contentsOf: CARTELLA.appendingPathComponent("icona_vertov.png")) ?? NSApp.applicationIconImage
}

/// Lo stato dell'elaborazione chiesto al server (nil se non risponde entro un secondo e mezzo).
func statoLavoro(_ fatto: @escaping ([String: Any]?) -> Void) {
    var richiesta = URLRequest(url: INDIRIZZO.appendingPathComponent("api/stato"))
    richiesta.timeoutInterval = 1.5
    URLSession.shared.dataTask(with: richiesta) { dati, _, _ in
        let stato = dati.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        DispatchQueue.main.async { fatto(stato?["lavoro"] as? [String: Any]) }
    }.resume()
}

// --------------------------------------------------------------------------
// Il motore: app.py --servizio
// --------------------------------------------------------------------------

final class Motore {
    private var processo: Process?
    var finito: ((Int32) -> Void)?

    /// L'ambiente Python c'è già? Si guarda solo se esistono Python e le librerie principali (istantaneo).
    var ambientePronto: Bool {
        guard FileManager.default.isExecutableFile(atPath: PYTHON.path) else { return false }
        let lib = AMBIENTE.appendingPathComponent("lib")
        let versioni = (try? FileManager.default.contentsOfDirectory(atPath: lib.path)) ?? []
        return versioni.contains { versione in
            ["torch", "transformers", "cv2", "mlx_whisper", "aaf2", "pypdf"].allSatisfy { FileManager.default.fileExists(
                atPath: lib.appendingPathComponent("\(versione)/site-packages/\($0)").path) }
        }
    }

    /// Primo avvio su un Mac nuovo: prepara l'ambiente Python (lo stesso di Murnau, se c'è già lo si completa).
    func preparaAmbiente(_ fatto: @escaping (Bool) -> Void) {
        annota("primo avvio: preparo l'ambiente Python in \(AMBIENTE.path)")
        let comandi = """
        export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
        [ -x "$HOME/.venvs/descrittore/bin/python" ] || /opt/homebrew/bin/python3 -m venv "$HOME/.venvs/descrittore" \
          || python3 -m venv "$HOME/.venvs/descrittore" || exit 1
        "$HOME/.venvs/descrittore/bin/python" -m pip install -q --disable-pip-version-check \
          pywebview pyaaf2 opencv-python numpy scipy pypdf pillow torch transformers huggingface_hub mlx-whisper
        """
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = ["-c", comandi]
        collegaRegistro(p)
        p.terminationHandler = { fine in DispatchQueue.main.async { fatto(fine.terminationStatus == 0) } }
        do { try p.run() } catch { fatto(false) }
    }

    func avvia() {
        let p = Process()
        p.executableURL = PYTHON
        p.arguments = [CARTELLA.appendingPathComponent("app.py").path, "--servizio"]
        p.currentDirectoryURL = CARTELLA
        var ambiente = ProcessInfo.processInfo.environment
        ambiente["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (ambiente["PATH"] ?? "/usr/bin:/bin")
        ambiente["PYTHONUNBUFFERED"] = "1"
        ambiente["HF_HUB_DISABLE_TELEMETRY"] = "1"
        ambiente["VERTOV_APP"] = Bundle.main.bundleURL.path  // dopo un aggiornamento app.py riapre questa stessa app
        // Niente file .pyc scritti dentro l'app: la cambierebbero e ne romperebbero la firma.
        ambiente["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = ambiente
        collegaRegistro(p)
        p.terminationHandler = { [weak self] fine in
            DispatchQueue.main.async {
                guard let self, self.processo === fine else { return }
                self.processo = nil
                self.finito?(fine.terminationStatus)
            }
        }
        do {
            try p.run()
            processo = p
            annota("avviato il motore (app.py --servizio, processo \(p.processIdentifier))")
        } catch {
            annota("non riesco ad avviare il motore: \(error.localizedDescription)")
            finito?(-1)
        }
    }

    /// Ferma il motore (che ferma a sua volta un'eventuale analisi: si potrà riprendere).
    func ferma() {
        guard let p = processo, p.isRunning else { return }
        processo = nil
        p.terminate()  // SIGTERM: app.py ferma l'analisi ed esce
        let limite = Date().addingTimeInterval(5)
        while p.isRunning && Date() < limite { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        annota("motore fermato")
    }

    private func collegaRegistro(_ p: Process) {
        if let maniglia = registroInAggiunta() {
            p.standardOutput = maniglia
            p.standardError = maniglia
        }
    }
}

/// Aspetta che il server risponda (al massimo `secondi`), poi chiama `fatto(true)`; altrimenti `fatto(false)`.
func aspettaServer(secondi: Double, _ fatto: @escaping (Bool) -> Void) {
    let limite = Date().addingTimeInterval(secondi)
    func prova() {
        var richiesta = URLRequest(url: INDIRIZZO.appendingPathComponent("api/sessioni"))
        richiesta.timeoutInterval = 1
        URLSession.shared.dataTask(with: richiesta) { _, risposta, _ in
            DispatchQueue.main.async {
                if (risposta as? HTTPURLResponse)?.statusCode == 200 { fatto(true) }
                else if Date() > limite { fatto(false) }
                else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.1, execute: prova) }
            }
        }.resume()
    }
    prova()
}

// --------------------------------------------------------------------------
// La pagina: WebKit con il trascinamento dei file
// --------------------------------------------------------------------------

final class PaginaVertov: WKWebView {
    var fileLasciati: (([String]) -> Void)?

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let opzioni: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        let file = (sender.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: opzioni) as? [URL]) ?? []
        // Prima la pagina (toglie l'evidenziazione dei riquadri), poi i percorsi completi al server.
        let gestito = super.performDragOperation(sender)
        if !file.isEmpty {
            fileLasciati?(file.map(\.path))
            return true
        }
        return gestito
    }
}

// Nella finestra del report «Salva come PDF / stampa» chiama window.print(), che WebKit da solo ignora:
// lo si passa all'app, che apre la stampa del Mac (da lì anche «Salva come PDF»).
let STAMPA = WKUserScript(source: "window.print = () => window.webkit.messageHandlers.vertov.postMessage({stampa: true});",
                          injectionTime: .atDocumentStart, forMainFrameOnly: true)

// --------------------------------------------------------------------------
// L'app
// --------------------------------------------------------------------------

@MainActor
final class Vertov: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
                    WKScriptMessageHandlerWithReply {
    let motore = Motore()
    var finestra: NSWindow!
    var pagina: PaginaVertov!
    var attesa: NSView!
    var scritta: NSTextField!
    var secondarie: [String: NSWindow] = [:]  // guida e report
    var paginaPronta = false
    var daAggiungere: [String] = []  // file aperti dal Finder prima che la pagina fosse pronta
    var inUscita = false
    var orologioDock: Timer?

    // ----- avvio

    func applicationDidFinishLaunching(_ notification: Notification) {
        annota("avvio, versione \(versione()), da \(Bundle.main.bundleURL.path)")
        NSApp.mainMenu = barraDeiMenu()
        creaFinestra()
        guard FileManager.default.fileExists(atPath: CARTELLA.appendingPathComponent("app.py").path) else {
            errore("Vertov.app è incompleta: manca il programma.",
                   "Reinstalla Vertov (dalla cartella del codice: mac/compila.sh --installa).", soloEsci: true)
            return
        }
        motore.finito = { [weak self] codice in self?.motoreFermato(codice) }
        if motore.ambientePronto {
            avviaMotore()
        } else {
            scritta.stringValue = "Primo avvio: preparo Vertov, ci vuole qualche minuto…"
            motore.preparaAmbiente { [weak self] riuscito in
                guard let self else { return }
                if riuscito { self.avviaMotore() } else {
                    self.errore("Non riesco a preparare l'ambiente Python di Vertov.",
                                "Serve Python 3 (per esempio con Homebrew: brew install python). I dettagli sono nel registro di Vertov.")
                }
            }
        }
    }

    func avviaMotore() {
        scritta.stringValue = "Avvio di Vertov…"
        motore.avvia()
        aspettaServer(secondi: 90) { [weak self] pronto in
            guard let self, !self.inUscita else { return }
            if pronto {
                self.pagina.load(URLRequest(url: INDIRIZZO))
                self.seguiNelDock()
            } else {
                self.errore("Il motore di Vertov non risponde.", "I dettagli sono nel registro di Vertov (Aiuto › Apri il registro di Vertov).")
            }
        }
    }

    func motoreFermato(_ codice: Int32) {
        guard !inUscita else { return }
        annota("il motore si è fermato (codice \(codice))")
        orologioDock?.invalidate()
        NSApp.dockTile.badgeLabel = nil
        if codice == CODICE_RIAVVIO {  // aggiornamento installato: app.py riapre Vertov appena ci chiudiamo
            inUscita = true
            NSApp.terminate(nil)
        } else if codice == CODICE_PORTA_OCCUPATA {
            errore("C'è già un altro Vertov aperto.",
                   "Forse è stato avviato dal Terminale (app.py). Chiudilo e riapri Vertov.", soloEsci: true)
        } else {
            let avviso = NSAlert()
            avviso.icon = iconaVertov()
            avviso.messageText = "Il motore di Vertov si è fermato."
            avviso.informativeText = "Un'analisi in corso riprenderà da dove si era fermata. I dettagli sono nel registro di Vertov."
            avviso.addButton(withTitle: "Riavvia")
            avviso.addButton(withTitle: "Esci da Vertov")
            if avviso.runModal() == .alertFirstButtonReturn {
                paginaPronta = false
                mostraAttesa(true)
                avviaMotore()
            } else {
                inUscita = true
                NSApp.terminate(nil)
            }
        }
    }

    func errore(_ messaggio: String, _ dettaglio: String, soloEsci: Bool = false) {
        let avviso = NSAlert()
        avviso.icon = iconaVertov()
        avviso.messageText = messaggio
        avviso.informativeText = dettaglio
        avviso.addButton(withTitle: "Esci da Vertov")
        if !soloEsci { avviso.addButton(withTitle: "Apri il registro") }
        if avviso.runModal() == .alertSecondButtonReturn { apriRegistro(nil) }
        inUscita = true
        NSApp.terminate(nil)
    }

    /// Durante un'analisi, la percentuale compare sull'icona di Vertov nel Dock.
    func seguiNelDock() {
        orologioDock?.invalidate()
        orologioDock = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
            statoLavoro { lavoro in
                let attivo = lavoro?["attivo"] as? Bool ?? false
                if let percento = lavoro?["percento"] as? Double, attivo {
                    NSApp.dockTile.badgeLabel = "\(Int(percento.rounded()))%"
                } else {
                    NSApp.dockTile.badgeLabel = attivo ? "…" : nil
                }
            }
        }
    }

    // ----- finestra

    func creaFinestra() {
        finestra = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 880),
                            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        finestra.title = "Vertov"
        finestra.minSize = NSSize(width: 720, height: 560)
        finestra.backgroundColor = CARTA
        finestra.tabbingMode = .disallowed
        finestra.isReleasedWhenClosed = false
        finestra.delegate = self
        finestra.center()
        finestra.setFrameAutosaveName("Vertov finestra principale")  // ricorda posizione e misura

        let configurazione = WKWebViewConfiguration()
        configurazione.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "vertov")
        pagina = PaginaVertov(frame: .zero, configuration: configurazione)
        pagina.navigationDelegate = self
        pagina.uiDelegate = self
        pagina.allowsMagnification = false
        pagina.allowsBackForwardNavigationGestures = false
        pagina.setValue(false, forKey: "drawsBackground")  // niente lampo bianco mentre si carica
        pagina.underPageBackgroundColor = CARTA
        pagina.fileLasciati = { [weak self] percorsi in self?.aggiungi(percorsi) }

        // Mentre il motore parte: il logo e una riga di testo, sulla carta di Vertov.
        let logo = NSImageView(image: iconaVertov() ?? NSImage())
        logo.imageScaling = .scaleProportionallyUpOrDown
        logo.widthAnchor.constraint(equalToConstant: 96).isActive = true
        logo.heightAnchor.constraint(equalToConstant: 96).isActive = true
        scritta = NSTextField(labelWithString: "Avvio di Vertov…")
        scritta.font = .systemFont(ofSize: 15, weight: .medium)
        scritta.textColor = .secondaryLabelColor
        let pila = NSStackView(views: [logo, scritta])
        pila.orientation = .vertical
        pila.spacing = 14
        pila.translatesAutoresizingMaskIntoConstraints = false
        attesa = NSView()
        attesa.addSubview(pila)
        NSLayoutConstraint.activate([pila.centerXAnchor.constraint(equalTo: attesa.centerXAnchor),
                                     pila.centerYAnchor.constraint(equalTo: attesa.centerYAnchor)])

        let contenuto = NSView()
        for vista in [pagina!, attesa!] as [NSView] {
            vista.translatesAutoresizingMaskIntoConstraints = false
            contenuto.addSubview(vista)
            NSLayoutConstraint.activate([vista.leadingAnchor.constraint(equalTo: contenuto.leadingAnchor),
                                         vista.trailingAnchor.constraint(equalTo: contenuto.trailingAnchor),
                                         vista.topAnchor.constraint(equalTo: contenuto.topAnchor),
                                         vista.bottomAnchor.constraint(equalTo: contenuto.bottomAnchor)])
        }
        finestra.contentView = contenuto
        mostraAttesa(true)
        finestra.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func mostraAttesa(_ si: Bool) {
        attesa.isHidden = !si
        pagina.isHidden = si
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        finestra.makeKeyAndOrderFront(nil)  // clic sull'icona nel Dock
        return true
    }

    // ----- uscita: il riquadro di Vertov, con il suo logo e «Non mostrare più» (la preferenza è quella di app.py)

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if sender === finestra { NSApp.terminate(nil); return false }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if inUscita { motore.ferma(); return .terminateNow }
        // Prima si chiede al server se c'è un'analisi in corso: in quel caso si chiede sempre conferma,
        // perché chiudere la fermerebbe e un'analisi lanciata per la notte non deve interrompersi per un clic.
        statoLavoro { [weak self] lavoro in
            guard let self else { return }
            let chiudi = self.confermaUscita(analisi: lavoro?["attivo"] as? Bool ?? false)
            if chiudi {
                self.inUscita = true
                self.orologioDock?.invalidate()
                self.motore.ferma()
            }
            NSApp.reply(toApplicationShouldTerminate: chiudi)
        }
        return .terminateLater
    }

    func confermaUscita(analisi: Bool) -> Bool {
        if !analisi && leggiPreferenze()["chiudi_senza_chiedere"] as? Bool ?? false { return true }
        NSApp.activate(ignoringOtherApps: true)
        let avviso = NSAlert()
        avviso.icon = iconaVertov()
        if analisi {
            avviso.messageText = "Chiudere Vertov e fermare l'analisi?"
            avviso.informativeText = "L'elaborazione in corso verrà fermata. Rilanciandola, riprende dal punto in cui si è interrotta."
        } else {
            avviso.messageText = "Chiudere Vertov?"
            avviso.informativeText = "Le sessioni sono già salvate."
            avviso.showsSuppressionButton = true
            avviso.suppressionButton?.title = "Non mostrare più"
        }
        avviso.addButton(withTitle: "Chiudi")
        avviso.addButton(withTitle: "Annulla")
        guard avviso.runModal() == .alertFirstButtonReturn else { return false }
        if !analisi && avviso.suppressionButton?.state == .on { scriviPreferenza("chiudi_senza_chiedere", true) }
        return true
    }

    // ----- file dal Finder e dal Dock («Apri con», trascinamento sull'icona)

    func application(_ application: NSApplication, open urls: [URL]) {
        aggiungi(urls.filter(\.isFileURL).map(\.path))
    }

    func aggiungi(_ percorsi: [String]) {
        guard !percorsi.isEmpty else { return }
        guard paginaPronta else { daAggiungere += percorsi; return }
        if let json = try? JSONSerialization.data(withJSONObject: percorsi),
           let testo = String(data: json, encoding: .utf8) {
            pagina.evaluateJavaScript("aggiungiPercorsi(\(testo))")
        }
        finestra.makeKeyAndOrderFront(nil)
    }

    // ----- messaggi dalla pagina: finestre di scelta dei file (api() in interfaccia.html), report, stampa

    func userContentController(_ controller: WKUserContentController, didReceive messaggio: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard let corpo = messaggio.body as? [String: Any] else { replyHandler(nil, nil); return }
        if corpo["stampa"] != nil, let vista = messaggio.webView {
            stampa(vista)
            replyHandler(nil, nil)
            return
        }
        if let quale = corpo["finestra"] as? String {
            if quale == "report" { apriReport(nil) } else if quale == "guida" { apriGuida(nil) }
            replyHandler(nil, nil)
            return
        }
        guard let scelta = corpo["scegli"] as? [String: Any], let tipo = scelta["tipo"] as? String else {
            replyHandler([String](), nil)
            return
        }
        let cartella = tipo == "cartella" || tipo == "esporta"
        let pannello = NSOpenPanel()
        pannello.canChooseFiles = !cartella
        pannello.canChooseDirectories = cartella
        pannello.allowsMultipleSelection = scelta["multipli"] as? Bool ?? false
        pannello.canCreateDirectories = tipo == "esporta"
        switch tipo {
        case "esporta": pannello.message = "Scegli la cartella in cui esportare marker, foglio di calcolo, report e dialoghi"
        case "cartella": pannello.message = "Scegli una cartella con i materiali del film (video, montaggio, audio, documenti)"
        default: pannello.message = "Scegli i materiali: video, EDL o AAF, tracce audio, sceneggiatura, dialoghi, ordini del giorno"
        }
        pannello.prompt = "Scegli"
        let finestraPagina = messaggio.webView?.window ?? finestra!
        pannello.beginSheetModal(for: finestraPagina) { risposta in
            replyHandler(risposta == .OK ? pannello.urls.map(\.path) : [String](), nil)
        }
    }

    func stampa(_ vista: WKWebView) {
        let info = (NSPrintInfo.shared.copy() as? NSPrintInfo) ?? NSPrintInfo()
        info.horizontalPagination = .fit
        info.verticalPagination = .automatic
        info.isHorizontallyCentered = true
        info.isVerticallyCentered = false
        let operazione = vista.printOperation(with: info)
        operazione.showsPrintPanel = true
        operazione.showsProgressPanel = true
        operazione.view?.frame = vista.bounds  // senza, WebKit stampa pagine bianche
        if let finestraVista = vista.window {
            operazione.runModal(for: finestraVista, delegate: nil, didRun: nil, contextInfo: nil)
        } else {
            operazione.run()
        }
    }

    // ----- navigazione: solo Vertov dentro le sue finestre, il resto nel browser

    func webView(_ webView: WKWebView, decidePolicyFor azione: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = azione.request.url else { decisionHandler(.cancel); return }
        if url.host == "127.0.0.1" && url.port == PORTA || url.scheme == "about" || url.scheme == "data" {
            decisionHandler(.allow)
        } else if url.isFileURL {
            decisionHandler(.cancel)  // un file lasciato fuori dai riquadri: lo aggiunge già performDragOperation
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for azione: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = azione.request.url, !url.isFileURL { NSWorkspace.shared.open(url) }  // link con target="_blank"
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView === pagina else { return }
        paginaPronta = true
        mostraAttesa(false)
        let attesi = daAggiungere
        daAggiungere = []
        aggiungi(attesi)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        annota("pagina non caricata: \(error.localizedDescription)")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        annota("WebKit si è fermato: ricarico la pagina")
        webView.reload()
    }

    // ----- alert(), confirm() e prompt() della pagina, come finestre del Mac

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage messaggio: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let avviso = NSAlert()
        avviso.icon = iconaVertov()
        avviso.messageText = messaggio
        avviso.addButton(withTitle: "OK")
        avviso.beginSheetModal(for: webView.window ?? finestra) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage messaggio: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let avviso = NSAlert()
        avviso.icon = iconaVertov()
        avviso.messageText = messaggio
        avviso.addButton(withTitle: "OK")
        avviso.addButton(withTitle: "Annulla")
        avviso.beginSheetModal(for: webView.window ?? finestra) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt richiesta: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let avviso = NSAlert()
        avviso.icon = iconaVertov()
        avviso.messageText = richiesta
        let campo = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        campo.stringValue = defaultText ?? ""
        avviso.accessoryView = campo
        avviso.addButton(withTitle: "OK")
        avviso.addButton(withTitle: "Annulla")
        avviso.beginSheetModal(for: webView.window ?? finestra) { r in
            completionHandler(r == .alertFirstButtonReturn ? campo.stringValue : nil)
        }
    }

    // ----- menu

    /// Una voce di menu che esegue un comando dell'interfaccia (vertovComando in interfaccia.html).
    func comando(_ titolo: String, _ comando: String, _ tasto: String = "", _ maiuscole: Bool = false) -> NSMenuItem {
        let item = NSMenuItem(title: titolo, action: #selector(eseguiComando(_:)), keyEquivalent: tasto)
        item.representedObject = comando
        item.target = self
        if maiuscole { item.keyEquivalentModifierMask = [.command, .shift] }
        return item
    }

    func voce(_ titolo: String, _ azione: Selector, _ tasto: String = "",
              _ modificatori: NSEvent.ModifierFlags = .command, bersaglio: AnyObject? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: titolo, action: azione, keyEquivalent: tasto)
        item.keyEquivalentModifierMask = modificatori
        item.target = bersaglio
        return item
    }

    func menu(_ titolo: String, _ voci: [NSMenuItem]) -> NSMenuItem {
        let menu = NSMenu(title: titolo)
        voci.forEach(menu.addItem)
        let contenitore = NSMenuItem(title: titolo, action: nil, keyEquivalent: "")
        contenitore.submenu = menu
        return contenitore
    }

    func barraDeiMenu() -> NSMenu {
        let barra = NSMenu()
        let servizi = NSMenuItem(title: "Servizi", action: nil, keyEquivalent: "")
        servizi.submenu = NSMenu(title: "Servizi")
        NSApp.servicesMenu = servizi.submenu
        barra.addItem(menu("Vertov", [
            voce("Informazioni su Vertov", #selector(informazioni(_:)), bersaglio: self), .separator(),
            comando("Impostazioni…", "impostazioni", ","), comando("Controlla aggiornamenti…", "aggiornamenti"),
            .separator(), servizi, .separator(),
            voce("Nascondi Vertov", #selector(NSApplication.hide(_:)), "h"),
            voce("Nascondi altre", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            voce("Mostra tutte", #selector(NSApplication.unhideAllApplications(_:)), "", []), .separator(),
            voce("Esci da Vertov", #selector(NSApplication.terminate(_:)), "q"),
        ]))
        barra.addItem(menu("File", [
            comando("Nuova sessione", "nuova", "n"), comando("Apri sessione…", "apri", "o"),
            comando("Salva sessione", "salva", "s"), comando("Elimina sessione…", "elimina"), .separator(),
            comando("Aggiungi materiali…", "aggiungi-file", "o", true), comando("Aggiungi una cartella…", "aggiungi-cartella"),
            .separator(),
            comando("Apri il report", "report-apri"), comando("Esporta…", "esporta", "e"),
            comando("Mostra i file di lavoro", "lavoro"), .separator(),
            comando("Libera spazio…", "pulizia"), .separator(),
            voce("Chiudi finestra", #selector(NSWindow.performClose(_:)), "w"),
        ]))
        barra.addItem(menu("Modifica", [
            voce("Annulla", Selector(("undo:")), "z"), voce("Ripeti", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            voce("Taglia", #selector(NSText.cut(_:)), "x"), voce("Copia", #selector(NSText.copy(_:)), "c"),
            voce("Incolla", #selector(NSText.paste(_:)), "v"), voce("Seleziona tutto", #selector(NSText.selectAll(_:)), "a"),
        ]))
        barra.addItem(menu("Vista", [
            comando("Materiali", "vista-materiali", "1"), comando("Risultato", "vista-risultato", "2"),
            comando("Esporta", "vista-esporta", "3"), .separator(),
            comando("Controlla i volti…", "volti", "v", true), comando("Dettagli dell'elaborazione", "dettagli"),
            .separator(),
            voce("Attiva modalità a tutto schermo", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ]))
        barra.addItem(menu("Analisi", [
            comando("Descrivi il film", "descrivi", "r"), comando("Ferma", "ferma", "."), .separator(),
            comando("Controlla i volti…", "volti", "v", true),
        ]))
        let finestre = menu("Finestra", [
            voce("Contrai", #selector(NSWindow.performMiniaturize(_:)), "m"),
            voce("Ridimensiona", #selector(NSWindow.performZoom(_:)), "", []), .separator(),
            voce("Porta tutto in primo piano", #selector(NSApplication.arrangeInFront(_:)), "", []),
        ])
        barra.addItem(finestre)
        NSApp.windowsMenu = finestre.submenu
        let aiuto = menu("Aiuto", [
            voce("Guida di Vertov", #selector(apriGuida(_:)), "?", bersaglio: self), .separator(),
            voce("Apri il registro di Vertov", #selector(apriRegistro(_:)), "", [], bersaglio: self),
        ])
        barra.addItem(aiuto)
        NSApp.helpMenu = aiuto.submenu
        return barra
    }

    @objc func eseguiComando(_ item: NSMenuItem) {
        guard paginaPronta, let comando = item.representedObject as? String else { return }
        finestra.makeKeyAndOrderFront(nil)
        pagina.evaluateJavaScript("vertovComando(\(String(reflecting: comando)))")
    }

    @objc func informazioni(_ sender: Any?) {
        NSApp.orderFrontStandardAboutPanel(options: [
            .applicationName: "Vertov", .applicationVersion: versione(), .version: "",
            .applicationIcon: iconaVertov() ?? NSImage(),
            .credits: NSAttributedString(string: "Descrive ogni inquadratura del film, in locale sul Mac.\nTitolo in Rubik Mono One (licenza SIL OFL).",
                                         attributes: [.font: NSFont.systemFont(ofSize: 11)]),
        ])
    }

    /// Una finestra di Vertov per una pagina del server (la guida, il report); se è già aperta la si ricarica.
    func finestraSecondaria(_ chiave: String, titolo: String, percorso: String, larghezza: CGFloat,
                            stampabile: Bool = false) {
        let indirizzo = INDIRIZZO.appendingPathComponent(percorso)
        if let aperta = secondarie[chiave], let vista = aperta.contentView as? WKWebView {
            vista.load(URLRequest(url: indirizzo))
            aperta.makeKeyAndOrderFront(nil)
            return
        }
        let nuova = NSWindow(contentRect: NSRect(x: 0, y: 0, width: larghezza, height: 860),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        nuova.title = titolo
        nuova.backgroundColor = CARTA
        nuova.isReleasedWhenClosed = false
        nuova.tabbingMode = .disallowed
        nuova.center()
        nuova.setFrameAutosaveName("Vertov \(chiave)")
        let configurazione = WKWebViewConfiguration()
        if stampabile {
            configurazione.userContentController.addUserScript(STAMPA)
            configurazione.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "vertov")
        }
        let vista = WKWebView(frame: .zero, configuration: configurazione)
        vista.navigationDelegate = self
        vista.uiDelegate = self
        vista.setValue(false, forKey: "drawsBackground")
        vista.load(URLRequest(url: indirizzo))
        nuova.contentView = vista
        nuova.makeKeyAndOrderFront(nil)
        secondarie[chiave] = nuova
    }

    @objc func apriGuida(_ sender: Any?) {
        finestraSecondaria("guida", titolo: "Guida di Vertov", percorso: "aiuto", larghezza: 820)
    }

    @objc func apriReport(_ sender: Any?) {
        finestraSecondaria("report", titolo: "Report di Vertov", percorso: "report", larghezza: 1000, stampabile: true)
    }

    @objc func apriRegistro(_ sender: Any?) {
        if FileManager.default.fileExists(atPath: REGISTRO.path) {
            NSWorkspace.shared.open([REGISTRO], withApplicationAt: URL(fileURLWithPath: "/System/Applications/Utilities/Console.app"),
                                    configuration: NSWorkspace.OpenConfiguration())
        } else {
            NSWorkspace.shared.open(REGISTRO.deletingLastPathComponent())
        }
    }
}

// Il codice qui sopra gira tutto sul thread principale, come vuole AppKit.
MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegato = Vertov()
    app.delegate = delegato
    app.setActivationPolicy(.regular)
    app.run()
}
