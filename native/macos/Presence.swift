import SwiftUI

/// How a profile is, as its presence shows it.
enum Mood: String, Equatable, Sendable {
    case idle, thinking, working, fixing, waiting, needsYou, happy, sad, offline

    /// Something is going on that is worth the eye: these move at any size.
    var active: Bool { [.thinking, .working, .fixing, .needsYou, .happy].contains(self) }
}

/// What a presence is doing with its shape: the part of a state that can be blended, so
/// one turns into the next instead of swapping.
struct PresenceParams: VectorArithmetic, Equatable {
    var wobble = 0.03     // how far the edge moves from a circle
    var jag = 0.0         // a finer, rougher ripple on top of it
    var arc = 0.0         // a bright arc travelling the rim
    var moon = 0.0        // a small body in orbit
    var ripple = 0.0      // rings going out
    var warm = 0.0        // accents turning amber
    var dim = 0.0         // colour and light drained
    var hollow = 0.0      // an outline with nothing in it
    var tick = 0.0        // a mark of something finished

    static var zero: PresenceParams { PresenceParams(wobble: 0) }
    static func + (a: PresenceParams, b: PresenceParams) -> PresenceParams {
        PresenceParams(wobble: a.wobble + b.wobble, jag: a.jag + b.jag, arc: a.arc + b.arc, moon: a.moon + b.moon,
                       ripple: a.ripple + b.ripple, warm: a.warm + b.warm, dim: a.dim + b.dim, hollow: a.hollow + b.hollow,
                       tick: a.tick + b.tick)
    }
    static func - (a: PresenceParams, b: PresenceParams) -> PresenceParams { var c = b; c.scale(by: -1); return a + c }
    mutating func scale(by k: Double) {
        wobble *= k; jag *= k; arc *= k; moon *= k; ripple *= k; warm *= k; dim *= k; hollow *= k; tick *= k
    }
    var magnitudeSquared: Double {
        wobble * wobble + jag * jag + arc * arc + moon * moon + ripple * ripple + warm * warm + dim * dim + hollow * hollow + tick * tick
    }

    static func of(_ mood: Mood) -> PresenceParams {
        switch mood {
        case .idle: return PresenceParams()
        case .thinking: return PresenceParams(wobble: 0.055, moon: 1)
        case .working: return PresenceParams(wobble: 0.04, arc: 1)
        case .fixing: return PresenceParams(wobble: 0.07, jag: 1, arc: 1, warm: 1)
        case .waiting: return PresenceParams(wobble: 0.015, dim: 0.45)
        case .needsYou: return PresenceParams(wobble: 0.05, ripple: 1, warm: 1)
        case .happy: return PresenceParams(wobble: 0.02, tick: 1)
        case .sad: return PresenceParams(wobble: 0.01, dim: 0.75)
        case .offline: return PresenceParams(wobble: 0, dim: 1, hollow: 1)
        }
    }
}

/// A profile, as a body of its own colour that moves the way the profile is. At rest it
/// breathes. Thinking, something circles it. Working, light runs round its rim. Fixing,
/// its edge roughens and the light turns amber. Waiting, it dims and slows. When it needs
/// you, rings go out from it. Finished, it holds a tick for a moment. No face: how it is
/// shows in how it moves.
struct Presence: View {
    let color: Color
    let mood: Mood
    var size: CGFloat = 40
    /// So a row of them does not move in step.
    var seed: Int = 0
    @Environment(\.accessibilityReduceMotion) private var still
    @State private var shown = PresenceParams()
    @State private var began = false

    /// A window can hold dozens of these. One at rest only breathes when it is large enough
    /// for that to be seen; a small one moves when it has something to say.
    private var moves: Bool {
        if still || mood == .offline || mood == .sad { return false }
        return mood.active || size >= 36
    }

