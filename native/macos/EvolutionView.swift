import AppKit
import Charts
import SwiftUI

/// The last full read. It outlives the screen, so coming back draws at once.
@Observable @MainActor
private final class EvolutionModel {
    static let shared = EvolutionModel()

    private var base = ""
    private(set) var data: JSON = .null
    private(set) var problem: String?
    /// The switch shows what was asked for until the server has answered.
    var pendingEnabled: Bool?

    var loaded: Bool { !data.isNull }

    func load(_ api: API) async {
        if api.base != base { base = api.base; data = .null; problem = nil }
        do {
            let next = try await api.get("/api/evolution")
            if Task.isCancelled { return }
            if next != data { data = next }
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    func setEnabled(_ on: Bool, store: Store) async {
        guard let api = store.api else { store.notify("Not connected to the runtime yet.", error: true); return }
        pendingEnabled = on
        defer { pendingEnabled = nil }
        do {
            _ = try await api.put("/api/evolution", ["enabled": on])
            store.notify(on ? "Learning is on." : "Learning is paused. Lessons already learned stay in use.")
            await load(api)
        } catch {
            store.notify(error.localizedDescription, error: true)
        }
    }

    /// Shows the server's answer straight away; the event that follows reads the rest.
    func updateLesson(_ id: String, _ fields: [String: Any], store: Store) async throws {
        guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
        let lesson = try await api.put("/api/lessons/\(API.escape(id))", fields).lesson
        if case .object(var object) = data, !lesson.isNull {
            var rows = data.lessons.list
            if let index = rows.firstIndex(where: { $0.id == lesson.id }) { rows[index] = lesson }
            object["lessons"] = .array(rows)
            data = .object(object)
        }
        store.refreshSoon(after: 0.05)
    }

    func addLesson(_ text: String, scope: String, store: Store) async throws {
        guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
        _ = try await api.post("/api/lessons", ["agent": scope, "text": text])
        store.notify("Lesson added. It applies from the next turn.")
        await load(api)
    }
}

/// The store is read in this body alone, so a tick redraws nothing else on the screen.
private struct EvolutionLoader: View {
    @Environment(Store.self) private var store
    private struct Key: Equatable { let tick: Int; let base: String?; let link: Bool }

    var body: some View {
        Color.clear.task(id: Key(tick: store.ticks.evolution, base: store.api?.base, link: store.link)) {
            if let api = store.api { await EvolutionModel.shared.load(api) }
        }
    }
}

private enum Series {
    static let one = adaptive(light: (0x2a, 0x78, 0xd6), dark: (0x39, 0x87, 0xe5))
    static let two = adaptive(light: (0xeb, 0x68, 0x34), dark: (0xd9, 0x59, 0x26))

    private static func adaptive(light: (Int, Int, Int), dark: (Int, Int, Int)) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let rgb = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: CGFloat(rgb.0) / 255, green: CGFloat(rgb.1) / 255, blue: CGFloat(rgb.2) / 255, alpha: 1)
        })
    }
}

/// Wraps its children like a line of text.
private struct Flow: Layout {
    var spacing: CGFloat = 8
    var lineSpacing: CGFloat = 4

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(subviews, width: proposal.width ?? .infinity).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let origins = arrange(subviews, width: bounds.width).origins
        for (subview, origin) in zip(subviews, origins) {
            subview.place(at: CGPoint(x: bounds.minX + origin.x, y: bounds.minY + origin.y), proposal: .unspecified)
        }
    }

    private func arrange(_ subviews: Subviews, width: CGFloat) -> (size: CGSize, origins: [CGPoint]) {
        var origins: [CGPoint] = [], x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0, widest: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += lineHeight + lineSpacing
                lineHeight = 0
            }
            origins.append(CGPoint(x: x, y: y))
            widest = max(widest, x + size.width)
            lineHeight = max(lineHeight, size.height)
            x += size.width + spacing
        }
        return (CGSize(width: widest, height: y + lineHeight), origins)
    }
}

// MARK: The screen

