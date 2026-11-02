// The floating bar's body, drawn as one fragment shader: a dark glass shape that can bud a
// droplet, aurora light flowing around its edge, a living plasma orb, the tasks orbiting it,
// and ripples of light that excite the edge as they pass. Coordinates are in points from
// the top left of the window. Colour is premultiplied, and may exceed alpha on purpose:
// that is what makes the glow add light to whatever is behind it.
#include <metal_stdlib>
using namespace metal;

struct Uniforms {
    float4 bar;      // centre x, y; half width, half height
    float4 peek;
    float4 colorA, colorB, colorC;
    float2 size, orb, pointer;
    float scale, time, rim, energy;
    float level, pulse, barRadius, peekRadius;
    float peekAlpha, orbRadius, pointerGlow, goo;
    int rippleCount, satCount;
};

vertex float4 aura_vertex(uint id [[vertex_id]]) {
    float2 corner = float2((id << 1) & 2, id & 2);
    return float4(corner * 2.0 - 1.0, 0.0, 1.0);
}

static float hash(float2 p) {
    p = fract(p * float2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}

static float noise(float2 p) {
    float2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + float2(1, 0)), f.x), mix(hash(i + float2(0, 1)), hash(i + float2(1, 1)), f.x), f.y);
}

static float fbm(float2 p) {
    const float2x2 turn = float2x2(0.8, 0.6, -0.6, 0.8);
    float value = 0.0, amplitude = 0.5;
    for (int i = 0; i < 3; i++) {
        value += amplitude * noise(p);
        p = turn * p * 2.03 + 7.1;
        amplitude *= 0.5;
    }
    return value;
}

static float sdRound(float2 p, float2 half_, float radius) {
    float2 q = abs(p) - half_ + radius;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - radius;
}

// The union that makes two shapes behave like liquid: as they part, a neck stretches and snaps.
static float smin(float a, float b, float k) {
    float h = max(k - abs(a - b), 0.0) / k;
    return min(a, b) - h * h * k * 0.25;
}

static float3 palette(float x, constant Uniforms& u) {
    x = fract(x) * 3.0;
    float f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    if (x < 1.0) return mix(u.colorA.rgb, u.colorB.rgb, f);
    if (x < 2.0) return mix(u.colorB.rgb, u.colorC.rgb, f);
    return mix(u.colorC.rgb, u.colorA.rgb, f);
}

