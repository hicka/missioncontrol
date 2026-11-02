import AppKit
import Metal
import QuartzCore
import simd

/// A value that moves like something with weight. Everything that changes shape on the bar
/// goes through one of these, which is why it overshoots and settles instead of sliding.
struct Spring {
    var value: Float
    var target: Float
    var velocity: Float = 0
    var response: Float = 0.36
    var damping: Float = 0.74

    init(_ value: Float, response: Float = 0.36, damping: Float = 0.74) {
        self.value = value; target = value; self.response = response; self.damping = damping
    }

    mutating func step(_ dt: Float) {
        guard moving else { value = target; velocity = 0; return }
        let stiffness = pow(2 * .pi / response, 2)
        let friction = 2 * damping * sqrt(stiffness)
        var left = dt
        while left > 0 {
            let slice = min(left, 1.0 / 240)
            velocity += (stiffness * (target - value) - friction * velocity) * slice
            value += velocity * slice
            left -= slice
        }
    }

    mutating func snap(_ to: Float) { value = to; target = to; velocity = 0 }
    var moving: Bool { abs(value - target) > 0.04 || abs(velocity) > 0.3 }
}

/// How each state of the bar looks. Colour and energy glide from one to the next.
private func look(_ pulse: Pulse) -> (a: SIMD3<Float>, b: SIMD3<Float>, c: SIMD3<Float>, energy: Float) {
    let blue = SIMD3<Float>(0.25, 0.55, 1), violet = SIMD3<Float>(0.62, 0.36, 1), pink = SIMD3<Float>(1, 0.32, 0.62)
    switch pulse {
    case .offline: return (.init(0.34, 0.34, 0.38), .init(0.42, 0.42, 0.46), .init(0.28, 0.28, 0.32), 0.03)
    case .idle: return (.init(0.34, 0.44, 0.82), .init(0.52, 0.38, 0.86), .init(0.3, 0.58, 0.82), 0.1)
    case .thinking: return (blue, violet, pink, 0.62)
    case .streaming: return (violet, pink, .init(1, 0.58, 0.28), 0.74)
    case .working: return (blue, .init(0.2, 0.85, 0.92), .init(0.3, 0.95, 0.68), 0.5)
    case .attention: return (.init(0.98, 0.7, 0.25), .init(1, 0.45, 0.2), .init(1, 0.85, 0.42), 0.42)
    case .listening: return (blue, violet, pink, 0.55)
    case .speaking: return (.init(0.3, 0.9, 0.85), violet, blue, 0.5)
    }
}

private struct Uniforms {
    var bar: SIMD4<Float>, peek: SIMD4<Float>
    var colorA: SIMD4<Float>, colorB: SIMD4<Float>, colorC: SIMD4<Float>
    var size: SIMD2<Float>, orb: SIMD2<Float>, pointer: SIMD2<Float>
    var scale: Float, time: Float, rim: Float, energy: Float
    var level: Float, pulse: Float, barRadius: Float, peekRadius: Float
    var peekAlpha: Float, orbRadius: Float, pointerGlow: Float, goo: Float
    var rippleCount: Int32, satCount: Int32
}

/// Draws the bar with Metal, on its own display link. SwiftUI lays out the text on top and
/// says where things are; none of this motion goes through SwiftUI, so an animation frame
/// costs one small draw call rather than a view update, and the bar can stay alive all day.
@MainActor
final class AuraView: NSView {
    struct Shapes: Equatable { var bar: CGRect?; var peek: CGRect?; var orb: CGRect? }

    /// False where Metal could not start; the bar then draws a plain dark shape instead.
    private(set) var available = false
    var onUnavailable: (() -> Void)?
    /// Runs before each frame, with the seconds since the last one.
    var onTick: ((Float) -> Void)?
    var pulseState: Pulse = .offline { didSet { if pulseState != oldValue { setLook(pulseState) } } }
    var level: Float = 0
    /// Keeps the frame rate up while the bar is open.
    var keepLively = false
    private(set) var lean = SIMD2<Float>(0, 0)
    private(set) var pointerDistance: Float = .infinity

