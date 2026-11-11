// Lineup.app - a chrome-less, rounded, always-on-top window showing your agents surfing.
// It runs lineup.py in `stream` mode and draws its frames natively.
//
// Keys:  q quit · i info · m reduced motion · c compact · [ ] text size · b bell · t pin · - / = transparency
// Mouse: click a surfer for its card (question, latest result, changed files) · drag (or shake!) to move
import AppKit

let script: String = {
    // prefer the copy the installer put in ~/.lineup (same one the hooks use), else the bundled one
    let home = ProcessInfo.processInfo.environment["LINEUP_HOME"]
        ?? (FileManager.default.homeDirectoryForCurrentUser.path + "/.lineup")
    let installed = home + "/lineup.py"
    if FileManager.default.fileExists(atPath: installed) { return installed }
    return Bundle.main.path(forResource: "lineup", ofType: "py") ?? installed
}()
let pythonCandidates = [ProcessInfo.processInfo.environment["LINEUP_PYTHON"] ?? "",
                        "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].filter { !$0.isEmpty }
let pad: CGFloat = 14

// MARK: - ANSI frame -> attributed rows (only the subset lineup.py emits)

struct Frame {
    var rows: [Int: NSAttributedString] = [:]
    var bell = false
}

final class Parser {
    var font: NSFont
    var bold: NSFont
    init(font: NSFont, bold: NSFont) { self.font = font; self.bold = bold }

    func parse(_ s: String) -> Frame {
        var frame = Frame()
        var row = -1
        var line = NSMutableAttributedString()
        var run = ""
        var color = NSColor.white
        var isBold = false

        func flushRun() {
            guard !run.isEmpty else { return }
            line.append(NSAttributedString(string: run, attributes: [
                .font: isBold ? bold : font, .foregroundColor: color,
            ]))
            run = ""
        }
        func flushRow() {
            flushRun()
            if row >= 0 { frame.rows[row] = line }
            line = NSMutableAttributedString()
        }

        let u = Array(s.unicodeScalars)
        var i = 0
        while i < u.count {
            let c = u[i]
            if c == "\u{1b}", i + 1 < u.count, u[i + 1] == "[" {
                var j = i + 2
                var params = ""
                while j < u.count, !(u[j].value >= 0x40 && u[j].value <= 0x7e) {
                    params.unicodeScalars.append(u[j]); j += 1
                }
                guard j < u.count else { break }
                let cmd = u[j]
                if cmd == "H" {
                    flushRow()
                    row = (Int(params.split(separator: ";").first ?? "1") ?? 1) - 1
                } else if cmd == "m" {
                    let p = params.split(separator: ";").map { Int($0) ?? 0 }
                    var newColor = color, newBold = isBold
                    var k = 0
                    while k < p.count {
                        switch p[k] {
                        case 0: newBold = false; newColor = .white
                        case 1: newBold = true
                        case 22: newBold = false
                        case 38 where k + 4 < p.count && p[k + 1] == 2:
                            newColor = NSColor(srgbRed: CGFloat(p[k + 2]) / 255, green: CGFloat(p[k + 3]) / 255,
                                               blue: CGFloat(p[k + 4]) / 255, alpha: 1)
                            k += 4
                        case 48 where k + 4 < p.count && p[k + 1] == 2: k += 4
                        default: break
                        }
                        k += 1
                    }
                    if newColor != color || newBold != isBold { flushRun(); color = newColor; isBold = newBold }
                }
                i = j + 1
                continue
            }
            if c == "\u{07}" { frame.bell = true } else { run.unicodeScalars.append(c) }
            i += 1
        }
        flushRow()
        return frame
    }
}

// MARK: - the view

final class OceanView: NSView {
    var fontSize: CGFloat = 12
    var font = NSFont(name: "Menlo", size: 12) ?? .monospacedSystemFont(ofSize: 12, weight: .regular)
    var boldFont = NSFont(name: "Menlo-Bold", size: 12) ?? .monospacedSystemFont(ofSize: 12, weight: .bold)
    var cellW: CGFloat = 7.22
    var lineH: CGFloat = 15

    /// Smaller letters = more ocean in the same window. Re-sends the grid size to the engine.
    func setFontSize(_ size: CGFloat) {
        fontSize = size
        font = NSFont(name: "Menlo", size: size) ?? .monospacedSystemFont(ofSize: size, weight: .regular)
        boldFont = NSFont(name: "Menlo-Bold", size: size) ?? .monospacedSystemFont(ofSize: size, weight: .bold)
        cellW = ("M" as NSString).size(withAttributes: [.font: font]).width
        lineH = ceil(NSLayoutManager().defaultLineHeight(for: font))
        rows = [:]
        lastSize = (0, 0)
        setFrameSize(frame.size)
        needsDisplay = true
    }
    var rows: [Int: NSAttributedString] = [:]
    var onResize: ((Int, Int) -> Void)?
    var onKey: ((String) -> Void)?
    var lastSize = (0, 0)
    var bgAlpha: CGFloat = 0.8
    var onClick: ((Int, Int) -> Void)?
    var statusRow = -1
    var statusTip = ""
    private var dragging = false

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override var mouseDownCanMoveWindow: Bool { false }      // we decide: click or drag

    func cell(at p: NSPoint) -> (Int, Int) {
        (Int(floor((p.x - pad) / cellW)), Int(floor((p.y - pad) / lineH)))
    }

    override func mouseDown(with event: NSEvent) { dragging = false }

    override func mouseDragged(with event: NSEvent) {
        if !dragging {
            dragging = true
            window?.performDrag(with: event)                  // native window drag (also shakes the sea)
        }
    }

    override func mouseUp(with event: NSEvent) {
        if !dragging {
            let (c, r) = cell(at: convert(event.locationInWindow, from: nil))
            onClick?(c, r)
        }
        dragging = false
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseMoved, .activeAlways, .inVisibleRect],
                                       owner: self, userInfo: nil))
    }

    override func mouseMoved(with event: NSEvent) {
        let (_, r) = cell(at: convert(event.locationInWindow, from: nil))
        let tip: String? = (r == statusRow && !statusTip.isEmpty) ? statusTip : nil
        if toolTip != tip { toolTip = tip }
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.withAlphaComponent(bgAlpha).setFill()
        bounds.fill(using: .copy)   // semi-transparent backdrop; the ocean itself stays solid
        for (r, s) in rows {
            s.draw(at: NSPoint(x: pad, y: pad + CGFloat(r) * lineH))
        }
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        let cols = max(10, Int((newSize.width - 2 * pad) / cellW))
        let lines = max(5, Int((newSize.height - 2 * pad) / lineH))
        if (cols, lines) != lastSize {
            lastSize = (cols, lines)
            onResize?(cols, lines)
        }
    }

    override func keyDown(with event: NSEvent) {
        onKey?(event.charactersIgnoringModifiers?.lowercased() ?? "")
    }
}