struct EvolutionView: View {
    var body: some View {
        Page(title: "Getting better",
             subtitle: "Your corrections and its repeated mistakes become lessons. Each lesson is tried first and kept only if the mistake stops happening; one that does not help is retired on its own. Lessons can make it more careful, never give it more permission.",
             actions: { EvolutionActions() },
             content: { EvolutionSections() })
            .background { EvolutionLoader() }
    }
}

private struct EvolutionActions: View {
    @Environment(Store.self) private var store
    @State private var reviewing = false

    var body: some View {
        let model = EvolutionModel.shared
        let enabled = model.pendingEnabled ?? (model.loaded ? model.data.enabled.flag : store.evolution.enabled.flag)
        HStack(spacing: 14) {
            Toggle("Learning", isOn: Binding(get: { enabled }, set: { on in Task { await model.setEnabled(on, store: store) } }))
                .toggleStyle(.switch)
                .controlSize(.small)
                .disabled(!model.loaded && store.evolution.enabled.isNull)
            Button { review() } label: { Label("Run review now", systemImage: "arrow.clockwise") }
                .disabled(reviewing)
        }
        .fixedSize()
    }

    private func review() {
        reviewing = true
        Task {
            do {
                guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
                _ = try await api.post("/api/evolution/review")
                store.notify("Review started. The Evolution page updates when it finishes.")
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            reviewing = false
        }
    }
}

private struct EvolutionSections: View {
    var body: some View {
        let model = EvolutionModel.shared
        ScoreRow()
        if model.loaded {
            DailyCard()
            LessonsSection()
            ClustersSection()
            ReviewSection()
        } else if let problem = model.problem {
            Card { EmptyNote(title: "Could not load", detail: problem) }
        } else {
            Text("Loading…").font(.callout).foregroundStyle(.secondary)
        }
    }
}

// MARK: Headline numbers

private enum Trend {
    case hidden, noData
    case change(Int, unit: String, goodWhenUp: Bool)
}

private func points(_ now: JSON, _ before: JSON) -> Int? {
    guard let now = now.optNum, let before = before.optNum else { return nil }
    return Int(((now - before) * 100).rounded())
}

/// Draws the moment the screen opens: the store's summary has the same week, previous week
/// and daily shape as the full read, which takes over when it arrives.
private struct ScoreRow: View {
    @Environment(Store.self) private var store
    @State private var columns = 4

    var body: some View {
        let cards = scoreCards
        VStack(spacing: 12) {
            if columns == 4 {
                line(cards[...])
            } else {
                line(cards[..<2])
                line(cards[2...])
            }
        }
        .onGeometryChange(for: Int.self) { $0.size.width >= 720 ? 4 : 2 } action: { columns = $0 }
    }

    /// Fixed vertically so the cards on a line take the height of the tallest.
    private func line(_ cards: ArraySlice<ScoreCard>) -> some View {
        HStack(alignment: .top, spacing: 12) { ForEach(cards) { $0 } }
            .fixedSize(horizontal: false, vertical: true)
    }

    private var scoreCards: [ScoreCard] {
        let model = EvolutionModel.shared
        let card = model.loaded ? model.data.scorecard : store.evolution
        let week = card.week, previous = card.previous_week
        let days = Array(card.days.list.suffix(14))
        let active = model.loaded ? model.data.counts.active.int : store.evolution.active.int
        let trial = model.loaded ? model.data.counts.trial.int : store.evolution.trial.int
        let retired = model.loaded ? " · \(model.data.counts.retired.int) retired" : ""
        let hadNoWeek = previous.runs.optNum == 0 && previous.tool_calls.optNum == 0
        let corrections = hadNoWeek ? nil : previous.corrections.optNum.map { week.corrections.int - Int($0) }

        return [
            ScoreCard(label: "Clean runs",
                      value: week.clean_rate.optNum.map { "\(Int(($0 * 100).rounded()))" } ?? "Measuring…",
                      unit: week.clean_rate.isNull ? nil : "%", measuring: week.clean_rate.isNull,
                      spark: days.map(\.clean_rate.optNum),
                      trend: week.runs.int == 0 ? .hidden : trend(points(week.clean_rate, previous.clean_rate), " pts", goodWhenUp: true)),
            ScoreCard(label: "Your corrections", value: "\(week.corrections.int)",
                      spark: days.map(\.corrections.optNum),
                      trend: trend(corrections, "", goodWhenUp: false)),
            ScoreCard(label: "Tool error rate",
                      value: week.error_rate.optNum.map { String(format: "%.1f", $0 * 100) } ?? "—",
                      unit: week.error_rate.isNull ? nil : "%",
                      spark: days.map(\.error_rate.optNum),
                      trend: trend(points(week.error_rate, previous.error_rate), " pts", goodWhenUp: false)),
            ScoreCard(label: "Lessons in use", value: "\(active)", trend: .hidden,
                      note: "\(plural(trial, "lesson")) on trial\(retired)"),
        ]
    }

