import AppKit
import SwiftUI

/// A module's card: a tree of typed nodes (server/module_ui.py) drawn natively. It is data,
/// never markup. Nothing a module sends is run or loaded here, and a node this app does not
/// know is left out rather than guessed at.
enum ModuleTone {
    static func color(_ tone: String, fallback: Color = .primary) -> Color {
        switch tone {
        case "ok": return MC.good
        case "warn": return MC.warn
        case "bad": return MC.danger
        case "info": return MC.info
        case "accent": return .accentColor
        case "muted": return .secondary
        default: return fallback
        }
    }

    static func word(_ tone: String) -> String {
        switch tone {
        case "ok": return "OK"
        case "warn": return "Warning"
        case "bad": return "Problem"
        case "info": return "Note"
        default: return ""
        }
    }

    static func width(_ size: String) -> CGFloat {
        switch size {
        case "small": return 236
        case "large": return 440
        default: return 320
        }
    }
}

/// What a press on a card says: which action, its value, and the card's fields.
struct CardEvent {
    let action: String
    let value: JSON
    let values: [String: JSON]
}

struct ModuleCardView: View {
    let card: JSON
    var onDesktop = false
    let act: (CardEvent) -> Void
    @State private var fields: [String: JSON] = [:]

    var body: some View {
        let tone = card.tone.str
        VStack(alignment: .leading, spacing: 10) {
            header
            CardNode(node: card.view, fields: $fields, act: send)
                .frame(maxWidth: .infinity, alignment: .leading)
                // On the desktop a press on text drags the card, so the text is not selectable.
                .environment(\.cardTextSelectable, !onDesktop)
            footer
        }
        .padding(14)
        .frame(width: ModuleTone.width(card.size.str), alignment: .leading)
        .background(background)
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
            .strokeBorder(tone.isEmpty ? MC.line.opacity(0.6) : ModuleTone.color(tone).opacity(0.7), lineWidth: tone.isEmpty ? 0.5 : 1.2))
        .opacity(running ? 1 : 0.72)
        .accessibilityElement(children: .contain)
        .accessibilityLabel([card.title.str, ModuleTone.word(tone)].filter { !$0.isEmpty }.joined(separator: ", "))
    }

    private var running: Bool { card.module_status.str == "running" }

    @ViewBuilder private var background: some View {
        if onDesktop {
            Color.clear.glassBackground(cornerRadius: 16)
        } else {
            RoundedRectangle(cornerRadius: 16, style: .continuous).fill(MC.surface)
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 8) {
            let symbol = card.symbol.str
            if !symbol.isEmpty, NSImage(systemSymbolName: symbol, accessibilityDescription: nil) != nil {
                Image(systemName: symbol)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(ModuleTone.color(card.tone.str, fallback: .secondary))
                    .frame(width: 18)
                    .accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: card.title.str.isEmpty ? card.module_name.str : card.title.str)
                    .font(.system(size: 13, weight: .semibold)).lineLimit(1)
                if !card.subtitle.str.isEmpty {
                    Text(verbatim: card.subtitle.str).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                }
            }
            Spacer(minLength: 4)
            if !running { Pill(status: card.module_status.str.isEmpty ? "stopped" : card.module_status.str) }
        }
        .contentShape(Rectangle())
    }

    private var footer: some View {
        TimelineView(.periodic(from: .now, by: 30)) { _ in
            HStack(spacing: 4) {
                // The module's name, unless the card already goes by it.
                if card.module_name.str != card.title.str, !card.module_name.str.isEmpty {
                    Text(verbatim: card.module_name.str)
                    Text("·")
                }
                Text(verbatim: card.updated.num > 0 ? "updated \(ago(card.updated.num))" : "not drawn yet")
            }
            .font(.system(size: 10.5))
            .foregroundStyle(.tertiary)
            .lineLimit(1)
        }
    }

    private func send(_ action: String, _ value: JSON) {
        act(CardEvent(action: action, value: value, values: fields))
    }
}

private struct CardTextSelectable: EnvironmentKey { static let defaultValue = true }
extension EnvironmentValues {
    var cardTextSelectable: Bool {
        get { self[CardTextSelectable.self] }
        set { self[CardTextSelectable.self] = newValue }
    }
}

private struct Selectable: ViewModifier {
    let on: Bool
    func body(content: Content) -> some View {
        if on { content.textSelection(.enabled) } else { content.textSelection(.disabled) }
    }
}

/// One node and what is under it.
private struct CardNode: View {
    @Environment(\.cardTextSelectable) private var selectable
    let node: JSON
    @Binding var fields: [String: JSON]
    let act: (String, JSON) -> Void

