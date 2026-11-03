import SwiftUI

/// What a shell command reaches, changes and runs, read from its text so a held one can be
/// taken in at a glance. Only a label: what is approved is always the whole command.
struct CommandEffects: Equatable {
    var hosts: [String] = [], deletes: [String] = [], programs: [String] = []

    var isEmpty: Bool { hosts.isEmpty && deletes.isEmpty && programs.isEmpty }

    private static let host = try? NSRegularExpression(pattern: #"https?://([A-Za-z0-9.-]+)"#)
    /// Moving around, printing and filtering text say nothing about what a command does.
    private static let plumbing: Set<String> = ["cd", "echo", "printf", "export", "set", "true", "false", "sleep", "test", "[",
                                                "cat", "grep", "egrep", "head", "tail", "sed", "awk", "tr", "sort", "uniq", "wc",
                                                "cut", "xargs", "tee", "mkdir", "ls", "pwd", "jq", "rm", "env", "time", "nohup"]

    init(_ command: String) {
        let whole = NSRange(command.startIndex..., in: command)
        for match in Self.host?.matches(in: command, range: whole) ?? [] {
            guard let range = Range(match.range(at: 1), in: command) else { continue }
            let name = String(command[range])
            if !hosts.contains(name) { hosts.append(name) }
        }
        // Followed through `cd`, so `cd /tmp && rm -rf bpe` reads as deleting /tmp/bpe.
        var folder: String?
        for segment in Self.segments(command) {
            var words = segment.split(whereSeparator: \.isWhitespace).map(String.init)
            while let first = words.first, first.contains("="), !first.hasPrefix("=") { words.removeFirst() }
            guard let program = words.first.map({ ($0 as NSString).lastPathComponent }) else { continue }
            if program == "cd", words.count > 1 {
                let target = words[1]
                folder = target.hasPrefix("/") || target.hasPrefix("~") || folder == nil ? target : (folder! as NSString).appendingPathComponent(target)
            } else if program == "rm" {
                for target in words.dropFirst() where !target.hasPrefix("-") {
                    let path = target.hasPrefix("/") || target.hasPrefix("~") || folder == nil ? target : (folder! as NSString).appendingPathComponent(target)
                    if !deletes.contains(path) { deletes.append(path) }
                }
            } else if !Self.plumbing.contains(program), !programs.contains(program) {
                programs.append(program)
            }
        }
    }

    /// The command split where one program hands over to the next, never inside quotes, so
    /// a script passed to `python3 -c` stays one piece.
    private static func segments(_ command: String) -> [String] {
        var out: [String] = [], current = "", quote: Character?
        var previous: Character = " "
        for character in command {
            if let open = quote {
                if character == open && previous != "\\" { quote = nil }
                current.append(character)
            } else if character == "'" || character == "\"" {
                quote = character
                current.append(character)
            } else if character == ";" || character == "|" || character == "&" || character == "\n" || character == "(" || character == ")" {
                if !current.trimmingCharacters(in: .whitespaces).isEmpty { out.append(current) }
                current = ""
            } else {
                current.append(character)
            }
            previous = character
        }
        if !current.trimmingCharacters(in: .whitespaces).isEmpty { out.append(current) }
        return out
    }

    /// A few words for a row that has no room for chips. Without a site or a deletion to
    /// name, the command's own first line says more than a program's name.
    static func summary(_ command: String) -> String {
        let found = CommandEffects(command)
        guard !found.hosts.isEmpty || !found.deletes.isEmpty else {
            return clip(oneLine(command.components(separatedBy: "\n").first ?? command), 60)
        }
        return (found.deletes.map { "deletes \($0)" } + found.hosts + found.programs).prefix(3).joined(separator: ", ")
    }
}

/// A command an agent was not allowed to run on its own, drawn from its record and kept
/// current: waiting with the command in full, then what became of it. Approving is only
/// offered once all of it is on screen, because whatever is approved runs exactly as shown.
struct ApprovalCard: View {
    @Environment(Store.self) private var store
    let id: String
    /// The notice's own words, for a server that cannot be asked about the request.
    let fallback: String
    @State private var expanded = false
    @State private var showOutput = false
    @State private var missing = false

    private static let preview = 6

    var body: some View {
        Group {
            if let row = store.approvalCards[id] {
                if row.status.str == "pending" { waiting(row) } else { answered(row) }
            } else if missing {
                MarkdownView(text: fallback)
            } else {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Held command").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
                .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
        }
        .task(id: id) {
            if store.approvalCards[id] == nil { missing = !(await store.loadApproval(id)) }
        }
    }

    private func isTool(_ row: JSON) -> Bool { row.kind.str == "mcp" }

    // MARK: Waiting

    @ViewBuilder private func waiting(_ row: JSON) -> some View {
        let command = row.command.str
        let lines = command.components(separatedBy: "\n")
        let long = lines.count > Self.preview
        let whole = !long || expanded
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "exclamationmark.shield").foregroundStyle(MC.warn).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(store.name(row.agent.str)) wants to \(isTool(row) ? "call a tool" : "run a command")")
                        .font(.system(size: 13, weight: .semibold))
                    Text(context(row)).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 8)
                Text(ago(row.ts.num)).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            if !isTool(row) { effects(CommandEffects(command)) }
            VStack(alignment: .leading, spacing: 0) {
                // Wrapped, never scrolled sideways: a line running off the edge would be
                // part of the command that is approved without being seen.
                Text(verbatim: whole ? command : lines.prefix(Self.preview).joined(separator: "\n"))
                    .font(.system(size: 11.5, design: .monospaced))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                if long {
                    Button(expanded ? "Show less" : "Show all \(lines.count) lines") { expanded.toggle() }
                        .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
                        .padding(.horizontal, 10).padding(.bottom, 8)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            if !row.reason.str.isEmpty {
                Text(row.reason.str).font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let prior = priorNote(row.prior) {
                Label(prior, systemImage: "clock.arrow.circlepath").font(.system(size: 11.5)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                Spacer()
                AsyncButton("Decline") { try await store.decide(approval: id, approve: false) }
                if whole {
                    AsyncButton(isTool(row) ? "Approve" : "Run it") { try await store.decide(approval: id, approve: true) }
                        .buttonStyle(.borderedProminent)
                } else {
                    Button("Review") { expanded = true }.buttonStyle(.borderedProminent)
                }
            }
            .controlSize(.small)
            .disabled(!store.live)
        }
        .padding(12)
        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.primary.opacity(0.08)))
    }