    private func trend(_ delta: Int?, _ unit: String, goodWhenUp: Bool) -> Trend {
        delta.map { .change($0, unit: unit, goodWhenUp: goodWhenUp) } ?? .noData
    }
}

private struct ScoreCard: View, Identifiable {
    var id: String { label }
    let label: String
    let value: String
    var unit: String?
    var measuring = false
    var spark: [Double?]?
    let trend: Trend
    var note: String?

    var body: some View {
        Card(padding: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Text(label).font(.system(size: 12)).foregroundStyle(.secondary)
                figure
                trendLine
                if let note { Text(note).font(.system(size: 12)).foregroundStyle(.secondary) }
                if let spark { Sparkline(values: spark, label: label).padding(.top, 4) }
            }
            .frame(maxHeight: .infinity, alignment: .topLeading)
            .accessibilityElement(children: .combine)
        }
    }

    private var figure: some View {
        HStack(alignment: .firstTextBaseline, spacing: 3) {
            if measuring {
                Text(value).font(.system(size: 16, weight: .medium)).foregroundStyle(.secondary)
            } else {
                Text(value).font(.system(size: 26, weight: .semibold)).monospacedDigit()
            }
            if let unit { Text(unit).font(.system(size: 13, weight: .medium)).foregroundStyle(.secondary) }
        }
        .frame(height: 32, alignment: .bottomLeading)
    }

    @ViewBuilder private var trendLine: some View {
        switch trend {
        case .hidden:
            EmptyView()
        case .noData:
            Text("vs previous 7 days: no data yet").font(.system(size: 12)).foregroundStyle(.secondary)
        case .change(let delta, let unit, let goodWhenUp):
            let good: Bool? = delta == 0 ? nil : (delta > 0) == goodWhenUp
            let sign = delta > 0 ? "+" : delta < 0 ? "−" : "±"
            let text = "\(sign)\(abs(delta))\(unit) vs previous 7 days"
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                if let good { Image(systemName: good ? "checkmark" : "exclamationmark.triangle").imageScale(.small).accessibilityHidden(true) }
                Text(text).monospacedDigit()
            }
            .font(.system(size: 12))
            .foregroundStyle(good == nil ? Color.secondary : good == true ? MC.good : MC.danger)
            .accessibilityLabel(good == nil ? text : "\(text), \(good == true ? "better" : "worse")")
        }
    }
}

private struct Sparkline: View {
    let values: [Double?]
    let label: String

    private struct Point: Identifiable { let id: Int; let value: Double }

    var body: some View {
        let shown = values.enumerated().compactMap { index, value in value.map { Point(id: index, value: $0) } }
        if shown.count < 2 {
            Color.clear.frame(height: 30).accessibilityHidden(true)
        } else {
            let low = shown.map(\.value).min() ?? 0, high = shown.map(\.value).max() ?? 1
            Chart {
                ForEach(shown) { point in
                    LineMark(x: .value("Day", point.id), y: .value(label, point.value))
                        .lineStyle(StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                        .foregroundStyle(Color.secondary.opacity(0.6))
                }
                if let last = shown.last {
                    PointMark(x: .value("Day", last.id), y: .value(label, last.value))
                        .symbolSize(28)
                        .foregroundStyle(Color.primary)
                }
            }
            .chartXAxis(.hidden)
            .chartYAxis(.hidden)
            .chartLegend(.hidden)
            .chartXScale(domain: 0...max(values.count - 1, 1))
            .chartYScale(domain: low...(high > low ? high : low + 1))
            .chartPlotStyle { $0.padding(.vertical, 5).padding(.horizontal, 4) }
            .frame(height: 30)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Trend over \(values.count) days")
        }
    }
}

// MARK: Daily charts

private struct ChartDay: Identifiable, Equatable {
    let id: String
    let short: String
    let long: String
    let row: JSON
}

private let dayParser: DateFormatter = {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm"
    return formatter
}()

private func chartDays(_ rows: [JSON]) -> [ChartDay] {
    rows.map { row in
        let day = row.day.str
        guard let date = dayParser.date(from: day + "T12:00") else { return ChartDay(id: day, short: day, long: day, row: row) }
        return ChartDay(id: day, short: date.formatted(.dateTime.month(.abbreviated).day()),
                        long: date.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day()), row: row)
    }
}