    var body: some View {
        switch node.type.str {
        case "stack": stack
        case "grid": grid
        case "text": text
        case "stat": stat
        case "status": status
        case "badge":
            Text(verbatim: node.text.str)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(ModuleTone.color(node.tone.str, fallback: .secondary))
                .padding(.horizontal, 8).padding(.vertical, 2.5)
                .background(ModuleTone.color(node.tone.str, fallback: .secondary).opacity(0.14), in: Capsule())
                .fixedSize()
        case "progress":
            VStack(alignment: .leading, spacing: 4) {
                if !node.label.str.isEmpty { Text(verbatim: node.label.str).font(.system(size: 11)).foregroundStyle(.secondary) }
                CardBar(value: node.value.num, tint: ModuleTone.color(node.tone.str, fallback: .accentColor))
            }
            .accessibilityElement(children: .combine)
            .accessibilityValue("\(Int((min(max(node.value.num, 0), 1) * 100).rounded())) percent")
        case "chart": CardChart(node: node)
        case "symbol":
            let name = node.name.str
            Image(systemName: NSImage(systemSymbolName: name, accessibilityDescription: nil) != nil ? name : "questionmark.square.dashed")
                .font(.system(size: node.size.optNum.map { CGFloat(min(max($0, 8), 64)) } ?? 16))
                .foregroundStyle(ModuleTone.color(node.tone.str, fallback: .secondary))
        case "table": table
        case "button": button
        case "link":
            if let url = URL(string: node.url.str), ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
                Button { NSWorkspace.shared.open(url) } label: {
                    Label(node.label.str, systemImage: "arrow.up.right").labelStyle(TrailingIcon())
                }
                .buttonStyle(.plain)
                .foregroundStyle(Color.accentColor)
                .font(.system(size: 12))
                .help(url.absoluteString)
            }
        case "field":
            TextField(node.placeholder.str, text: Binding(
                get: { fields[node.name.str]?.str ?? node.value.str },
                set: { fields[node.name.str] = .string($0) }))
            .textFieldStyle(.roundedBorder)
            .controlSize(.small)
        case "toggle":
            Toggle(isOn: Binding(
                get: { fields[node.name.str]?.flag ?? node.value.flag },
                set: { on in
                    fields[node.name.str] = .bool(on)
                    if !node.action.str.isEmpty { act(node.action.str, .bool(on)) }
                })) {
                Text(verbatim: node.label.str.isEmpty ? node.name.str : node.label.str).font(.system(size: 12))
            }
            .toggleStyle(.switch)
            .controlSize(.mini)
        case "divider": Divider()
        case "spacer": Spacer(minLength: 4)
        default: EmptyView()
        }
    }

    private var children: [JSON] { node.children.list }

    @ViewBuilder private var stack: some View {
        let spacing = node.spacing.optNum.map { CGFloat(min(max($0, 0), 40)) } ?? 8
        if node.axis.str == "h" {
            HStack(alignment: .center, spacing: spacing) {
                ForEach(Array(children.enumerated()), id: \.offset) { _, child in CardNode(node: child, fields: $fields, act: act) }
            }
        } else {
            let align: HorizontalAlignment = node.align.str == "center" ? .center : node.align.str == "trailing" ? .trailing : .leading
            VStack(alignment: align, spacing: spacing) {
                ForEach(Array(children.enumerated()), id: \.offset) { _, child in
                    CardNode(node: child, fields: $fields, act: act)
                        .frame(maxWidth: .infinity, alignment: Alignment(horizontal: align, vertical: .center))
                }
            }
        }
    }

    private var grid: some View {
        let count = min(max(node.columns.optNum.map { Int($0) } ?? 2, 1), 6)
        let spacing = node.spacing.optNum.map { CGFloat(min(max($0, 0), 40)) } ?? 10
        return LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: spacing, alignment: .topLeading), count: count),
                         alignment: .leading, spacing: spacing) {
            ForEach(Array(children.enumerated()), id: \.offset) { _, child in CardNode(node: child, fields: $fields, act: act) }
        }
    }

    private var text: some View {
        let font: Font
        switch node.style.str {
        case "title": font = .system(size: 17, weight: .semibold)
        case "headline": font = .system(size: 13, weight: .semibold)
        case "caption": font = .system(size: 11)
        case "footnote": font = .system(size: 10)
        case "mono": font = .system(size: 11.5, design: .monospaced)
        default: font = .system(size: 12.5)
        }
        let lines = node.lines.optNum.map { max(1, Int($0)) }
        return Text(verbatim: node.text.str)
            .font(font)
            .foregroundStyle(ModuleTone.color(node.tone.str))
            .lineLimit(lines)
            .fixedSize(horizontal: false, vertical: lines == nil)
            .modifier(Selectable(on: selectable))
    }

    private var stat: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(verbatim: node.label.str).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(verbatim: node.value.str)
                    .font(.system(size: 22, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(ModuleTone.color(node.tone.str))
                    .lineLimit(1).minimumScaleFactor(0.6)
                if !node.unit.str.isEmpty { Text(verbatim: node.unit.str).font(.system(size: 12)).foregroundStyle(.secondary) }
                switch node.trend.str {
                case "up": Image(systemName: "arrow.up.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                case "down": Image(systemName: "arrow.down.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                case "flat": Image(systemName: "arrow.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                default: EmptyView()
                }
            }
            if !node.detail.str.isEmpty { Text(verbatim: node.detail.str).font(.system(size: 10.5)).foregroundStyle(.secondary).lineLimit(2) }
        }
        .accessibilityElement(children: .combine)
    }

    private var status: some View {
        let tone = node.tone.str
        return HStack(spacing: 8) {
            Circle().fill(ModuleTone.color(tone, fallback: .secondary)).frame(width: 8, height: 8)
                .shadow(color: ModuleTone.color(tone, fallback: .clear).opacity(tone == "bad" ? 0.6 : 0), radius: 3)
            Text(verbatim: node.label.str).font(.system(size: 12.5)).lineLimit(1)
            Spacer(minLength: 6)
            if !node.detail.str.isEmpty {
                Text(verbatim: node.detail.str).font(.system(size: 11.5)).monospacedDigit().foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityValue(ModuleTone.word(tone))
    }

    private var table: some View {
        let columns = node.columns.list.map(\.str)
        let rows = node.rows.list.map { $0.list.map(\.str) }
        return Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
            if !columns.isEmpty {
                GridRow {
                    ForEach(Array(columns.enumerated()), id: \.offset) { _, title in
                        Text(verbatim: title).font(.system(size: 10.5, weight: .medium)).foregroundStyle(.secondary)
                    }
                }
                Divider()
            }
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                GridRow {
                    ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                        Text(verbatim: cell).font(.system(size: 12)).monospacedDigit().lineLimit(1)
                    }
                }
            }
        }
    }

    @ViewBuilder private var button: some View {
        let label = Group {
            if !node.symbol.str.isEmpty, NSImage(systemSymbolName: node.symbol.str, accessibilityDescription: nil) != nil {
                Label(node.label.str, systemImage: node.symbol.str)
            } else {
                Text(verbatim: node.label.str)
            }
        }
        switch node.style.str {
        case "primary":
            Button { act(node.action.str, node.value) } label: { label }.buttonStyle(.borderedProminent).controlSize(.small)
        case "destructive":
            Button(role: .destructive) { act(node.action.str, node.value) } label: { label }.buttonStyle(.bordered).controlSize(.small)
        default:
            Button { act(node.action.str, node.value) } label: { label }.buttonStyle(.bordered).controlSize(.small)
        }
    }
}