    private let metalLayer = CAMetalLayer()
    private var queue: MTLCommandQueue?
    private var pipeline: MTLRenderPipelineState?
    private var link: CADisplayLink?
    private var lastTime: CFTimeInterval = 0
    private enum Pace { case still, steady, full }
    private var pace = Pace.full

    // Shapes are held relative to the bottom centre of the window. That is the one point
    // that stays put when the window grows, so growing never reads as motion.
    private var barX = Spring(0), barY = Spring(-68), barW = Spring(30), barH = Spring(22), barRadius = Spring(22)
    private var peekX = Spring(0), peekY = Spring(-90), peekW = Spring(30), peekH = Spring(6), peekRadius = Spring(10)
    private var peekAlpha = Spring(0, response: 0.3, damping: 1)
    private var orbX = Spring(-18, response: 0.32, damping: 0.8), orbY = Spring(-68, response: 0.32, damping: 0.8), orbR = Spring(11, response: 0.3, damping: 0.62)
    private var press = Spring(0, response: 0.22, damping: 0.55)
    private var goo = Spring(8, response: 0.5, damping: 1)
    private var leanX = Spring(0, response: 0.42, damping: 0.62), leanY = Spring(0, response: 0.42, damping: 0.62)
    private var stretchX = Spring(0, response: 0.3, damping: 0.5), stretchY = Spring(0, response: 0.3, damping: 0.5)
    private var hasShape = false, hasPeek = false

    private var color = (a: SIMD3<Float>(0.34, 0.34, 0.38), b: SIMD3<Float>(0.42, 0.42, 0.46), c: SIMD3<Float>(0.28, 0.28, 0.32))
    private var targetColor = (a: SIMD3<Float>(0.34, 0.34, 0.38), b: SIMD3<Float>(0.42, 0.42, 0.46), c: SIMD3<Float>(0.28, 0.28, 0.32))
    private var energy: Float = 0.03, targetEnergy: Float = 0.03
    private var pulse: Float = 0, time: Float = 0, rim: Float = 0, smoothLevel: Float = 0
    private var pointer = SIMD2<Float>(-999, -999), pointerGlow: Float = 0
    private var beat: Float = 0

    private struct Ripple { var age: Float = 0; let tint: SIMD3<Float>; let strength: Float; let speed: Float; let life: Float }
    private var ripples: [Ripple] = []

    private struct Satellite { let id: String; let tint: SIMD3<Float>; var angle: Float; let speed: Float; var alpha: Spring; var orbit: Spring; var leaving = false }
    private var satellites: [Satellite] = []