private struct DailyCard: View {
    @AppStorage("evolution.showTable") private var showTable = false

    var body: some View {
        let days = chartDays(EvolutionModel.shared.data.scorecard.days.list)
        Card(padding: 16) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("Daily, last 30 days").font(.headline)
                    Text("Clean runs are runs that finished, were not corrected and did not flail.")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    Button(showTable ? "Show charts" : "View as table") { showTable.toggle() }
                        .buttonStyle(.link)
                        .font(.caption)
                        .fixedSize()
                }
                if showTable { DailyTable(days: days) } else { DailyCharts(days: days) }
            }
        }
    }
}

/// One measure per chart, one axis each: a clean-run rate and a count of corrections on the
/// same axes would invent a relationship between them.
private struct DailyCharts: View {
    let days: [ChartDay]

    var body: some View {
        if days.contains(where: { $0.row.clean_rate.optNum != nil }) {
            DayBars(title: "Clean runs", noun: "clean runs", key: "clean_rate", days: days, rate: true, tint: Series.one)
        } else {
            Text("Clean runs are counted from today, as each run finishes. The two charts below already have history.")
                .font(.system(size: 12.5)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        DayBars(title: "Tool calls that failed", noun: "tool calls failed", key: "error_rate", days: days, rate: true, tint: Series.two)
        DayBars(title: "Your corrections", noun: "corrections", key: "corrections", days: days, rate: false, tint: Series.two)
    }
}

/// Holds its own hover, so moving the pointer redraws one chart and nothing else.
private struct DayBars: View {
    let title: String
    let noun: String
    let key: String
    let days: [ChartDay]
    let rate: Bool
    let tint: Color
    @State private var hovered: String?

    private func value(_ day: ChartDay) -> Double? { day.row[key].optNum }

    private var axisTop: Double {
        if rate { return 1 }
        let highest = days.compactMap(value).max() ?? 0
        return (max(2, highest) / 2).rounded(.up) * 2
    }

    var body: some View {
        let top = axisTop
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 12)).foregroundStyle(.secondary)
            Chart {
                ForEach(days) { day in
                    let amount = value(day) ?? 0
                    let visible = amount / top >= 0.006
                    // A day too small to see still gets a stub, so the row of days stays readable.
                    BarMark(x: .value("Day", day.id), y: .value(title, visible ? amount : top * 0.025), width: .ratio(0.62))
                        .cornerRadius(3)
                        .foregroundStyle(visible ? tint : Color.secondary.opacity(0.25))
                        .opacity(day.id == hovered ? 0.75 : 1)
                }
                if let day = days.first(where: { $0.id == hovered }) {
                    RuleMark(x: .value("Day", day.id))
                        .foregroundStyle(Color.clear)
                        .annotation(position: .top, alignment: .center, spacing: 2, overflowResolution: .init(x: .fit(to: .chart), y: .disabled)) {
                            tooltip(day)
                        }
                }
            }
            .chartLegend(.hidden)
            .chartXScale(domain: days.map(\.id))
            .chartYScale(domain: 0...top)
            .chartYAxis {
                AxisMarks(position: .leading, values: [0, top / 2, top]) { mark in
                    AxisGridLine()
                    AxisValueLabel {
                        if let amount = mark.as(Double.self) {
                            Text(rate ? "\(Int((amount * 100).rounded()))%" : "\(Int(amount.rounded()))").monospacedDigit()
                        }
                    }
                }
            }
            .chartXAxis {
                // The first, the middle and the last day: thirty dates do not fit under a bar chart.
                AxisMarks { mark in
                    AxisValueLabel(collisionResolution: .disabled) {
                        if let id = mark.as(String.self), dayTicks.contains(id), let day = days.first(where: { $0.id == id }) { Text(day.short).fixedSize() }
                    }
                }
            }
            .chartOverlay { proxy in
                GeometryReader { geometry in
                    Rectangle().fill(Color.clear).contentShape(Rectangle())
                        .onContinuousHover { phase in
                            switch phase {
                            case .active(let point):
                                guard let frame = proxy.plotFrame else { return }
                                let next = proxy.value(atX: point.x - geometry[frame].minX, as: String.self)
                                if next != hovered { hovered = next }
                            case .ended:
                                hovered = nil
                            }
                        }
                }
            }
            .frame(height: 112)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(title) per day, last \(days.count) days")
            .accessibilityValue(summary)
        }
        .zIndex(hovered == nil ? 0 : 1)
    }

    private var dayTicks: [String] {
        guard let first = days.first, let last = days.last else { return [] }
        return first.id == last.id ? [first.id] : [first.id, days[days.count / 2].id, last.id]
    }

    private func words(_ amount: Double) -> String { rate ? "\(Int((amount * 100).rounded())) percent" : "\(Int(amount.rounded()))" }

    private var summary: String {
        let measured = days.compactMap { day in value(day).map { (day: day, amount: $0) } }
        guard let latest = measured.last, let high = measured.max(by: { $0.amount < $1.amount }), let low = measured.min(by: { $0.amount < $1.amount }) else {
            return "No data yet. View as table lists every day."
        }
        let total = measured.reduce(0) { $0 + $1.amount }
        let middle = rate ? "average \(words(total / Double(measured.count)))" : "\(Int(total.rounded())) in total"
        return "\(plural(measured.count, "day")) with data, \(middle). Highest \(words(high.amount)) on \(high.day.short), lowest \(words(low.amount)) on \(low.day.short), latest \(words(latest.amount)) on \(latest.day.short). View as table lists every day."
    }

    private func tooltip(_ day: ChartDay) -> some View {
        let amount = value(day)
        let shown = amount.map { rate ? "\(Int(($0 * 100).rounded()))%" : "\(Int($0))" } ?? "No runs"
        var extra = ""
        if key == "clean_rate", day.row.runs.int > 0 { extra = "\(day.row.clean.int) of \(day.row.runs.int) runs" }
        if key == "error_rate", day.row.tool_calls.int > 0 { extra = "\(day.row.tool_errors.int) of \(day.row.tool_calls.int) calls" }
        return VStack(alignment: .leading, spacing: 1) {
            Text(shown).font(.system(size: 13, weight: .semibold)).monospacedDigit()
            Text("\(noun) · \(day.long)").font(.system(size: 11)).foregroundStyle(.secondary)
            if !extra.isEmpty { Text(extra).font(.system(size: 11)).foregroundStyle(.secondary) }
        }
        .padding(.horizontal, 9).padding(.vertical, 6)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(MC.line))
        .fixedSize()
    }
}