    private func context(_ row: JSON) -> String {
        let place = row.cwd.str.isEmpty ? "" : "in \(row.cwd.str.replacingOccurrences(of: NSHomeDirectory(), with: "~"))"
        return [row.task.str, place].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    @ViewBuilder private func effects(_ found: CommandEffects) -> some View {
        if !found.isEmpty {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 6) { chips(found) }
                VStack(alignment: .leading, spacing: 6) { chips(found) }
            }
        }
    }

    @ViewBuilder private func chips(_ found: CommandEffects) -> some View {
        ForEach(found.deletes, id: \.self) { chip("trash", "deletes \($0)") }
        ForEach(found.hosts, id: \.self) { chip("network", $0) }
        ForEach(found.programs.prefix(3), id: \.self) { chip("terminal", $0) }
    }

    private func chip(_ symbol: String, _ text: String) -> some View {
        Label(text, systemImage: symbol)
            .font(.system(size: 11))
            .lineLimit(1)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(Color.primary.opacity(0.07), in: Capsule())
    }

    private func priorNote(_ prior: JSON) -> String? {
        guard !prior.isNull else { return nil }
        let verb = prior.status.str == "denied" ? "declined" : "approved"
        let time = Date(timeIntervalSince1970: prior.ts.num).formatted(date: .omitted, time: .shortened)
        if prior.identical.flag { return "The same command you \(verb) at \(time)." }
        let changed = prior.diff.list.map(\.str).joined(separator: "\n")
        return "Close to one you \(verb) at \(time). What changed:\n\(changed.isEmpty ? "only spacing" : changed)"
    }

    // MARK: Answered

    @ViewBuilder private func answered(_ row: JSON) -> some View {
        let status = row.status.str
        let output = Output(row.result.str)
        let what = isTool(row) ? clip(row.command.str, 60) : CommandEffects.summary(row.command.str)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: symbol(status, output)).font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(status == "ran" && output.failed ? MC.danger : Color.secondary)
                    .frame(width: 16).accessibilityHidden(true)
                Text(title(status, output)).font(.system(size: 12.5, weight: .medium))
                if !what.isEmpty { Text(what).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1) }
                Spacer(minLength: 8)
                if status == "approved" { ProgressView().controlSize(.mini) }
                Text(Date(timeIntervalSince1970: row.updated.num).formatted(date: .omitted, time: .shortened))
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                if status == "ran", !output.text.isEmpty {
                    Button { showOutput.toggle() } label: {
                        Image(systemName: "chevron.down").font(.system(size: 10, weight: .semibold)).rotationEffect(.degrees(showOutput ? 180 : 0))
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    .help(showOutput ? "Hide output" : "Show output").accessibilityLabel(showOutput ? "Hide output" : "Show output")
                }
            }
            if showOutput { CodeBlock(code: output.text, size: 11, folded: 8) }
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
        .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityElement(children: .contain)
    }

    private func symbol(_ status: String, _ output: Output) -> String {
        switch status {
        case "ran": return output.failed ? "exclamationmark.circle" : "checkmark.circle"
        case "denied": return "xmark.circle"
        default: return "circle.dotted"
        }
    }

    private func title(_ status: String, _ output: Output) -> String {
        switch status {
        case "ran": return output.exit.map { $0 == 0 ? "Ran" : "Ran, exit \($0)" } ?? "Ran"
        case "denied": return "Declined"
        case "approved": return "Approved, running"
        default: return status.capitalized
        }
    }

    /// A run's output starts with its exit status on a line of its own.
    private struct Output {
        var exit: Int?
        var text = ""
        var failed: Bool { (exit ?? 0) != 0 }
        init(_ result: String) {
            let lines = result.components(separatedBy: "\n")
            if let first = lines.first, first.hasPrefix("exit "), let code = Int(first.dropFirst(5)) {
                exit = code
                text = lines.dropFirst().joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            } else {
                text = result.trimmingCharacters(in: .whitespacesAndNewlines)
            }
        }
    }
}

/// Between two messages far apart in time, when the second one was said.
struct TimeDivider: View {
    let ts: Double
    var body: some View {
        HStack(spacing: 10) {
            Rectangle().fill(Color.primary.opacity(0.1)).frame(height: 0.5)
            Text(Self.label(ts)).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize()
            Rectangle().fill(Color.primary.opacity(0.1)).frame(height: 0.5)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    static func label(_ ts: Double) -> String {
        let date = Date(timeIntervalSince1970: ts), calendar = Calendar.current
        let time = date.formatted(date: .omitted, time: .shortened)
        if calendar.isDateInToday(date) { return "Today \(time)" }
        if calendar.isDateInYesterday(date) { return "Yesterday \(time)" }
        return date.formatted(.dateTime.weekday(.wide).day().month(.abbreviated)) + " " + time
    }

    /// A gap this long reads as a new sitting.
    static func separates(_ earlier: Double, _ later: Double) -> Bool { later - earlier > 45 * 60 }
}