fragment float4 aura_fragment(float4 position [[position]],
                              constant Uniforms& u [[buffer(0)]],
                              constant float4* ripples [[buffer(1)]],
                              constant float4* rippleTint [[buffer(2)]],
                              constant float4* sats [[buffer(3)]],
                              constant float4* satTint [[buffer(4)]]) {
    const float tau = 6.28318530718;
    float2 p = position.xy / u.scale;
    float2 toBar = p - u.bar.xy;
    float dBar = sdRound(toBar, u.bar.zw, u.barRadius);
    float d = dBar;
    if (u.peekAlpha > 0.002) d = smin(dBar, sdRound(p - u.peek.xy, u.peek.zw, u.peekRadius), u.goo);

    float2 toOrb = p - u.orb;
    float orbDistance = length(toOrb);
    float nearestSat = 1e5;
    for (int i = 0; i < u.satCount; i++) nearestSat = min(nearestSat, length(p - sats[i].xy));
    // Most of the window is empty, and nothing out here is worth computing noise for.
    if (d > 74.0 && orbDistance > 60.0 && nearestSat > 20.0) return float4(0.0);

    // Light has to be gone by the window's edge, or the window shows as a hard rectangle.
    float edgeFade = smoothstep(0.0, 24.0, min(min(p.x, p.y), min(u.size.x - p.x, u.size.y - p.y)));

    // Where this point sits around the shape, for the light flowing along the edge.
    float around = atan2(toBar.y / max(u.bar.w, 1.0), toBar.x / max(u.bar.z, 1.0)) / tau;
    float flow = d > -24.0 ? fbm(float2(around * 5.0 + u.rim * 0.6, u.time * 0.35)) : 0.5;
    float3 glowColor = palette(around + u.rim + (flow - 0.5) * 0.55, u);

    // Ripples leave the orb and light up whatever they cross, the edge most of all.
    float excite = 0.0;
    float3 rippleLight = float3(0.0);
    for (int i = 0; i < u.rippleCount; i++) {
        float offset = (orbDistance - ripples[i].x) / ripples[i].y;
        float wave = ripples[i].z * exp(-offset * offset);
        excite += wave;
        rippleLight += rippleTint[i].rgb * wave;
    }
    float pointerDistance = length(p - u.pointer);
    float pointerLight = u.pointerGlow * exp(-pointerDistance * pointerDistance / 9000.0);

    float outside = max(d, 0.0);
    float alpha = 0.4 * exp(-outside / 11.0) * edgeFade;   // the shadow that lifts it off the screen
    float3 color = float3(0.0);

    // Light spilling out from the edge, in slow uneven tongues.
    if (d > -2.0) {
        float tongues = 0.35 + 1.35 * (fbm(float2(around * 9.0 - u.rim * 1.2, u.time * 0.5)) - 0.3);
        float reach = min(5.0 + 5.0 * u.energy + 7.0 * u.level + 3.0 * u.pulse, 13.0);
        float bloom = exp(-outside / reach) * min(0.07 + 0.55 * u.energy + 0.7 * u.level + 0.35 * u.pulse + excite * 0.7 + pointerLight * 0.5, 1.0) * tongues * edgeFade;
        color += glowColor * bloom + rippleLight * exp(-outside / 14.0) * 0.45 * edgeFade;
        alpha += bloom * 0.6 * (1.0 - alpha);
    }

    // The body: near-black glass with slow curtains of aurora, brightest along the edge and
    // dark in the middle, where the words are.
    float fill = smoothstep(0.7, -0.7, d);
    if (fill > 0.0) {
        float2 curtain = float2(p.x * 0.011 + u.time * 0.05, p.y * 0.0035 - u.time * 0.03);
        float sheet = noise(curtain + noise(curtain * 1.7 + u.time * 0.07) * 1.2);
        float depth = exp(min(d, 0.0) / 42.0);
        float3 haze = palette(sheet * 1.1 + u.rim * 0.3, u) * smoothstep(0.32, 0.95, sheet) * depth * (0.08 + 0.55 * u.energy);
        float inner = exp(min(d, 0.0) / 13.0);
        float3 body = float3(0.027, 0.027, 0.036) + haze + glowColor * inner * (0.035 + 0.22 * u.energy + 0.25 * u.level + 0.2 * u.pulse);
        body += float3(0.1, 0.1, 0.125) * pointerLight + rippleLight * 0.4;
        const float opacity = 0.95;
        color = color * (1.0 - fill * opacity) + body * opacity * fill;
        alpha = alpha * (1.0 - fill * opacity) + opacity * fill;
    }

    // The edge itself: a thin bright line, carrying everything that is happening.
    float line = smoothstep(1.35, 0.15, abs(d + 0.45));
    float lit = min(0.17 + 0.7 * u.energy + 1.0 * u.level + 0.7 * u.pulse + excite * 2.0 + pointerLight * 1.3, 1.5);
    color += mix(glowColor, float3(1.0), 0.13) * line * lit;
    alpha = max(alpha, line * min(lit, 1.0));
    // And the glass catching light along its top.
    float top = smoothstep(u.bar.w * 0.3, -u.bar.w, toBar.y);
    color += float3(1.0) * smoothstep(1.1, 0.0, abs(d + 1.2)) * top * 0.13;

    // The orb.
    float radius = u.orbRadius;
    if (radius > 0.5) {
        float angle = atan2(toOrb.y, toOrb.x);
        float wobble = (noise(float2(angle * 1.6 + u.time * 1.3, u.time * 0.9)) - 0.5) * 2.0;
        // A sphere when calm. It only loses its shape to a voice or a burst of words.
        float shaped = radius * (1.0 + wobble * (0.012 + u.energy * 0.022 + u.level * 0.34 + u.pulse * 0.09) + u.level * 0.18 + u.pulse * 0.06);
        float halo = exp(-max(orbDistance - shaped, 0.0) / (2.6 + radius * 0.34)) * edgeFade;
        color += palette(angle / tau + u.rim, u) * halo * (0.14 + 0.6 * u.energy + u.level + 0.45 * u.pulse);
        alpha = max(alpha, halo * 0.3);
        float inside = smoothstep(shaped + 0.6, shaped - 0.6, orbDistance);
        if (inside > 0.0) {
            float2 n = toOrb / shaped;
            float z = sqrt(max(0.0, 1.0 - dot(n, n)));
            float3 normal = float3(n, z);
            float2 sphere = n / (1.0 + z) * 1.15;
            float clock = u.time * 0.9 + u.rim;
            float2 warp = float2(noise(sphere * 1.6 + clock), noise(sphere * 1.6 - clock + 3.1));
            float plasma = noise(sphere * 1.7 + warp * 1.5 + clock * 0.3);
            float3 tint = palette(plasma * 1.25 + u.rim * 0.5 + n.x * 0.12, u);
            float core = smoothstep(0.15, 0.85, plasma) * (0.85 + u.energy * 0.9 + u.level * 1.3 + u.pulse * 0.8);
            // Lit from within: bright through the middle, deeper toward the limb.
            float3 ball = tint * (0.32 + core) * (0.6 + 0.55 * z);
            // Its highlight looks toward the pointer.
            float2 look = clamp((u.pointer - u.orb) / 140.0, -1.0, 1.0) * u.pointerGlow * 0.6 + float2(-0.35, -0.55);
            float3 light = normalize(float3(look, 0.8));
            ball += float3(1.0) * pow(max(dot(normal, light), 0.0), 14.0) * 0.5;
            ball += tint * pow(1.0 - z, 3.0) * (0.75 + u.energy * 0.8);
            color = mix(color, ball, inside);
            alpha = max(alpha, inside);
        }
        // The faint track the tasks run along, when there are any: tilted, like Saturn's ring,
        // and hidden where it passes behind the orb.
        if (u.satCount > 0) {
            const float c = 0.9135, s = -0.4067;
            float2 flat_ = float2(c * toOrb.x + s * toOrb.y, -s * toOrb.x + c * toOrb.y);
            float track = length(flat_ / float2(radius * 1.8, radius * 0.62));
            if (flat_.y >= 0.0 || orbDistance > shaped) {
                color += palette(angle / tau + u.rim, u) * smoothstep(0.075, 0.0, abs(track - 1.0)) * 0.28;
            }
        }
    }

    // One small light for each task running, in orbit.
    for (int i = 0; i < u.satCount; i++) {
        float distance = length(p - sats[i].xy);
        float glow = exp(-distance * distance / (2.0 * sats[i].z * sats[i].z)) * sats[i].w;
        // A white-hot core inside its colour, so it reads against an orb of the same hue.
        color += satTint[i].rgb * glow * 1.6 + float3(1.0) * glow * glow * glow * 0.9;
        alpha = max(alpha, min(glow * 1.3, 1.0));
    }

    // A little grain, so long dark gradients do not band.
    color += (hash(p + fract(u.time)) - 0.5) / 170.0 * alpha;
    color = max(color, 0.0);
    // Properly premultiplied: the glow tints a bright wallpaper rather than vanishing into it.
    alpha = clamp(max(alpha, max(color.r, max(color.g, color.b))), 0.0, 1.0);
    return float4(min(color, alpha), alpha);
}