private struct DailyTable: View {
    let days: [ChartDay]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .trailing, horizontalSpacing: 24, verticalSpacing: 6) {
                GridRow {
                    ForEach(["Day", "Runs", "Clean", "Tool calls", "Failed", "Corrections", "Tokens"], id: \.self) { heading in
                        Text(heading).font(.caption.weight(.medium)).foregroundStyle(.secondary)
                            .gridColumnAlignment(heading == "Day" ? .leading : .trailing)
                    }
                }
                Divider().gridCellUnsizedAxes(.horizontal)
                ForEach(days.reversed()) { day in
                    let row = day.row
                    GridRow {
                        Text(row.day.str)
                        Text("\(row.runs.int)")
                        Text(percent(row.clean_rate))
                        Text("\(row.tool_calls.int)")
                        Text(row.error_rate.optNum.map { String(format: "%.1f%%", $0 * 100) } ?? "—")
                        Text("\(row.corrections.int)")
                        Text(row.tokens.int.formatted())
                    }
                    .font(.system(size: 12))
                    .monospacedDigit()
                }
            }
            .textSelection(.enabled)
            .padding(.vertical, 2)
        }
    }
}

// MARK: Lessons

private enum LessonFilter: String, CaseIterable, Identifiable {
    case live, trial, active, held, retired
    var id: String { rawValue }
    var statuses: Set<String> {
        switch self {
        case .live: return ["trial", "active"]
        case .trial: return ["trial"]
        case .active: return ["active"]
        case .held: return ["proposed", "suspended"]
        case .retired: return ["retired"]
        }
    }
    var title: String {
        switch self {
        case .live: return "In use"
        case .trial: return "On trial"
        case .active: return "Adopted"
        case .held: return "Waiting on you"
        case .retired: return "Retired"
        }
    }
}