/// A thin bar in the card's own colours.
private struct CardBar: View {
    let value: Double
    let tint: Color

    var body: some View {
        GeometryReader { box in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.09))
                Capsule().fill(tint).frame(width: max(6, box.size.width * CGFloat(min(max(value, 0), 1))))
            }
        }
        .frame(height: 6)
    }
}

private struct TrailingIcon: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 3) { configuration.title; configuration.icon.imageScale(.small) }
    }
}

/// A line or bars, scaled to the values or to the min and max the module gives.
private struct CardChart: View {
    let node: JSON

    var body: some View {
        let values = node.values.list.map(\.num)
        let tint = ModuleTone.color(node.tone.str, fallback: .accentColor)
        VStack(alignment: .leading, spacing: 4) {
            if !node.label.str.isEmpty { Text(verbatim: node.label.str).font(.system(size: 11)).foregroundStyle(.secondary) }
            Canvas { context, size in
                guard !values.isEmpty else { return }
                let low = node.min.optNum ?? min(values.min() ?? 0, 0)
                let high = max(node.max.optNum ?? (values.max() ?? 1), low + 0.000001)
                func y(_ v: Double) -> CGFloat { size.height - CGFloat((min(max(v, low), high) - low) / (high - low)) * size.height }
                if node.kind.str == "bar" {
                    let step = size.width / CGFloat(values.count)
                    for (i, v) in values.enumerated() {
                        let rect = CGRect(x: CGFloat(i) * step + step * 0.12, y: y(v), width: step * 0.76, height: size.height - y(v))
                        context.fill(Path(roundedRect: rect, cornerRadius: min(2, step * 0.2)), with: .color(tint.opacity(0.85)))
                    }
                } else {
                    let step = values.count > 1 ? size.width / CGFloat(values.count - 1) : 0
                    var line = Path()
                    for (i, v) in values.enumerated() {
                        let point = CGPoint(x: CGFloat(i) * step, y: y(v))
                        if i == 0 { line.move(to: point) } else { line.addLine(to: point) }
                    }
                    var area = line
                    area.addLine(to: CGPoint(x: CGFloat(values.count - 1) * step, y: size.height))
                    area.addLine(to: CGPoint(x: 0, y: size.height))
                    area.closeSubpath()
                    context.fill(area, with: .linearGradient(Gradient(colors: [tint.opacity(0.22), tint.opacity(0)]),
                                                            startPoint: .zero, endPoint: CGPoint(x: 0, y: size.height)))
                    context.stroke(line, with: .color(tint), style: StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
                }
            }
            .frame(height: 38)
        }
        .accessibilityElement()
        .accessibilityLabel(node.label.str.isEmpty ? "Chart" : node.label.str)
        .accessibilityValue(node.values.list.last.map { "latest \($0.str)" } ?? "")
    }
}
