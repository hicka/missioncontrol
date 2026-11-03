import AppKit
import SwiftUI

/// Quiet surfaces in the system's own colours, so the app follows light and dark and sits
/// with everything else on the Mac. Status keeps its small palette and always travels with
/// a word, never colour alone.
enum MC {
    static let good = Color(red: 0.18, green: 0.75, blue: 0.38)
    static let warn = Color(red: 0.95, green: 0.69, blue: 0.24)
    static let danger = Color(red: 0.94, green: 0.39, blue: 0.39)
    static let info = Color(red: 0.42, green: 0.65, blue: 1.0)
    static let surface = Color(nsColor: .controlBackgroundColor)
    static let line = Color(nsColor: .separatorColor)

    static func tone(_ status: String) -> Color {
        switch status {
        case "running", "working", "active", "completed", "done", "connected", "enabled", "ok", "approved": return good
        case "waiting", "blocked", "pending", "queued", "paused", "trial", "stalled", "held", "proposed", "suspended": return warn
        case "failed", "error", "denied", "cancelled", "offline": return danger
        default: return .secondary
        }
    }
}

func color(hex: String, fallback: Color = .accentColor) -> Color {
    let digits = hex.trimmingCharacters(in: CharacterSet(charactersIn: "# "))
    guard digits.count == 6, let value = UInt32(digits, radix: 16) else { return fallback }
    return Color(red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255)
}

func ago(_ ts: Double) -> String {
    guard ts > 0 else { return "" }
    let seconds = max(0, Date().timeIntervalSince1970 - ts)
    if seconds < 60 { return "just now" }
    if seconds < 3600 { return "\(Int((seconds / 60).rounded()))m ago" }
    if seconds < 86400 { return "\(Int((seconds / 3600).rounded()))h ago" }
    return "\(Int((seconds / 86400).rounded()))d ago"
}

func plural(_ count: Int, _ one: String, _ many: String? = nil) -> String { "\(count) \(count == 1 ? one : (many ?? one + "s"))" }

func oneLine(_ text: String) -> String { text.split(whereSeparator: \.isWhitespace).joined(separator: " ") }

func clip(_ text: String, _ limit: Int) -> String {
    let line = oneLine(text)
    return line.count > limit ? String(line.prefix(limit - 1)).trimmingCharacters(in: .whitespaces) + "…" : line
}

func percent(_ value: JSON) -> String { value.optNum.map { "\(Int(($0 * 100).rounded()))%" } ?? "—" }

/// A status with its word.
struct Pill: View {
    let status: String
    var label: String?
    var body: some View {
        Text((label ?? status).replacingOccurrences(of: "_", with: " "))
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(MC.tone(status))
            .padding(.horizontal, 8).padding(.vertical, 2.5)
            .background(MC.tone(status).opacity(0.13), in: Capsule())
            .fixedSize()
    }
}

/// An agent's profile: its colour and initial.
struct AgentBadge: View {
    let agent: Agent?
    var size: CGFloat = 26
    var body: some View {
        // A near-black profile colour would vanish on a dark background.
        let raw = agent?.color ?? ""
        let tint = raw.lowercased().hasPrefix("#1d1d") ? Color.primary : color(hex: raw, fallback: .gray)
        Text(String((agent?.name ?? "?").prefix(1)).uppercased())
            .font(.system(size: size * 0.46, weight: .semibold, design: .rounded))
            .foregroundStyle(tint)
            .frame(width: size, height: size)
            .background(tint.opacity(0.16), in: Circle())
            .accessibilityHidden(true)
    }
}

struct Card<Content: View>: View {
    var padding: CGFloat = 0
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 0) { content }
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(MC.surface, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(MC.line.opacity(0.6), lineWidth: 0.5))
    }
}

struct SectionHead<Trailing: View>: View {
    let title: String
    var count: Int?
    @ViewBuilder var trailing: Trailing
    var body: some View {
        HStack(spacing: 8) {
            Text(title).font(.headline)
            if let count { Text("\(count)").font(.subheadline).foregroundStyle(.secondary).monospacedDigit() }
            Spacer()
            trailing
        }
        .padding(.top, 8)
    }
}
extension SectionHead where Trailing == EmptyView {
    init(_ title: String, count: Int? = nil) { self.init(title: title, count: count) { EmptyView() } }
}