private struct LessonsSection: View {
    @AppStorage("evolution.filter") private var filter = LessonFilter.live

    var body: some View {
        let lessons = EvolutionModel.shared.data.lessons.list
        let shown = lessons.filter { filter.statuses.contains($0.status.str) }
        SectionHead(title: "Lessons") {
            Picker("Show lessons", selection: $filter) {
                ForEach(LessonFilter.allCases) { option in
                    Text("\(option.title) \(lessons.filter { option.statuses.contains($0.status.str) }.count)").tag(option)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
        }
        Card {
            if shown.isEmpty {
                EmptyNote(title: filter == .live ? "No lessons yet" : "Nothing here",
                          detail: filter == .live ? "They come from your corrections and from failures that keep repeating." : nil)
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(shown) { lesson in
                        if lesson.id != shown.first?.id { Divider() }
                        LessonRow(lesson: lesson)
                    }
                }
            }
            Divider()
            AddLessonForm()
        }
    }
}

private struct LessonRow: View {
    @Environment(Store.self) private var store
    let lesson: JSON

    private static let sources = ["failure": "From repeated failures", "correction": "From your correction",
                                  "owner-rule": "Your standing rule", "owner": "Written by you"]

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("L\(lesson.id)").font(.system(size: 11, weight: .medium, design: .monospaced)).foregroundStyle(.secondary)
                    Text(lesson.text.str)
                        .font(.system(size: 13))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Flow(spacing: 10, lineSpacing: 4) {
                    scope
                    status
                    Text(Self.sources[lesson.source.str] ?? lesson.source.str)
                    Text(ago(lesson.ts.num))
                    if lesson.recurrences.int > 0 {
                        Label("recurred \(lesson.recurrences.int)×", systemImage: "exclamationmark.triangle").labelStyle(.titleAndIcon)
                    }
                }
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                if !lesson.note.str.isEmpty {
                    Text(lesson.note.str).font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                LessonEvidence(evidence: lesson.evidence)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            LessonActions(lesson: lesson)
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
    }

    @ViewBuilder private var scope: some View {
        if lesson.agent.str == "*" {
            Label("Everywhere", systemImage: "person.2").labelStyle(.titleAndIcon)
        } else {
            HStack(spacing: 5) {
                AgentBadge(agent: store.agent(lesson.agent.str), size: 16)
                Text(store.name(lesson.agent.str))
            }
        }
    }

    @ViewBuilder private var status: some View {
        switch lesson.status.str {
        case "trial": Pill(status: "trial", label: "On trial")
        case "active": Pill(status: "active", label: "Adopted")
        case "retired": Pill(status: "retired", label: "Retired")
        case "suspended": Pill(status: "suspended", label: "Suspended")
        case "proposed": Pill(status: "pending", label: "Waiting for your OK")
        default: Pill(status: lesson.status.str)
        }
    }
}

private struct LessonEvidence: View {
    let evidence: JSON

    private var text: String {
        let corrections = evidence.corrections.list
        if !corrections.isEmpty { return corrections.map(\.str).joined(separator: "\n\n") }
        return evidence.samples.list.map { sample in
            var line = "\(sample.agent.str): \(sample.call.str)\n  → \(sample.error.str)"
            if !sample.then_worked.str.isEmpty { line += "\n  then worked: \(sample.then_worked.str)" }
            return line
        }
        .joined(separator: "\n\n")
    }