// MARK: - app

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let view = OceanView()
    var proc: Process?
    var stdinPipe: Pipe?
    var buffer = Data()
    let ioQueue = DispatchQueue(label: "lineup.io")
    lazy var parser = Parser(font: view.font, bold: view.boldFont)
    let prefs = UserDefaults.standard
    lazy var pinned: Bool = prefs.object(forKey: "pinned") as? Bool ?? true
    lazy var opacity: Double = prefs.object(forKey: "opacity") as? Double ?? 0.8
    lazy var compact: Bool = prefs.bool(forKey: "compact")
    var fontKey: String { compact ? "fontSizeCompact" : "fontSize" }
    func savedFontSize() -> CGFloat {
        CGFloat(prefs.object(forKey: fontKey) as? Double ?? (compact ? 9 : 12))
    }
    lazy var reduceMotionPref: Bool = prefs.bool(forKey: "reduceMotion")
    let demo = CommandLine.arguments.contains("--demo")
    var agentSpots: [(id: String, x: Int, y: Int)] = []
    let popover = NSPopover()

    var reduceMotion: Bool {
        reduceMotionPref || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.title = "Lineup"
        for b in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            window.standardWindowButton(b)?.isHidden = true
        }
        window.isMovableByWindowBackground = false
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        view.setFontSize(savedFontSize())
        parser.font = view.font
        parser.bold = view.boldFont
        window.contentView = view
        window.setFrameAutosaveName("LineupWindow")
        if !window.setFrameUsingName("LineupWindow") { window.center() }
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(view)

        view.bgAlpha = CGFloat(opacity)
        applyPin()
        // shaking / dragging the window sloshes the sea (sent as cols/rows moved)
        lastOrigin = window.frame.origin
        NotificationCenter.default.addObserver(forName: NSWindow.didMoveNotification, object: window,
                                               queue: .main) { [weak self] _ in self?.windowMoved() }
        view.onResize = { [weak self] c, r in self?.send("size \(c) \(r)") }
        view.onKey = { [weak self] k in self?.handleKey(k) }
        view.onClick = { [weak self] c, r in self?.clicked(c, r) }
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.sendMotion() }
        startEngine()
        NSApp.activate(ignoringOtherApps: true)

        if let i = CommandLine.arguments.firstIndex(of: "--card"), i + 1 < CommandLine.arguments.count {
            let id = CommandLine.arguments[i + 1]
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.send("detail \(id)") }
        }
        // hidden self-test: --snapshot <png> renders the view after a few seconds and quits
        if let i = CommandLine.arguments.firstIndex(of: "--snapshot"), i + 1 < CommandLine.arguments.count {
            let path = CommandLine.arguments[i + 1]
            DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
                if let rep = self.view.bitmapImageRepForCachingDisplay(in: self.view.bounds) {
                    self.view.cacheDisplay(in: self.view.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
                }
                NSApp.terminate(nil)
            }
        }
    }

    func handleKey(_ k: String) {
        switch k {
        case "q": NSApp.terminate(nil)
        case "b": send("bell")
        case "i": send("info")
        case "m":
            reduceMotionPref.toggle()
            prefs.set(reduceMotionPref, forKey: "reduceMotion")
            sendMotion()
        case "c": toggleCompact()
        case "t":
            pinned.toggle()
            prefs.set(pinned, forKey: "pinned")
            applyPin()
        case "[": changeFontSize(-1)
        case "]": changeFontSize(+1)
        case "-", "_":
            setOpacity(opacity - 0.05)
        case "=", "+":
            setOpacity(opacity + 0.05)
        default: break
        }
    }

    var lastOrigin = NSPoint.zero

    func sendMotion() { send("motion \(reduceMotion ? "reduced" : "full")") }

    func toggleCompact() {
        compact.toggle()
        prefs.set(compact, forKey: "compact")
        applyFontSize(savedFontSize())
        var f = window.frame
        let top = f.maxY
        if compact {
            prefs.set(NSStringFromRect(f), forKey: "fullFrame")
            f.size = NSSize(width: 64 * view.cellW + 2 * pad, height: 12 * view.lineH + 2 * pad)
        } else if let s = prefs.string(forKey: "fullFrame") {
            f.size = NSRectFromString(s).size
        }
        f.origin.y = top - f.height                         // keep the top edge where it was
        window.setFrame(f, display: true, animate: !reduceMotion)
        send("compact \(compact ? "on" : "off")")
    }

    func clicked(_ c: Int, _ r: Int) {
        // a surfer's spot is its head; the body + board sit below it
        let hit = agentSpots.min { abs($0.x - c) + abs($0.y - r) < abs($1.x - c) + abs($1.y - r) }
        if let a = hit, abs(a.x - c) <= 5, r >= a.y - 2, r <= a.y + 5 {
            send("detail \(a.id)")
        } else if popover.isShown {
            popover.performClose(nil)
        }
    }

    func showCard(_ d: [String: Any]) {
        let name = d["name"] as? String ?? "agent"
        let mood = (d["mood"] as? String ?? "").replacingOccurrences(of: "_", with: " ")
        let board = NSColor(hex: d["board"] as? String ?? "#ff8c14")
        let body = NSMutableAttributedString()
        func add(_ s: String, _ font: NSFont, _ color: NSColor = .labelColor, gap: CGFloat = 0) {
            // `gap` = space above a section header; body lines stay tight
            let ps = NSMutableParagraphStyle(); ps.paragraphSpacingBefore = gap; ps.paragraphSpacing = 2
            body.append(NSAttributedString(string: s + "\n", attributes: [.font: font, .foregroundColor: color, .paragraphStyle: ps]))
        }
        let small = NSFont.systemFont(ofSize: 11, weight: .semibold)
        if let q = d["question"] as? String, !q.isEmpty {
            add("NEEDS YOUR ANSWER", small, .systemOrange)
            add(q, .systemFont(ofSize: 13, weight: .medium))
        }
        let result = (d["result"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        add("LATEST RESULT", small, .secondaryLabelColor, gap: body.length > 0 ? 10 : 0)
        add(result.isEmpty ? "Nothing yet." : result.replacingOccurrences(of: "\n\n", with: "\n"), .systemFont(ofSize: 12))
        if let files = d["files"] as? [String], !files.isEmpty {
            add("CHANGED FILES", small, .secondaryLabelColor, gap: 10)
            for f in files.reversed() {
                add("• " + (f as NSString).lastPathComponent, .monospacedSystemFont(ofSize: 11, weight: .regular))
            }
        }

        let vc = NSViewController()
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 8
        root.edgeInsets = NSEdgeInsets(top: 14, left: 16, bottom: 14, right: 16)

        let title = NSTextField(labelWithString: name)
        title.font = .systemFont(ofSize: 16, weight: .bold)
        title.textColor = board
        let sub = NSTextField(labelWithString: "\(mood) · \(d["title"] as? String ?? "-")")
        sub.font = .systemFont(ofSize: 11)
        sub.textColor = .secondaryLabelColor
        sub.toolTip = d["cwd"] as? String
        root.addArrangedSubview(title)
        root.addArrangedSubview(sub)

        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 340, height: 220))
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        let tv = NSTextView(frame: scroll.bounds)
        tv.isEditable = false
        tv.drawsBackground = false
        tv.textContainerInset = .zero
        tv.textContainer?.lineFragmentPadding = 0          // line up with the title
        tv.textStorage?.setAttributedString(body)
        tv.autoresizingMask = [.width]
        scroll.documentView = tv
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.widthAnchor.constraint(equalToConstant: 340).isActive = true
        root.translatesAutoresizingMaskIntoConstraints = false
        root.widthAnchor.constraint(equalToConstant: 372).isActive = true   // 340 + insets
        let used = tv.layoutManager.map { lm -> CGFloat in
            lm.ensureLayout(for: tv.textContainer!); return lm.usedRect(for: tv.textContainer!).height
        } ?? 220
        scroll.heightAnchor.constraint(equalToConstant: min(260, max(40, used + 6))).isActive = true
        root.addArrangedSubview(scroll)

        let buttons = NSStackView()
        buttons.orientation = .horizontal
        let cwd = d["cwd"] as? String ?? ""
        let open = NSButton(title: "Open project", target: nil, action: nil)
        open.isEnabled = !cwd.isEmpty
        open.onClick { NSWorkspace.shared.open(URL(fileURLWithPath: cwd)) }
        let copy = NSButton(title: "Copy result", target: nil, action: nil)
        copy.isEnabled = !result.isEmpty
        copy.onClick {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(result, forType: .string)
        }
        buttons.addArrangedSubview(open)
        buttons.addArrangedSubview(copy)
        root.addArrangedSubview(buttons)
        vc.view = root

        popover.contentViewController = vc
        popover.behavior = .transient
        popover.animates = !reduceMotion
        let id = d["id"] as? String ?? ""
        let spot = agentSpots.first { $0.id == id } ?? (id: id, x: 10, y: 10)
        let anchor = NSRect(x: pad + CGFloat(spot.x - 2) * view.cellW, y: pad + CGFloat(spot.y) * view.lineH,
                            width: 5 * view.cellW, height: 4 * view.lineH)
        popover.show(relativeTo: anchor, of: view, preferredEdge: .maxY)
        if let out = ProcessInfo.processInfo.environment["LINEUP_CARD_SNAPSHOT"] {   // self-test hook
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                let v = vc.view
                if let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) {
                    v.cacheDisplay(in: v.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: out))
                }
            }
        }
    }

    func windowMoved() {
        let o = window.frame.origin
        let dx = (o.x - lastOrigin.x) / view.cellW
        let dy = -(o.y - lastOrigin.y) / view.lineH     // screen y points up, rows point down
        lastOrigin = o
        if abs(dx) + abs(dy) > 0.05, abs(dx) < 400, abs(dy) < 400 {
            send(String(format: "shake %.2f %.2f", dx, dy))
        }
    }

    func changeFontSize(_ delta: CGFloat) {
        let size = min(18, max(7, view.fontSize + delta))
        guard size != view.fontSize else { return }
        prefs.set(Double(size), forKey: fontKey)
        applyFontSize(size)
    }

    func applyFontSize(_ size: CGFloat) {
        view.setFontSize(size)
        let f = view.font, b = view.boldFont
        ioQueue.async { self.parser.font = f; self.parser.bold = b }   // parser lives on the io queue
    }

    func applyPin() {
        window.level = pinned ? .floating : .normal
        window.collectionBehavior = pinned ? [.canJoinAllSpaces, .fullScreenAuxiliary] : []
        send("pin \(pinned ? "on" : "off")")
    }

    func setOpacity(_ v: Double) {
        opacity = min(1.0, max(0.3, (v * 20).rounded() / 20))
        prefs.set(opacity, forKey: "opacity")
        view.bgAlpha = CGFloat(opacity)
        view.needsDisplay = true
    }

    func startEngine() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonCandidates.first { FileManager.default.isExecutableFile(atPath: $0) }
                              ?? "/usr/bin/python3")
        p.arguments = [script, "stream"] + (demo ? ["--demo"] : [])
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        let inP = Pipe(), outP = Pipe()
        p.standardInput = inP
        p.standardOutput = outP
        p.standardError = FileHandle.nullDevice
        outP.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard let self, !d.isEmpty else { return }
            self.ioQueue.async { self.consume(d) }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {   // engine died: bring it back
                guard let self, NSApp.isRunning else { return }
                self.startEngine()
            }
        }
        do { try p.run() } catch { NSLog("Lineup: failed to start python3: \(error)"); return }
        proc = p
        stdinPipe = inP
        let (c, r) = view.lastSize
        if c > 0 { send("size \(c) \(r)") }
        if pinned { send("pin on") }
        sendMotion()
        if compact { send("compact on") }
    }

    func consume(_ d: Data) {
        buffer.append(d)
        guard let last = buffer.lastIndex(of: 0x0C) else { return }
        let upto = buffer[buffer.startIndex..<last]
        let start = upto.lastIndex(of: 0x0C).map { $0 + 1 } ?? upto.startIndex
        let frameData = buffer[start..<last]
        buffer = Data(buffer[(last + 1)...])
        var text = String(decoding: frameData, as: UTF8.self)
        var meta: [String: Any]? = nil
        if let a = text.firstIndex(of: "\u{1d}"), let b = text.firstIndex(of: "\u{1e}"), a < b {
            let json = String(text[text.index(after: a)..<b])
            meta = (try? JSONSerialization.jsonObject(with: Data(json.utf8))) as? [String: Any]
            text.removeSubrange(a...b)
        }
        let frame = parser.parse(text)
        DispatchQueue.main.async {
            if let m = meta {
                self.agentSpots = (m["agents"] as? [[String: Any]] ?? []).compactMap {
                    guard let id = $0["id"] as? String, let x = $0["x"] as? Int, let y = $0["y"] as? Int else { return nil }
                    return (id: id, x: x, y: y)
                }
                self.view.statusRow = m["status_row"] as? Int ?? -1
                self.view.statusTip = m["path"] as? String ?? ""
                if let d = m["detail"] as? [String: Any] { self.showCard(d) }
            }
            for (r, s) in frame.rows { self.view.rows[r] = s }
            self.view.rows = self.view.rows.filter { $0.key < self.view.lastSize.1 }
            self.view.needsDisplay = true
            if frame.bell { NSSound(named: "Glass")?.play() }
        }
    }

    func send(_ line: String) {
        guard let h = stdinPipe?.fileHandleForWriting else { return }
        try? h.write(contentsOf: (line + "\n").data(using: .utf8)!)
    }

    func applicationWillTerminate(_ n: Notification) {
        proc?.terminationHandler = nil
        try? stdinPipe?.fileHandleForWriting.close()
        proc?.terminate()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Lineup", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = main
    }
}

extension NSColor {
    convenience init(hex: String) {
        var v: UInt64 = 0
        Scanner(string: hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))).scanHexInt64(&v)
        self.init(srgbRed: CGFloat((v >> 16) & 255) / 255, green: CGFloat((v >> 8) & 255) / 255,
                  blue: CGFloat(v & 255) / 255, alpha: 1)
    }
}

private var clickHandlers: [ObjectIdentifier: () -> Void] = [:]
extension NSButton {
    func onClick(_ f: @escaping () -> Void) {
        clickHandlers[ObjectIdentifier(self)] = f
        target = self
        action = #selector(runClick)
    }
    @objc private func runClick() { clickHandlers[ObjectIdentifier(self)]?() }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