    var body: some View {
        Group {
            if !moves {
                PresenceDrawing(params: PresenceParams.of(mood), mood: mood, color: color, time: 0, seed: seed, moving: false)
            } else {
                TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
                    PresenceDrawing(params: shown, mood: mood, color: color, time: context.date.timeIntervalSinceReferenceDate,
                                    seed: seed, moving: true)
                }
            }
        }
        .frame(width: size, height: size)
        .onAppear { if !began { shown = PresenceParams.of(mood); began = true } }
        .onChange(of: mood) { _, next in
            withAnimation(.spring(response: 0.5, dampingFraction: 0.7)) { shown = PresenceParams.of(next) }
        }
        .accessibilityElement()
        .accessibilityLabel(Self.spoken(mood))
    }

    static func spoken(_ mood: Mood) -> String {
        switch mood {
        case .idle: return "At rest"
        case .thinking: return "Thinking"
        case .working: return "Working"
        case .fixing: return "Fixing something"
        case .waiting: return "Waiting"
        case .needsYou: return "Needs you"
        case .happy: return "Finished"
        case .sad: return "Could not finish"
        case .offline: return "Not connected"
        }
    }
}

private struct PresenceDrawing: View, Animatable {
    var params: PresenceParams
    let mood: Mood
    let color: Color
    let time: Double
    let seed: Int
    let moving: Bool

    var animatableData: PresenceParams {
        get { params }
        set { params = newValue }
    }

    var body: some View {
        Canvas { context, size in draw(&context, size) }
    }

    private var t: Double { moving ? time + Double(abs(seed) % 97) * 0.41 : 0 }