    var body: some View {
        let text = text
        if !text.isEmpty {
            DisclosureGroup("Why this lesson exists") {
                Text(text)
                    .font(.system(size: 12, design: .monospaced))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .padding(.top, 4)
            }
            .font(.system(size: 12))
        }
    }
}

private struct LessonActions: View {
    @Environment(Store.self) private var store
    let lesson: JSON

    var body: some View {
        let status = lesson.status.str
        let pinned = lesson.pinned.flag
        HStack(spacing: 6) {
            AsyncButton(action: { try await change(["pinned": !pinned]) }) {
                Image(systemName: pinned ? "pin.fill" : "pin").foregroundStyle(pinned ? Color.accentColor : Color.secondary)
            }
            .buttonStyle(.borderless)
            .help(pinned ? "Unpin" : "Pin: always shown first")
            .accessibilityLabel(pinned ? "Unpin" : "Pin")

            if status == "trial" { AsyncButton("Adopt") { try await change(["status": "active"]) } }
            if status == "proposed" { AsyncButton("Use it") { try await change(["status": "active"]) } }
            if status == "retired" || status == "suspended" {
                let owners = lesson.source.str == "owner" || lesson.source.str == "owner-rule"
                AsyncButton("Restore") { try await change(["status": owners ? "active" : "trial"]) }
            }
            if status != "retired" {
                AsyncButton(action: { try await change(["status": "retired"]) }) { Text("Retire").foregroundStyle(MC.danger) }
            }
        }
        .controlSize(.small)
        .fixedSize()
    }

    private func change(_ fields: [String: Any]) async throws {
        try await EvolutionModel.shared.updateLesson(lesson.id, fields, store: store)
    }
}

/// Typing here redraws the form and nothing above it.
private struct AddLessonForm: View {
    @Environment(Store.self) private var store
    @State private var text = ""
    @State private var scope = "*"
    @State private var adding = false

    private var clean: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        HStack(spacing: 8) {
            TextField("Lesson", text: $text, prompt: Text("Teach it something yourself, e.g. 'acmeshop lives in ~/Sites/acmeshop'"))
                .textFieldStyle(.roundedBorder)
                .labelsHidden()
                .onSubmit { add() }
                .onChange(of: text) { _, new in if new.count > 280 { text = String(new.prefix(280)) } }
            Picker("Applies to", selection: $scope) {
                Text("Everywhere").tag("*")
                ForEach(store.agents) { Text($0.name).tag($0.id) }
            }
            .labelsHidden()
            .fixedSize()
            Button("Add lesson") { add() }
                .buttonStyle(.borderedProminent)
                .disabled(clean.isEmpty || adding)
        }
        .padding(12)
    }

    private func add() {
        let lesson = clean
        guard !lesson.isEmpty, !adding else { return }
        adding = true
        Task {
            defer { adding = false }
            do {
                try await EvolutionModel.shared.addLesson(lesson, scope: scope, store: store)
                text = ""
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
        }
    }
}

// MARK: Repeating mistakes

private struct ClustersSection: View {
    var body: some View {
        let clusters = EvolutionModel.shared.data.clusters.list
        SectionHead(title: "Mistakes that keep repeating") {
            Text("last 14 days").font(.caption).foregroundStyle(.secondary)
        }
        Card {
            if clusters.isEmpty {
                EmptyNote(title: "Nothing repeating", detail: "No failure has repeated three times in two weeks.")
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(clusters, id: \.signature.str) { cluster in
                        if cluster.signature != clusters.first?.signature { Divider() }
                        ClusterRow(cluster: cluster)
                    }
                }
            }
        }
    }
}

private struct ClusterRow: View {
    @Environment(Store.self) private var store
    let cluster: JSON

