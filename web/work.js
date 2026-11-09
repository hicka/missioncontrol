/* Mission Control: work as it is being done.
   The conversation shows what the agent says. This is the part in between: each
   task a conversation started, as GET /api/work describes it, kept current from
   live `execution` events, and drawn as a card: who has it, what they are on,
   each step as it lands, and a line to change its course. The same model says
   how each profile is, which is what its presence shows.
   Reads the page's `S` (agents, agentState, agentTool, link) when asked. */
const Work = (() => {
  const W = { tasks: [], ui: {}, loaded: false };
  const firstSeen = {};          // step id -> when this page first drew it
  const cheered = {}, failed = {};  // agent -> until when it shows a tick, or that it could not finish
  let timer = null, loading = false, again = false, lastJSON = "";

  const x = (s) => String(s ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
  const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;
  const clip = (s, n) => { s = String(s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; };
  const PATHS = {
    check: "m5 12.5 4.5 4.5L19 7.5", x: "M6.5 6.5l11 11M17.5 6.5l-11 11", chev: "m6 9.5 6 6 6-6",
    stop: "M7 7h10v10H7z", up: "M12 19V5.5M5.5 12 12 5.5l6.5 6.5", turn: "M5 5v6a3 3 0 0 0 3 3h11M15 10l4 4-4 4",
    alert: "M12 6v7.5M12 17v.4", clock: "M12 7.5V12l3 2M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17z",
    hand: "M8 12V6.5a1.5 1.5 0 0 1 3 0V11m0-5.5v-1a1.5 1.5 0 0 1 3 0V11m0-5a1.5 1.5 0 0 1 3 0v8.5a6 6 0 0 1-6 6h-.6a5 5 0 0 1-4-2L5 15.5a1.6 1.6 0 0 1 2.6-1.8L8 14",
    open: "M9 5h10v10M19 5 8 16M5 9v10h10", dots: "M6 12h.01M12 12h.01M18 12h.01",
  };
  const ic = (name) => `<svg class="wi" viewBox="0 0 24 24" aria-hidden="true"><path d="${PATHS[name]}"/></svg>`;
  const now = () => Date.now() / 1000;
  const state = () => (typeof S === "undefined" ? { agents: {}, agentState: {}, agentTool: {}, link: true } : S);

  /* ---------- the model ---------- */
  function shape() { return W.tasks.map((t) => `${t.id}|${t.thread}|${t.live ? 1 : 0}`).join(","); }
  function announce(before) {
    window.dispatchEvent(new CustomEvent("mc:work", { detail: { shape: before !== shape() } }));
  }
  async function load() {
    if (loading) { again = true; return; }
    loading = true;
    try {
      const res = await fetch("/api/work?limit=30");
      if (!res.ok) return;
      const text = await res.text();
      W.loaded = true;
      if (text === lastJSON) return;
      lastJSON = text;
      const before = shape();
      const was = Object.fromEntries(W.tasks.map((t) => [t.id, t]));
      W.tasks = JSON.parse(text).tasks || [];
      for (const t of W.tasks) {
        // The moment a task ends, whoever had it shows it: a tick, or that it could not.
        if (was[t.id]?.live && !t.live) {
          const book = t.outcome === "failed" ? failed : t.outcome === "finished" ? cheered : null;
          if (book) { book[t.agent] = now() + 8; setTimeout(() => announce(shape()), 8200); }
        }
      }
      announce(before);
    } catch { /* the next event asks again */ } finally {
      loading = false;
      if (again) { again = false; soon(); }
    }
  }
  function soon(delay = 200) { clearTimeout(timer); timer = setTimeout(load, delay); }

  /* A tool call started or ended. The card it belongs to takes it at once; the
     server's account of the task follows a moment later and settles anything this guessed. */
  function step(record) {
    if (!record || record.kind !== "tool" || !record.mission) return;
    const task = W.tasks.find((t) => t.id === record.mission);
    if (!task) { soon(50); return; }
    if (!record.quiet && record.label) {
      const st = { id: record.id, ts: record.ts, agent: record.agent, tool: record.name, label: record.label,
                   status: record.status, seconds: Math.max(0, (record.updated || record.ts) - record.ts) };
      const i = task.steps.findIndex((s) => s.id === st.id);
      if (i >= 0) task.steps[i] = st; else task.steps.push(st);
      if (st.status === "running") {
        const before = task.steps.slice(0, -1).filter((s) => s.tool !== "report").pop();
        task.phase = before && before.status === "failed" ? "fixing" : "working";
      } else if (task.phase === "working" || task.phase === "fixing") {
        task.phase = "thinking";
      }
      task.updated = now();
      lastJSON = "";
      announce(shape());
    }
    soon(600);
  }

  /* What it is doing this moment, as a person would say it. */
  function phaseLine(t) {
    const last = t.steps[t.steps.length - 1];
    switch (t.phase) {
      case "thinking": return "Thinking";
      case "working": return last && last.status === "running" ? last.label : "Working";
      case "fixing": return last ? `Fixing · ${last.label}` : "Fixing";
      case "queued": return "Waiting for a free place";
      case "needs_you": return "Needs you";
      case "waiting": {
        const w = t.waiting_on || {};
        return { job: `Running · ${w.text}`, watch: `Watching · ${w.text}`, thread: `Waiting on ${w.text}`, provider: w.text }[w.kind] || "Waiting";
      }
      default: return { failed: "Could not finish", stopped: "Stopped" }[t.outcome] || "Finished";
    }
  }
  const live = () => W.tasks.filter((t) => t.live);
  const liveOf = (aid) => live().filter((t) => t.agent === aid);
  const inThread = (thread) => W.tasks.filter((t) => t.thread === thread);
  const get = (id) => W.tasks.find((t) => t.id === id);

  /* How a profile is: what its task is doing, then what it is doing itself. */
  function mood(aid) {
    const s = state();
    if (s.link === false) return "offline";
    const tasks = liveOf(aid);
    const has = (...phases) => tasks.some((t) => phases.includes(t.phase));
    if (has("needs_you")) return "needsyou";
    if (has("fixing")) return "fixing";
    if (has("working")) return "working";
    if (has("thinking")) return "thinking";
    const st = s.agentState[aid] || "idle";
    if (st === "thinking") return "thinking";
    if (st === "doing") return "working";
    if (has("waiting", "queued")) return "waiting";
    if ((cheered[aid] || 0) > now() || s.flash?.[aid]) return "happy";
    if ((failed[aid] || 0) > now()) return "sad";
    return "idle";
  }
  function busy(aid) {
    const st = state().agentState[aid] || "idle";
    return st !== "idle" || liveOf(aid).some((t) => ["thinking", "working", "fixing"].includes(t.phase));
  }
  /* What a profile is on right now, in words. */
  function doing(aid) {
    const s = state();
    const task = liveOf(aid)[0];
    if (task) return phaseLine(task);
    const st = s.agentState[aid] || "idle";
    if (st === "thinking") return "Thinking";
    if (st === "doing") {
      const tool = s.agentTool[aid];
      return tool ? tool.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()) : "Working";
    }
    return "";
  }
  /* The lead's own line: thinking, or who it is waiting on. */
  function leadLine(leadId) {
    const s = state();
    if (s.link === false) return "Reconnecting…";
    const st = s.agentState[leadId] || "idle";
    if (st === "thinking") return "Thinking…";
    if (st === "doing") return "Working…";
    const all = live();
    const needs = all.find((t) => t.phase === "needs_you");
    if (needs) return `${needs.agent_name} needs you`;
    const names = [...new Set(all.map((t) => t.agent_name))].sort();
    if (names.length) return `Waiting on ${names.slice(0, 2).join(" and ")}${names.length > 2 ? " and others" : ""}`;
    return "Ready";
  }

  /* ---------- the card ---------- */
  const SHOWN = 4;
  const took = (seconds) => (seconds < 90 ? `${Math.round(seconds)}s` : `${Math.round(seconds / 60)}m`);
  const elapsed = (since) => {
    const s = Math.max(0, Math.floor(now() - since));
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  function tone(t) {
    if (t.phase === "needs_you" || t.phase === "fixing") return "warn";
    if (t.phase === "done") return { finished: "good", failed: "danger" }[t.outcome] || "muted";
    return "accent";
  }
  function mark(t) {
    if (t.phase === "done") return ic({ finished: "check", failed: "alert" }[t.outcome] || "stop");
    if (t.phase === "needs_you") return ic("hand");
    if (t.phase === "waiting" || t.phase === "queued") return `<i class="work-breath"></i>`;
    return `<i class="work-spin big"></i>`;
  }
  // A note reads as words. A bare command reads as a command.
  const looksLikeCommand = (s) => s.tool === "terminal" && /\/| -|=/.test(s.label);
  function stepRow(s) {
    const fresh = (firstSeen[s.id] ??= performance.now()) > performance.now() - 450;
    const m = s.status === "running" ? `<i class="work-spin"></i>`
      : s.status === "completed" ? ic("check") : s.status === "failed" ? ic("x") : `<i class="work-dot"></i>`;
    return `<div class="work-step ${x(s.status)}${fresh ? " new" : ""}" title="${x(s.label)}">
      <span class="work-step-mark">${m}</span><span class="work-step-label${looksLikeCommand(s) ? " mono" : ""}">${x(s.label)}</span>
      ${s.status !== "running" && s.seconds >= 1 ? `<span class="work-step-time">${took(s.seconds)}</span>` : ""}</div>`;
  }
  // Words with their links kept: [text](url) and a bare address both open in the browser.
  function linked(text) {
    const rx = /\[([^\]\n]{1,120})\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"]+)/g;
    let out = "", last = 0;
    for (const m of String(text || "").matchAll(rx)) {
      let url = m[2] || m[3], label = m[1] || m[3], end = m.index + m[0].length;
      if (!m[2]) {
        const trail = url.match(/[.,;:!?)\]}'"]+$/);
        if (trail) { url = url.slice(0, -trail[0].length); label = url; end -= trail[0].length; }
      }
      out += x(text.slice(last, m.index)) + `<a href="${x(url)}" target="_blank" rel="noopener noreferrer">${x(label)}</a>`;
      last = end;
    }
    return out + x(String(text || "").slice(last));
  }
  function card(t, opts = {}) {
    const ui = W.ui[t.id] || (W.ui[t.id] = {});
    const open = ui.open ?? t.live;
    const tn = tone(t);
    const line = phaseLine(t);
    const moving = t.live && t.phase !== "needs_you";
    const head = `<div class="work-head" data-work-act="toggle" role="button" tabindex="0" aria-expanded="${open}"
        aria-label="${x(`${t.agent_name}, ${t.title}, ${line}`)}">
      <span class="work-mark">${mark(t)}</span>
      <div class="work-id"><div class="work-title">${x(t.title)}</div>
        <div class="work-sub"><span>${x(t.agent_name)}</span><span class="sep">·</span>${t.live
          ? `<span class="work-phase${moving ? " shimmer" : ""}">${x(line)}</span>`
          : `<span>${x(`${line} · ${plural(t.steps.length, "step")} · ${took(Math.max(0, t.updated - t.started))}`)}</span>`}</div></div>
      ${t.live ? `<button class="work-btn work-stop" data-work-act="stop" title="Stop this task" aria-label="Stop this task">${ic("stop")}</button>
        <span class="work-elapsed" data-since="${t.started}">${elapsed(t.started)}</span>` : ""}
      ${opts.detail ? `<button class="work-btn" data-act="mission" data-id="${x(t.id)}" title="Open the task" aria-label="Open the task">${ic("open")}</button>` : ""}
      <span class="work-chev">${ic("chev")}</span></div>`;
    let body = "";
    if (open) {
      const hidden = ui.all ? 0 : Math.max(0, t.steps.length - SHOWN);
      const visible = t.steps.slice(hidden);
      const steps = (hidden ? `<button class="work-earlier" data-work-act="all">${ic("dots")}<span>${plural(hidden, "earlier step")}</span></button>` : "")
        + visible.map(stepRow).join("")
        + (t.phase === "thinking" ? `<div class="work-step thinking"><span class="work-step-mark"><i class="work-dots"><b></b><b></b><b></b></i></span><span class="work-step-label">Thinking</span></div>` : "");
      const needs = t.phase === "needs_you" ? `<div class="work-needs"><div>${linked((t.waiting_on || {}).text || "")}</div>
        ${(t.waiting_on || {}).approval ? `<button class="btn small" data-work-act="inbox">Review in Inbox</button>` : ""}</div>` : "";
      const checks = t.checks || [];
      const done = checks.filter((c) => c.done && c.how).length;
      const finish = checks.length ? `<div class="work-checks" title="${x(checks.map((c) => `${c.done && c.how ? "✓" : "·"} ${c.check}`).join("\n"))}">
        ${checks.map((_, i) => `<i class="${i < done ? "on" : ""}"></i>`).join("")}<span>${done} of ${checks.length} confirmed</span></div>` : "";
      const summary = !t.live && t.summary ? `<div class="work-summary">${linked(t.summary)}</div>` : "";
      const steer = t.live ? `<form class="work-steer" data-work-steer="${x(t.id)}" autocomplete="off">
          ${ic("turn")}<input type="text" value="${x(ui.steer || "")}" ${ui.sending ? "disabled" : ""}
            placeholder="${x(t.phase === "needs_you" ? `Answer ${t.agent_name}` : `Tell ${t.agent_name} to change course`)}"
            aria-label="${x(`Message for ${t.agent_name} about this task`)}">
          <button type="submit" class="work-send${(ui.steer || "").trim() ? "" : " hidden"}" aria-label="Send">${ic("up")}</button></form>
        ${ui.steered ? `<div class="work-steered">Sent. ${x(t.agent_name)} reads it at its next step: “${x(clip(ui.steered, 70))}”</div>` : ""}` : "";
      body = `<div class="work-body">${steps ? `<div class="work-steps">${steps}</div>` : ""}${needs}${finish}${summary}${steer}</div>`;
    }
    return `<div class="work-card tone-${tn}${t.live ? " live" : ""}${moving ? " moving" : ""}${opts.flush ? " flush" : ""}"
      data-work="${x(t.id)}" data-work-opts="${x(JSON.stringify(opts))}" style="--now:${presenceClock()}">${head}${body}</div>`;
  }

  /* Redrawing a card must not take the words out from under someone typing in it. */
  function keepFocus(fn) {
    const el = document.activeElement;
    const form = el && el.closest ? el.closest(".work-steer") : null;
    const held = form ? { id: form.dataset.workSteer, a: el.selectionStart, b: el.selectionEnd,
                          zone: el.closest("[data-work-zone]")?.dataset.workZone } : null;
    fn();
    if (!held) return;
    const sel = `.work-steer[data-work-steer="${CSS.escape(held.id)}"] input`;
    const zone = held.zone ? document.querySelector(`[data-work-zone="${CSS.escape(held.zone)}"]`) : null;
    const input = (zone || document).querySelector(sel);
    if (input && !input.disabled) { input.focus(); try { input.setSelectionRange(held.a, held.b); } catch {} }
  }
  /* Every card already on the page, brought up to date where it stands. */
  function paint(root = document) {
    keepFocus(() => {
      root.querySelectorAll(".work-card[data-work]").forEach((el) => {
        const t = get(el.dataset.work);
        if (!t) { el.remove(); return; }
        let opts = {};
        try { opts = JSON.parse(el.dataset.workOpts || "{}"); } catch {}
        el.outerHTML = card(t, opts);
      });
    });
  }
  function repaintOne(id) {
    keepFocus(() => document.querySelectorAll(`.work-card[data-work="${CSS.escape(id)}"]`).forEach((el) => {
      const t = get(id);
      let opts = {};
      try { opts = JSON.parse(el.dataset.workOpts || "{}"); } catch {}
      if (t) el.outerHTML = card(t, opts);
    }));
    window.dispatchEvent(new CustomEvent("mc:work-layout"));
  }
  const say = (text, error) => (window.MissionDeck?.notify ? window.MissionDeck.notify(text, error) : console.warn(text));
  async function post(path, body) {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok || data.ok === false) throw new Error(data.error || (typeof data.detail === "string" ? data.detail : "That did not work. Please try again."));
    return data;
  }
  /* The owner's word to one task while it runs, past the lead. */
  async function steer(id) {
    const t = get(id), ui = W.ui[id] || (W.ui[id] = {});
    const text = (ui.steer || "").trim();
    if (!t || !text || ui.sending) return;
    ui.sending = true; repaintOne(id);
    try {
      await post(`/api/kernel/sessions/${encodeURIComponent(t.session)}/message`, { text });
      ui.steered = text; ui.steer = "";
      soon(200);
    } catch (err) { say(err.message, true); }
    ui.sending = false; repaintOne(id);
    document.querySelector(`.work-steer[data-work-steer="${CSS.escape(id)}"] input`)?.focus();
  }
  async function stop(id) {
    const t = get(id);
    if (!t) return;
    try { await post(`/api/kernel/sessions/${encodeURIComponent(t.session)}/stop`); soon(100); }
    catch (err) { say(err.message, true); }
  }

  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-work-act]");
    if (!b) return;
    const cardEl = b.closest(".work-card");
    const id = cardEl?.dataset.work;
    const t = id && get(id);
    if (!t) return;
    const ui = W.ui[id] || (W.ui[id] = {});
    const act = b.dataset.workAct;
    if (act === "toggle") {
      if (ev.target.closest("button, a, input")) return;
      ui.open = !(ui.open ?? t.live); repaintOne(id);
      if (ui.open) document.querySelectorAll(`.work-card[data-work="${CSS.escape(id)}"]`).forEach((el) => el.classList.add("opening"));
    } else if (act === "all") { ui.all = true; repaintOne(id); }
    else if (act === "stop") { ev.stopPropagation(); b.disabled = true; stop(id); }
    else if (act === "inbox") { window.MissionDeck?.show("inbox"); }
  });
  document.addEventListener("keydown", (ev) => {
    const head = ev.target.closest?.(".work-head");
    if (head && ev.target === head && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); head.click(); }
  });
  document.addEventListener("input", (ev) => {
    const form = ev.target.closest?.(".work-steer");
    if (!form) return;
    const ui = W.ui[form.dataset.workSteer] || (W.ui[form.dataset.workSteer] = {});
    ui.steer = ev.target.value;
    form.querySelector(".work-send")?.classList.toggle("hidden", !ui.steer.trim());
  });
  document.addEventListener("submit", (ev) => {
    const form = ev.target.closest?.(".work-steer");
    if (!form) return;
    ev.preventDefault();
    steer(form.dataset.workSteer);
  });
  // How long each has been at it, counted by the second.
  setInterval(() => {
    if (document.hidden) return;
    document.querySelectorAll(".work-elapsed[data-since]").forEach((el) => { el.textContent = elapsed(Number(el.dataset.since)); });
  }, 1000);

  return Object.assign(W, { load, soon, step, phaseLine, live, liveOf, inThread, get, mood, busy, doing, leadLine, card, paint, keepFocus, linked });
})();