struct EmptyNote: View {
    let title: String
    var detail: String?
    var body: some View {
        VStack(spacing: 6) {
            Text(title).font(.headline)
            if let detail { Text(detail).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center) }
        }
        .frame(maxWidth: .infinity).padding(28)
    }
}

/// A scrolling screen of the workspace, with its title and standard margins.
struct Page<Actions: View, Content: View>: View {
    let title: String
    var subtitle: String?
    @ViewBuilder var actions: Actions
    @ViewBuilder var content: Content
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(title).font(.system(size: 24, weight: .semibold))
                        if let subtitle { Text(subtitle).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                    }
                    Spacer()
                    actions
                }
                content
            }
            .padding(.horizontal, 28).padding(.vertical, 22)
            .frame(maxWidth: 980, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
    }
}
extension Page where Actions == EmptyView {
    init(_ title: String, subtitle: String? = nil, @ViewBuilder content: () -> Content) {
        self.init(title: title, subtitle: subtitle, actions: { EmptyView() }, content: content)
    }
}

/// A button for a request: stays pressed while it runs, and reports what went wrong.
struct AsyncButton<Label: View>: View {
    @Environment(Store.self) private var store
    let action: () async throws -> Void
    @ViewBuilder var label: Label
    @State private var running = false
    var body: some View {
        Button {
            guard !running else { return }
            running = true
            Task {
                defer { running = false }
                do { try await action() } catch { store.notify(error.localizedDescription, error: true) }
            }
        } label: { label }
        .disabled(running)
    }
}
extension AsyncButton where Label == Text {
    init(_ title: String, action: @escaping () async throws -> Void) {
        self.init(action: action) { Text(title) }
    }
}

extension View {
    /// Liquid Glass where the system has it, the usual material before that.
    @ViewBuilder func glassBackground(cornerRadius: CGFloat) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            self.glassEffect(.regular, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        } else {
            self.background(.ultraThickMaterial, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        }
        #else
        self.background(.ultraThickMaterial, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        #endif
    }
}

// MARK: Markdown

/// Agent replies are markdown. SwiftUI's own markdown is inline only, so blocks are split
/// here and each is drawn natively, with results kept so a long thread does not reparse.
enum MDBlock: Hashable {
    case paragraph(String), heading(Int, String), bullets([String]), numbered([String]), quote(String), code(String), rule
    case table([String], [[String]])
}

enum Markdown {
    private static let cache: NSCache<NSString, Box> = { let cache = NSCache<NSString, Box>(); cache.countLimit = 800; return cache }()
    private final class Box { let blocks: [MDBlock]; init(_ blocks: [MDBlock]) { self.blocks = blocks } }

    static func blocks(_ text: String) -> [MDBlock] {
        let key = text as NSString
        if let hit = cache.object(forKey: key) { return hit.blocks }
        let parsed = parse(text)
        cache.setObject(Box(parsed), forKey: key)
        return parsed
    }