    private let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        setUpMetal()
    }
    required init?(coder: NSCoder) { fatalError("not from a nib") }

    override func makeBackingLayer() -> CALayer { metalLayer }
    override var wantsUpdateLayer: Bool { true }
    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    /// The shader ships as source and compiles here, off the main thread. Metal caches the
    /// result, and building the app needs no Metal toolchain, which recent Xcode leaves out.
    private func setUpMetal() {
        guard let device = MTLCreateSystemDefaultDevice(),
              let url = Bundle.main.url(forResource: "BarAura", withExtension: "metal"),
              let source = try? String(contentsOf: url, encoding: .utf8) else { return }
        queue = device.makeCommandQueue()
        metalLayer.device = device
        metalLayer.pixelFormat = .bgra8Unorm
        metalLayer.isOpaque = false
        metalLayer.framebufferOnly = true
        metalLayer.backgroundColor = .clear
        available = queue != nil
        device.makeLibrary(source: source, options: nil) { [weak self] library, error in
            var built: MTLRenderPipelineState?
            if let library, let vertex = library.makeFunction(name: "aura_vertex"), let fragment = library.makeFunction(name: "aura_fragment") {
                let descriptor = MTLRenderPipelineDescriptor()
                descriptor.vertexFunction = vertex
                descriptor.fragmentFunction = fragment
                descriptor.colorAttachments[0].pixelFormat = .bgra8Unorm
                built = try? device.makeRenderPipelineState(descriptor: descriptor)
            }
            if let error, built == nil { FileHandle.standardError.write(Data("Bar shader: \(error)\n".utf8)) }
            let state = UncheckedBox(built)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.pipeline = state.value
                if state.value == nil { self.available = false; self.onUnavailable?() }
            }
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        link?.invalidate(); link = nil
        guard window != nil, available else { return }
        let next = displayLink(target: self, selector: #selector(frameTick(_:)))
        next.add(to: .main, forMode: .common)
        link = next
        setPace(.full)
        resizeDrawable()
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        resizeDrawable()
    }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        resizeDrawable()
    }

    private func resizeDrawable() {
        let scale = window?.backingScaleFactor ?? 2
        metalLayer.contentsScale = scale
        metalLayer.drawableSize = CGSize(width: bounds.width * scale, height: bounds.height * scale)
    }

    func setPaused(_ paused: Bool) { link?.isPaused = paused }

    /// Only what is moving fast is drawn at full rate. Slow drift reads as smooth at far less,
    /// and a quiet bar is most of the day.
    private func setPace(_ next: Pace) {
        pace = next
        switch next {
        case .still: link?.preferredFrameRateRange = CAFrameRateRange(minimum: 8, maximum: 15, preferred: 12)
        case .steady: link?.preferredFrameRateRange = CAFrameRateRange(minimum: 20, maximum: 30, preferred: 30)
        case .full: link?.preferredFrameRateRange = CAFrameRateRange(minimum: 30, maximum: 60, preferred: 60)
        }
    }

    private func setLook(_ pulse: Pulse) {
        let next = look(pulse)
        targetColor = (next.a, next.b, next.c)
        targetEnergy = next.energy
    }

    // MARK: What the controller tells it

    func update(_ shapes: Shapes, cornerRadius: CGFloat) {
        if let rect = shapes.bar {
            let centre = SIMD2<Float>(Float(rect.midX), Float(rect.midY))
            barX.target = centre.x; barY.target = centre.y
            barW.target = Float(rect.width / 2); barH.target = Float(rect.height / 2)
            barRadius.target = Float(cornerRadius)
            if !hasShape {
                hasShape = true
                barX.snap(centre.x); barY.snap(centre.y); barW.snap(Float(rect.width / 2)); barH.snap(Float(rect.height / 2)); barRadius.snap(Float(cornerRadius))
            }
        }
        if let rect = shapes.peek {
            if !hasPeek {
                // A droplet starts inside the top of the bar, and pushes its way out.
                hasPeek = true
                peekX.snap(barX.value); peekY.snap(barY.value - barH.value + 6); peekW.snap(34); peekH.snap(6); peekRadius.snap(6)
                goo.value = 42
            }
            peekX.target = Float(rect.midX); peekY.target = Float(rect.midY)
            peekW.target = Float(rect.width / 2); peekH.target = Float(rect.height / 2); peekRadius.target = 18
            peekAlpha.target = 1
        } else if hasPeek {
            hasPeek = false
            peekX.target = barX.target; peekY.target = barY.target - barH.target + 6
            peekW.target = 34; peekH.target = 6; peekRadius.target = 6
            peekAlpha.target = 0
            goo.value = 42
        }
        if let rect = shapes.orb {
            orbX.target = Float(rect.midX); orbY.target = Float(rect.midY)
            orbR.target = Float(rect.width / 2)
            if orbX.value == -18 { orbX.snap(orbX.target); orbY.snap(orbY.target) }
        }
    }

    /// A word of the reply arrived. Each one lands as a small kick of light.
    func token() { pulse = min(pulse + 0.2, 1); orbR.velocity += 3 }

    func setPressed(_ pressed: Bool) { press.target = pressed ? 1 : 0 }

    func stretch(vx: Float, vy: Float) {
        stretchX.target = max(-1, min(1, vx / 1800))
        stretchY.target = max(-1, min(1, vy / 1800))
    }

    /// Light leaving the orb, crossing the bar, and setting the edge off as it passes.
    func ripple(_ tint: SIMD3<Float>, strength: Float = 1, speed: Float = 240, life: Float = 1.1) {
        guard !reduceMotion else { return }
        ripples.append(Ripple(tint: tint, strength: strength, speed: speed, life: life))
        if ripples.count > 4 { ripples.removeFirst() }
        orbR.velocity += 40 * strength
    }

    func nudge() {
        guard !reduceMotion else { return }
        press.velocity += 7
        leanY.velocity -= 90
    }

    /// One light per running task. A task that ends falls into the orb and sets off a ripple.
    func setSatellites(_ wanted: [(id: String, tint: SIMD3<Float>)]) {
        for index in satellites.indices where !satellites[index].leaving && !wanted.contains(where: { $0.id == satellites[index].id }) {
            satellites[index].leaving = true
            satellites[index].orbit.target = 0
            satellites[index].alpha.target = 0
            ripple(satellites[index].tint, strength: 0.9, speed: 200)
        }
        for item in wanted.prefix(8) where !satellites.contains(where: { $0.id == item.id }) {
            var orbit = Spring(0, response: 0.7, damping: 0.6); orbit.target = 1
            var alpha = Spring(0, response: 0.4, damping: 1); alpha.target = 1
            let spread = Float(satellites.count) * 1.05
            satellites.append(Satellite(id: item.id, tint: item.tint, angle: spread, speed: 1.1 + Float(satellites.count) * 0.13, alpha: alpha, orbit: orbit))
        }
    }

    // MARK: Pointer

    /// In the view's coordinates, from the top left.
    func pointerMoved(to point: CGPoint) {
        pointer = SIMD2(Float(point.x), Float(point.y))
        let centre = SIMD2<Float>(Float(bounds.width) / 2 + barX.value, Float(bounds.height) + barY.value)
        let delta = pointer - centre
        let edge = abs(delta) - SIMD2(barW.value, barH.value)
        let distance = max(edge.x, edge.y)
        pointerDistance = max(distance, 0)
        guard !reduceMotion else { pointerGlow = 0; return }
        let reach: Float = 150
        let closeness = max(0, 1 - pointerDistance / reach)
        pointerGlow += (closeness * closeness - pointerGlow) * 0.2
        if keepLively { leanX.target = 0; leanY.target = 0; return }
        // It leans toward the pointer as if drawn to it, and lets go when it leaves.
        let length = max(simd_length(delta), 1)
        leanX.target = delta.x / length * 7 * closeness * closeness
        leanY.target = delta.y / length * 5 * closeness * closeness
    }

    func contains(_ point: CGPoint, slop: Float = 3) -> Bool {
        let p = SIMD2<Float>(Float(point.x), Float(point.y))
        let width = Float(bounds.width), height = Float(bounds.height)
        func inside(_ x: Float, _ y: Float, _ w: Float, _ h: Float) -> Bool {
            abs(p.x - (width / 2 + x)) <= w + slop && abs(p.y - (height + y)) <= h + slop
        }
        return inside(barX.value + leanX.value, barY.value + leanY.value, barW.value, barH.value) || (hasPeek && inside(peekX.value, peekY.value, peekW.value, peekH.value))
    }

    // MARK: A frame

    @objc private func frameTick(_ link: CADisplayLink) {
        let now = link.timestamp
        let dt = min(Float(now - lastTime), 1.0 / 15)
        lastTime = now
        onTick?(dt)
        step(dt)
        draw()
        let next: Pace = smoothLevel > 0.01 || pulse > 0.02 || !ripples.isEmpty || pointerGlow > 0.04 || anyMoving ? .full
            : energy > 0.2 || keepLively || !satellites.isEmpty ? .steady : .still
        if next != pace { setPace(next) }
    }

    private var anyMoving: Bool {
        barX.moving || barY.moving || barW.moving || barH.moving || peekAlpha.moving || peekY.moving || orbX.moving || orbR.moving || press.moving || leanX.moving || leanY.moving || stretchX.moving || stretchY.moving || satellites.contains { $0.orbit.moving }
    }

    private func step(_ dt: Float) {
        barX.step(dt); barY.step(dt); barW.step(dt); barH.step(dt); barRadius.step(dt)
        peekX.step(dt); peekY.step(dt); peekW.step(dt); peekH.step(dt); peekRadius.step(dt); peekAlpha.step(dt)
        orbX.step(dt); orbY.step(dt); orbR.step(dt)
        press.step(dt); goo.step(dt); leanX.step(dt); leanY.step(dt); stretchX.step(dt); stretchY.step(dt)
        goo.target = peekY.moving || peekH.moving ? 34 : 7
        let glide = min(1, dt * 3.5)
        color.a += (targetColor.a - color.a) * glide
        color.b += (targetColor.b - color.b) * glide
        color.c += (targetColor.c - color.c) * glide
        beat += dt
        // Waiting on the owner has a slow heartbeat rather than a constant glow.
        let heartbeat: Float = pulseState == .attention ? 0.16 * pow(max(0, sin(beat * 2.1)), 6) : 0
        energy += (targetEnergy + heartbeat - energy) * min(1, dt * 3)
        smoothLevel += (level - smoothLevel) * min(1, dt * 16)
        pulse *= max(0, 1 - dt * 3.2)
        if !reduceMotion {
            time += dt * (0.32 + energy * 1.0 + smoothLevel)
            rim += dt * (0.025 + energy * 0.2 + smoothLevel * 0.25 + pulse * 0.2)
        }
        for index in ripples.indices { ripples[index].age += dt }
        ripples.removeAll { $0.age > $0.life }
        for index in satellites.indices {
            satellites[index].alpha.step(dt)
            satellites[index].orbit.step(dt)
            let speedUp: Float = satellites[index].leaving ? 5 : 1
            if !reduceMotion { satellites[index].angle += dt * satellites[index].speed * speedUp }
        }
        satellites.removeAll { $0.leaving && $0.alpha.value < 0.02 }
        lean = SIMD2(leanX.value, leanY.value)
    }

    private func draw() {
        guard let pipeline, let queue, bounds.width > 0, bounds.height > 0, let drawable = metalLayer.nextDrawable() else { return }
        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = drawable.texture
        pass.colorAttachments[0].loadAction = .clear
        pass.colorAttachments[0].storeAction = .store
        pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 0, 0, 0)
        guard let buffer = queue.makeCommandBuffer(), let encoder = buffer.makeRenderCommandEncoder(descriptor: pass) else { return }

        let width = Float(bounds.width), height = Float(bounds.height), scale = Float(metalLayer.contentsScale)
        let squish = press.value
        // It gives a little when pressed, and stretches along the way it is being thrown.
        let halfW = barW.value * (1 - 0.03 * squish) * (1 + abs(stretchX.value) * 0.12 - abs(stretchY.value) * 0.05)
        let halfH = barH.value * (1 - 0.05 * squish) * (1 + abs(stretchY.value) * 0.16 - abs(stretchX.value) * 0.1)
        let barCentre = SIMD2<Float>(width / 2 + barX.value + leanX.value, height + barY.value + leanY.value)
        let orbCentre = SIMD2<Float>(width / 2 + orbX.value + leanX.value, height + orbY.value + leanY.value)
        let radius = min(barRadius.value, min(halfW, halfH))

        var uniforms = Uniforms(
            bar: SIMD4(barCentre.x, barCentre.y, max(halfW, 1), max(halfH, 1)),
            peek: SIMD4(width / 2 + peekX.value + leanX.value * 0.6, height + peekY.value + leanY.value * 0.6, max(peekW.value, 1), max(peekH.value, 1)),
            colorA: SIMD4(color.a, 1), colorB: SIMD4(color.b, 1), colorC: SIMD4(color.c, 1),
            size: SIMD2(width, height), orb: orbCentre, pointer: pointer,
            scale: scale, time: time, rim: rim, energy: energy,
            level: smoothLevel, pulse: pulse, barRadius: radius, peekRadius: min(peekRadius.value, min(peekW.value, peekH.value)),
            peekAlpha: peekAlpha.value, orbRadius: max(orbR.value, 0), pointerGlow: pointerGlow, goo: max(goo.value, 1),
            rippleCount: Int32(ripples.count), satCount: Int32(satellites.count))

        var waves = [SIMD4<Float>](repeating: .zero, count: 4), waveTints = waves
        for (index, ripple) in ripples.prefix(4).enumerated() {
            let progress = ripple.age / ripple.life
            let reach = ripple.age * ripple.speed
            waves[index] = SIMD4(reach, 7 + reach * 0.14, ripple.strength * pow(1 - progress, 2), 0)
            waveTints[index] = SIMD4(ripple.tint, 1)
        }
        var dots = [SIMD4<Float>](repeating: .zero, count: 8), dotTints = dots
        for (index, satellite) in satellites.prefix(8).enumerated() {
            // A ring seen at an angle, like Saturn's, tilted the same way as the one the shader
            // draws. Its near half passes in front of the orb and its far half behind.
            let orbit = satellite.orbit.value
            let alongX = cos(satellite.angle) * orbR.value * 1.8 * orbit, alongY = sin(satellite.angle) * orbR.value * 0.62 * orbit
            let offset = SIMD2<Float>(alongX * 0.9135 + alongY * 0.4067, -alongX * 0.4067 + alongY * 0.9135)
            let depth = sin(satellite.angle)
            let hidden = depth < 0 && simd_length(offset) < orbR.value
            dots[index] = SIMD4(orbCentre.x + offset.x, orbCentre.y + offset.y, 1.9 + depth * 0.5, satellite.alpha.value * (hidden ? 0 : depth < 0 ? 0.6 : 1))
            dotTints[index] = SIMD4(satellite.tint, 1)
        }

        encoder.setRenderPipelineState(pipeline)
        encoder.setFragmentBytes(&uniforms, length: MemoryLayout<Uniforms>.stride, index: 0)
        encoder.setFragmentBytes(waves, length: MemoryLayout<SIMD4<Float>>.stride * 4, index: 1)
        encoder.setFragmentBytes(waveTints, length: MemoryLayout<SIMD4<Float>>.stride * 4, index: 2)
        encoder.setFragmentBytes(dots, length: MemoryLayout<SIMD4<Float>>.stride * 8, index: 3)
        encoder.setFragmentBytes(dotTints, length: MemoryLayout<SIMD4<Float>>.stride * 8, index: 4)
        encoder.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
        encoder.endEncoding()
        buffer.present(drawable)
        buffer.commit()
    }
}

private struct UncheckedBox<Value>: @unchecked Sendable {
    let value: Value
    init(_ value: Value) { self.value = value }
}

/// An agent's colour as the renderer wants it.
func tintVector(hex: String) -> SIMD3<Float> {
    let digits = hex.trimmingCharacters(in: CharacterSet(charactersIn: "# "))
    guard digits.count == 6, let value = UInt32(digits, radix: 16) else { return SIMD3(0.6, 0.7, 1) }
    let tint = SIMD3<Float>(Float((value >> 16) & 255) / 255, Float((value >> 8) & 255) / 255, Float(value & 255) / 255)
    // A very dark profile colour would disappear against the bar.
    return simd_length(tint) < 0.5 ? SIMD3(0.75, 0.78, 0.85) : tint
}
