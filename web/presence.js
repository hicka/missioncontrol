/* Mission Control: a profile's presence.
   A body in the profile's colour that moves the way the profile is. At rest it
   breathes. Thinking, something circles it. Working, light runs round its rim.
   Fixing, its edge roughens and the light turns amber. Waiting, it dims and slows.
   When it needs you, rings go out from it. Finished, it holds a tick for a moment.
   No face: how it is shows in how it moves. */
(() => {
  const SPOKEN = {
    idle: "At rest", thinking: "Thinking", working: "Working", fixing: "Fixing something",
    waiting: "Waiting", needsyou: "Needs you", happy: "Finished", sad: "Could not finish",
    offline: "Not connected",
  };
  // Older callers still pass the agent's raw state.
  const ALIAS = { doing: "working", needs_you: "needsyou", sleepy: "idle", listening: "thinking", speaking: "working" };

  function hashOf(s) {
    let h = 0;
    for (const ch of String(s || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return h;
  }
  // Near black or near white reads as no colour at all: give it slate.
  function tint(agent) {
    const hex = /^#[0-9a-f]{6}$/i.test(agent?.color || "") ? agent.color : "";
    if (!hex) return "#8089a0";
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    if (Math.max(r, g, b) < 0.2 || Math.min(r, g, b) > 0.9) return "#808cad";
    return hex;
  }
  // The filter that roughens an edge, once per page.
  function defs() {
    if (document.getElementById("presence-defs")) return;
    const box = document.createElement("div");
    box.innerHTML = `<svg id="presence-defs" width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
      <filter id="presence-rough" x="-25%" y="-25%" width="150%" height="150%"><feTurbulence type="fractalNoise" baseFrequency=".2" numOctaves="1" seed="4" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="2.6"/></filter>
      <filter id="presence-rough-lg" x="-25%" y="-25%" width="150%" height="150%"><feTurbulence type="fractalNoise" baseFrequency=".09" numOctaves="1" seed="4" result="n"/><feDisplacementMap in="SourceGraphic" in2="n" scale="6"/></filter>
    </defs></svg>`;
    document.body.append(box.firstChild);
  }

  /* Every motion is timed from when the page loaded, not from when the element was
     drawn, so a list that is redrawn does not make its presences start over. */
  function clock() { return `-${(performance.now() / 1000).toFixed(2)}s`; }

  function presence(agent, size = 28, mood = "idle", still = false) {
    mood = ALIAS[mood] || mood;
    if (!SPOKEN[mood]) mood = "idle";
    if (mood === "fixing") defs();
    const seed = hashOf(agent?.id || agent?.name);
    const cls = ["presence", `m-${mood}`];
    if (size < 22) cls.push("tiny");
    if (size >= 40) cls.push("big");
    // One of many in a long list, where nothing is gained by all of them breathing.
    if (still) cls.push("still");
    const parts = {
      needsyou: `<i class="p-ring"></i><i class="p-ring two"></i>`,
      thinking: `<i class="p-moon"><b></b></i>`,
      working: `<i class="p-arc"></i>`,
      fixing: `<i class="p-arc"></i>`,
      happy: `<svg class="p-tick" viewBox="0 0 24 24"><path d="M7.4 12.5 10.8 15.9 17 8.3"/></svg>`,
    }[mood] || "";
    return `<span class="${cls.join(" ")}" role="img" aria-label="${SPOKEN[mood]}" ` +
      `style="--s:${size}px;--c:${tint(agent)};--now:${clock()};--seed:-${(seed % 97) * 0.41}s">` +
      `${parts}<i class="p-body"></i></span>`;
  }

  window.presence = presence;
  window.presenceTint = tint;
  window.presenceClock = clock;
  window.presenceSpoken = (mood) => SPOKEN[ALIAS[mood] || mood] || SPOKEN.idle;
})();