    static func parse(_ text: String) -> [MDBlock] {
        var blocks: [MDBlock] = [], paragraph: [String] = [], bullets: [String] = [], numbered: [String] = [], quote: [String] = []
        var code: [String]?, table: [String] = []
        func flushTable() {
            guard !table.isEmpty else { return }
            if let parsed = Self.table(table) { blocks.append(parsed) } else { paragraph.append(contentsOf: table) }
            table = []
        }
        func flush() {
            flushTable()
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))); paragraph = [] }
            if !bullets.isEmpty { blocks.append(.bullets(bullets)); bullets = [] }
            if !numbered.isEmpty { blocks.append(.numbered(numbered)); numbered = [] }
            if !quote.isEmpty { blocks.append(.quote(quote.joined(separator: "\n"))); quote = [] }
        }
        for line in text.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if let open = code { blocks.append(.code(open.joined(separator: "\n"))); code = nil }
                else { flush(); code = [] }
                continue
            }
            if code != nil { code?.append(line); continue }
            if trimmed.hasPrefix("|"), trimmed.dropFirst().contains("|") {
                if table.isEmpty { flush() }
                table.append(trimmed); continue
            }
            flushTable()
            if trimmed.isEmpty { flush(); continue }
            if trimmed == "---" || trimmed == "***" { flush(); blocks.append(.rule); continue }
            if trimmed.hasPrefix("#"), let space = trimmed.firstIndex(of: " "), trimmed[..<space].allSatisfy({ $0 == "#" }) {
                flush(); blocks.append(.heading(trimmed.distance(from: trimmed.startIndex, to: space), String(trimmed[trimmed.index(after: space)...]))); continue
            }
            if trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") || trimmed.hasPrefix("• ") { if !paragraph.isEmpty || !numbered.isEmpty || !quote.isEmpty { flush() }; bullets.append(String(trimmed.dropFirst(2))); continue }
            if let dot = trimmed.firstIndex(where: { $0 == "." || $0 == ")" }), dot != trimmed.startIndex, trimmed[..<dot].allSatisfy(\.isNumber), trimmed[trimmed.index(after: dot)...].hasPrefix(" ") {
                if !paragraph.isEmpty || !bullets.isEmpty || !quote.isEmpty { flush() }
                numbered.append(String(trimmed[trimmed.index(dot, offsetBy: 2)...])); continue
            }
            if trimmed.hasPrefix(">") { if !paragraph.isEmpty || !bullets.isEmpty || !numbered.isEmpty { flush() }; quote.append(trimmed.dropFirst().trimmingCharacters(in: .whitespaces)); continue }
            // An indented line carries on the list item above it.
            if line.hasPrefix("  ") {
                if !bullets.isEmpty { bullets[bullets.count - 1] += " " + trimmed; continue }
                if !numbered.isEmpty { numbered[numbered.count - 1] += " " + trimmed; continue }
            }
            if !bullets.isEmpty || !numbered.isEmpty || !quote.isEmpty { flush() }
            paragraph.append(line)
        }
        if let open = code { blocks.append(.code(open.joined(separator: "\n"))) }
        flush()
        return blocks
    }

    /// A pipe table counts only with the line of dashes under its header. Without one, the
    /// lines are left as written.
    private static func table(_ lines: [String]) -> MDBlock? {
        func cells(_ line: String) -> [String] {
            var inner = Substring(line)
            if inner.hasPrefix("|") { inner = inner.dropFirst() }
            if inner.hasSuffix("|") { inner = inner.dropLast() }
            return inner.split(separator: "|", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
        }
        guard lines.count >= 2 else { return nil }
        let rule = cells(lines[1])
        guard !rule.isEmpty, rule.allSatisfy({ cell in cell.count >= 3 && cell.allSatisfy { "-:".contains($0) } }) else { return nil }
        let header = cells(lines[0])
        let rows = lines.dropFirst(2).map { row in
            let values = cells(row)
            return (0..<header.count).map { $0 < values.count ? values[$0] : "" }
        }
        return .table(header, rows)
    }

    static func inline(_ text: String) -> AttributedString {
        var out = (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(text)
        linkAddresses(&out)
        return out
    }

    private static let address = try? NSRegularExpression(pattern: #"https?://[^\s<>"]+"#)

    /// Agents write addresses as plain text. Punctuation after one is not part of it, and
    /// an address inside code stays code. A long one shows as its site and last part, so
    /// a sentence is not mostly address; the link still goes to all of it.
    private static func linkAddresses(_ text: inout AttributedString) {
        let plain = String(text.characters)
        guard plain.contains("://"), let address else { return }
        // From the end, so shortening one does not move the ones before it.
        for match in address.matches(in: plain, range: NSRange(plain.startIndex..., in: plain)).reversed() {
            guard let found = Range(match.range, in: plain) else { continue }
            var written = String(plain[found])
            while let last = written.last, ".,;:!?)]}'\"".contains(last) { written.removeLast() }
            guard !written.isEmpty, let target = URL(string: written) else { continue }
            let start = text.characters.index(text.startIndex, offsetBy: plain.distance(from: plain.startIndex, to: found.lowerBound))
            guard let end = text.characters.index(start, offsetBy: written.count, limitedBy: text.endIndex) else { continue }
            // Code stays as written, and so does a link given words of its own. One the
            // parser linked by itself is just the address, and is shortened like any other.
            let runs = text[start..<end].runs
            let taken = runs.contains { $0.inlinePresentationIntent?.contains(.code) == true || ($0.link != nil && $0.link != target) }
            guard !taken else { continue }
            let label = shortAddress(target)
            if label != written {
                var shown = AttributedString(label)
                shown.mergeAttributes(text[start..<end].runs.first?.attributes ?? AttributeContainer())
                text.replaceSubrange(start..<end, with: shown)
                guard let shownEnd = text.characters.index(start, offsetBy: label.count, limitedBy: text.endIndex) else { continue }
                text[start..<shownEnd].link = target
            } else {
                text[start..<end].link = target
            }
        }
    }

    /// `raw.githubusercontent.com/…/bundledNativeModules.json` for a 90-character address.
    static func shortAddress(_ url: URL) -> String {
        let full = url.absoluteString
        guard full.count > 48, var host = url.host else { return full }
        if host.hasPrefix("www.") { host.removeFirst(4) }
        let parts = url.path.split(separator: "/")
        guard let last = parts.last else { return host }
        return parts.count > 1 ? "\(host)/…/\(last)" : "\(host)/\(last)"
    }
}

struct MarkdownView: View {
    let text: String
    var size: CGFloat = 13
    /// Still being written: the newest words are dim and brighten as more arrive.
    var ink = false
    var body: some View {
        let blocks = Markdown.blocks(text)
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                if ink, index == blocks.count - 1, case .paragraph(let last) = block {
                    Text(Self.inked(Markdown.inline(last))).fixedSize(horizontal: false, vertical: true)
                } else {
                    row(block)
                }
            }
        }
        .font(.system(size: size))
        .textSelection(.enabled)
    }

    private static func inked(_ text: AttributedString) -> AttributedString {
        var out = text
        let count = out.characters.count
        for (length, opacity) in [(42, 0.8), (24, 0.58), (10, 0.34)] where count > 0 {
            let start = out.characters.index(out.endIndex, offsetBy: -min(length, count))
            out[start..<out.endIndex].foregroundColor = Color.primary.opacity(opacity)
        }
        return out
    }

    @ViewBuilder private func row(_ block: MDBlock) -> some View {
        switch block {
        case .paragraph(let text): Text(Markdown.inline(text)).fixedSize(horizontal: false, vertical: true)
        case .heading(let level, let text): Text(Markdown.inline(text)).font(.system(size: size + (level == 1 ? 5 : level == 2 ? 3 : 1), weight: .semibold)).padding(.top, 3)
        case .bullets(let items): list(items) { _ in "•" }
        case .numbered(let items): list(items) { "\($0 + 1)." }
        case .quote(let text):
            Text(Markdown.inline(text)).foregroundStyle(.secondary).padding(.leading, 10)
                .overlay(alignment: .leading) { Rectangle().fill(MC.line).frame(width: 2) }
        case .code(let code): CodeBlock(code: code, size: size - 1)
        case .table(let header, let rows): table(header, rows)
        case .rule: Divider()
        }
    }

    private func table(_ header: [String], _ rows: [[String]]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 0) {
                GridRow {
                    ForEach(Array(header.enumerated()), id: \.offset) { _, cell in
                        Text(Markdown.inline(cell)).font(.system(size: size - 1, weight: .medium)).foregroundStyle(.secondary).padding(.vertical, 5)
                    }
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Divider().gridCellUnsizedAxes(.horizontal)
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                            Text(Markdown.inline(cell)).font(.system(size: size - 0.5)).padding(.vertical, 5)
                        }
                    }
                }
            }
            .padding(.horizontal, 2)
        }
    }

    private func list(_ items: [String], marker: @escaping (Int) -> String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(marker(index)).foregroundStyle(.secondary).monospacedDigit()
                    Text(Markdown.inline(item)).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// Code in a reply: copyable, and a long block folds to its first lines until asked.
struct CodeBlock: View {
    let code: String
    var size: CGFloat = 12
    var folded = 12
    @State private var open = false
    @State private var copied = false

    var body: some View {
        let lines = code.components(separatedBy: "\n")
        let long = lines.count > folded + 2
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Text(lines.count == 1 ? "1 line" : "\(lines.count) lines").font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                Button(copied ? "Copied" : "Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                    copied = true
                    Task { try? await Task.sleep(nanoseconds: 1_400_000_000); copied = false }
                }
                .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 10).padding(.top, 6).padding(.bottom, 2)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(long && !open ? lines.prefix(folded).joined(separator: "\n") : code)
                    .font(.system(size: size, design: .monospaced))
                    .padding(.horizontal, 10).padding(.bottom, long ? 4 : 9)
            }
            if long {
                Button(open ? "Show less" : "Show all \(lines.count) lines") { open.toggle() }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
                    .padding(.horizontal, 10).padding(.bottom, 8)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}