    private var look: (tone: Color, icon: String, status: String, label: String) {
        switch cluster.state.str {
        case "lesson": return (MC.good, "checkmark", "completed", "Lesson in place")
        case "held": return (MC.warn, "testtube.2", "pending", "Lesson waiting on you")
        case "owner": return (.secondary, "xmark", "retired", "You retired its lesson")
        case "gave-up": return (MC.danger, "exclamationmark.triangle", "failed", "Two lessons failed: needs a fix")
        default: return (MC.warn, "exclamationmark.triangle", "pending", "No lesson yet")
        }
    }

    var body: some View {
        let look = look
        let agents = cluster.agents.dict.keys.sorted().map(store.name).joined(separator: ", ")
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: look.icon).foregroundStyle(look.tone).frame(width: 18).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(cluster.signature.str)
                    .font(.system(size: 12, design: .monospaced))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                Text("\(plural(cluster.count.int, "time")) · \(agents) · last \(ago(cluster.last.num))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Pill(status: look.status, label: look.label)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }
}

// MARK: Last review

private struct ReviewSection: View {
    private struct Change: Identifiable {
        let id: String
        let icon: String, tone: Color, label: String, text: String
    }

    var body: some View {
        let data = EvolutionModel.shared.data
        let review = data.last_review
        let report = review.report
        let reviewAt = data.settings.review_at.str.isEmpty ? "03:30" : data.settings.review_at.str
        let changes = changelog(report)

        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("Last review").font(.headline)
            Text(review.isNull ? "not run yet" : Date(timeIntervalSince1970: review.ts.num).formatted(.dateTime.weekday(.abbreviated).hour().minute()))
                .font(.caption).foregroundStyle(.secondary)
            Spacer()
            Text("\(data.calls_today.int)/\(data.daily_calls.int) learning calls today · next review after \(reviewAt)")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(.top, 8)
        Card(padding: 14) {
            if review.isNull {
                Text("The first review runs a few minutes after the app starts, then every night.")
                    .font(.system(size: 13)).foregroundStyle(.secondary)
            } else {
                Flow(spacing: 22, lineSpacing: 6) {
                    tally(report.adopted.list.count, "adopted")
                    tally(report.retired.list.count, "retired")
                    tally(report.learned.list.count, "learned from failures")
                    regression(report.regression)
                }
                .font(.system(size: 13))
            }
        }
        if !changes.isEmpty {
            Card {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(changes) { change in
                        if change.id != changes.first?.id { Divider() }
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: change.icon).foregroundStyle(change.tone).frame(width: 18).accessibilityHidden(true)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(change.label).font(.caption).foregroundStyle(.secondary)
                                Text(change.text).font(.system(size: 13)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .padding(.horizontal, 14).padding(.vertical, 10)
                        .accessibilityElement(children: .combine)
                    }
                }
            }
        }
    }

    private func tally(_ count: Int, _ word: String) -> some View {
        HStack(spacing: 4) {
            Text("\(count)").fontWeight(.semibold).monospacedDigit()
            Text(word).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private func regression(_ guarded: JSON) -> some View {
        if guarded.isNull {
            Text("Regression guard held").foregroundStyle(.secondary)
        } else {
            Label("Regression guard fired: \(percent(guarded.baseline)) → \(percent(guarded.rate))", systemImage: "exclamationmark.triangle")
                .labelStyle(.titleAndIcon)
                .foregroundStyle(MC.warn)
        }
    }

    /// What the night changed, in words.
    private func changelog(_ report: JSON) -> [Change] {
        var rows: [Change] = []
        func add(_ items: [JSON], _ icon: String, _ tone: Color, _ label: String, _ text: (JSON) -> String = { $0.text.str }) {
            for item in items {
                rows.append(Change(id: "\(rows.count)", icon: icon, tone: tone, label: label, text: text(item)))
            }
        }
        add(report.adopted.list, "checkmark", MC.good, "Adopted")
        add(report.learned.list, "sparkles", MC.info, "Learned")
        add(report.retired.list, "xmark", .secondary, "Retired, it did not help")
        add(report.regression.suspended.list, "exclamationmark.triangle", MC.warn, "Suspended")
        add(report.attention.list, "exclamationmark.triangle", MC.warn, "Needs you") { "\($0.text.str) (\($0.why.str))" }
        return rows
    }
}