    private func draw(_ context: inout GraphicsContext, _ size: CGSize) {
        let s = min(size.width, size.height)
        let p = params
        let centre = CGPoint(x: size.width / 2, y: size.height / 2)
        let amber = Color(red: 1, green: 0.66, blue: 0.2)
        let accent = p.warm > 0.5 ? amber : Color.white
        var layer = context
        if p.dim > 0.01 { layer.addFilter(.saturation(1 - p.dim * 0.8)); layer.opacity = 1 - p.dim * 0.4 }

        // Rings going out, for one that wants the owner.
        if p.ripple > 0.02 {
            for index in 0..<2 {
                let along = moving ? ((t * 0.75 + Double(index) * 0.5).truncatingRemainder(dividingBy: 1)) : 0.5 * Double(index + 1) * 0.6
                let r = s * (0.3 + 0.2 * along)
                layer.stroke(Path(ellipseIn: CGRect(x: centre.x - r, y: centre.y - r, width: r * 2, height: r * 2)),
                             with: .color(amber.opacity((1 - along) * 0.7 * p.ripple)), lineWidth: max(1, s * 0.035))
            }
        }

        // The body. Breathing changes its size a little; the edge moves by the state.
        let speed = mood == .fixing ? 3.4 : mood == .waiting || mood == .sad ? 0.5 : mood == .thinking ? 1.7 : 1.1
        let breath = moving ? sin(t * (mood == .waiting ? 0.9 : 1.6)) * (mood == .waiting ? 0.035 : 0.018) : 0
        let lift = mood == .happy && moving ? abs(sin(t * 4.2)) * 0.05 : 0
        let radius = s * (0.315 + breath)
        let mid = CGPoint(x: centre.x, y: centre.y - lift * s + (mood == .sad ? s * 0.02 : 0))
        var body = Path()
        let points = 72
        for index in 0...points {
            let angle = Double(index) / Double(points) * 2 * .pi
            let ripple = sin(angle * 3 + t * speed) * 0.6 + sin(angle * 5 - t * speed * 1.3) * 0.4
                + p.jag * sin(angle * 11 + t * speed * 2.1) * 0.45
            // Sagging a little when it could not finish.
            let squash = mood == .sad ? 1 - 0.06 * max(0, -sin(angle)) : 1
            let r = radius * (1 + p.wobble * ripple) * squash
            let point = CGPoint(x: mid.x + cos(angle) * r, y: mid.y + sin(angle) * r)
            if index == 0 { body.move(to: point) } else { body.addLine(to: point) }
        }
        body.closeSubpath()

        if p.hollow < 0.98 {
            var glow = layer
            glow.addFilter(.blur(radius: s * 0.09))
            glow.fill(body, with: .color(color.opacity(0.4 * (1 - p.hollow) * (1 - p.dim * 0.7))))
            layer.fill(body, with: .radialGradient(
                Gradient(stops: [.init(color: color.opacity(0.62 * (1 - p.hollow)), location: 0),
                                 .init(color: color.opacity(0.98 * (1 - p.hollow)), location: 0.72),
                                 .init(color: color.opacity(0.86 * (1 - p.hollow)), location: 1)]),
                center: CGPoint(x: mid.x - radius * 0.28, y: mid.y - radius * 0.34), startRadius: 0, endRadius: radius * 1.5))
            // Light on its upper left, as on anything round.
            var shine = layer
            shine.addFilter(.blur(radius: s * 0.045))
            shine.fill(Path(ellipseIn: CGRect(x: mid.x - radius * 0.62, y: mid.y - radius * 0.7, width: radius * 0.7, height: radius * 0.5)),
                       with: .color(.white.opacity(0.38 * (1 - p.hollow) * (1 - p.dim * 0.6))))
        }
        if p.hollow > 0.02 {
            layer.stroke(body, with: .color(color.opacity(0.7 * p.hollow)), style: StrokeStyle(lineWidth: max(1, s * 0.05), dash: [s * 0.08, s * 0.07]))
        }

        // Light running round the rim while it works.
        if p.arc > 0.02 {
            let turn = moving ? (t * (mood == .fixing ? 1.5 : 1.0)).truncatingRemainder(dividingBy: 1) * 2 * Double.pi : 0.6
            let r = radius + s * 0.085
            var arc = Path()
            arc.addArc(center: mid, radius: r, startAngle: .radians(turn), endAngle: .radians(turn + 1.9), clockwise: false)
            layer.stroke(arc, with: .color((p.warm > 0.5 ? amber : color).opacity(0.95 * p.arc)),
                         style: StrokeStyle(lineWidth: max(1.5, s * 0.055), lineCap: .round))
            var tail = Path()
            tail.addArc(center: mid, radius: r, startAngle: .radians(turn + 3.3), endAngle: .radians(turn + 4.2), clockwise: false)
            layer.stroke(tail, with: .color((p.warm > 0.5 ? amber : color).opacity(0.4 * p.arc)),
                         style: StrokeStyle(lineWidth: max(1.5, s * 0.055), lineCap: .round))
        }

        // Something circling it while it thinks, passing in front and behind.
        if p.moon > 0.02 {
            let angle = moving ? t * 2.3 : 0.9
            let orbit = radius + s * 0.1
            let at = CGPoint(x: mid.x + cos(angle) * orbit, y: mid.y + sin(angle) * orbit * 0.42)
            let near = sin(angle) > 0
            let r = s * (near ? 0.062 : 0.045)
            layer.fill(Path(ellipseIn: CGRect(x: at.x - r, y: at.y - r, width: r * 2, height: r * 2)),
                       with: .color(accent.opacity((near ? 0.95 : 0.45) * p.moon)))
        }

        // A tick, held for a moment after it finishes.
        if p.tick > 0.02 {
            var tick = Path()
            tick.move(to: CGPoint(x: mid.x - radius * 0.38, y: mid.y + radius * 0.04))
            tick.addLine(to: CGPoint(x: mid.x - radius * 0.1, y: mid.y + radius * 0.32))
            tick.addLine(to: CGPoint(x: mid.x + radius * 0.42, y: mid.y - radius * 0.3))
            layer.stroke(tick.trimmedPath(from: 0, to: min(1, p.tick)), with: .color(.white.opacity(0.95)),
                         style: StrokeStyle(lineWidth: max(1.5, s * 0.07), lineCap: .round, lineJoin: .round))
        }
    }
}

/// A profile's colour, with one that would vanish on a dark background kept visible.
func presenceColor(_ hex: String) -> Color {
    let digits = hex.trimmingCharacters(in: CharacterSet(charactersIn: "# "))
    guard digits.count == 6, let value = UInt32(digits, radix: 16) else { return Color(red: 0.45, green: 0.5, blue: 0.6) }
    let r = Double((value >> 16) & 255) / 255, g = Double((value >> 8) & 255) / 255, b = Double(value & 255) / 255
    // Near black or near white reads as no colour at all: give it slate.
    if max(r, g, b) < 0.2 || min(r, g, b) > 0.9 { return Color(red: 0.5, green: 0.55, blue: 0.68) }
    return Color(red: r, green: g, blue: b)
}
