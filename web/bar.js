/* Mission Control: the floating bar.
   A small window that stays above the others: how the lead is, what the work under
   way is doing this moment, one presence for each running task, and a line to ask
   anything. Opened, it shows the same work cards the conversation shows and the
   last reply. In the desktop app the window is resized through pywebview's bridge;
   in a browser it is an ordinary small page. */
const S = { agents: {}, agentState: {}, agentTool: {}, link: null, user: null };
(() => {
  const $ = (sel) => document.querySelector(sel);
  const REST = 60, OPEN = 420;
  const B = { open: false, stream: "", reply: null, asked: 0, sending: false };
  const host = () => (window.pywebview && window.pywebview.api) || null;
  const lead = () => Object.values(S.agents).sort((a, b) => (a.order ?? 99) - (b.order ?? 99))[0];
  const tidy = (s) => String(s || "").replace(/\[([^\]]+)\]\(https?:[^)]+\)/g, "$1").replace(/\s+/g, " ").trim();

  function setOpen(open) {
    B.open = open;
    document.body.classList.toggle("open", open);
    $("#bar-status").setAttribute("aria-expanded", String(open));
    const h = open ? OPEN : REST;
    if (host()) host().resize(h);
    else { try { window.resizeTo(window.outerWidth, h + (window.outerHeight - window.innerHeight)); } catch {} }
    draw();
  }
  function openMain(view) {
    if (host()) host().open_main();
    else window.open(view === "inbox" ? "/#inbox" : "/", "_blank");
  }
  // The work cards say "Review in Inbox" and report errors through the main window's deck.
  window.MissionDeck = { show: openMain, notify: (text) => { B.note = text; draw(); setTimeout(() => { B.note = ""; draw(); }, 6000); } };

  function draw() {
    const l = lead();
    const live = Work.live();
    const needs = live.find((t) => t.phase === "needs_you");
    const moving = live.find((t) => ["working", "fixing", "thinking"].includes(t.phase)) || live[0];
    $("#bar-orb").innerHTML = presence(l || {}, 38, S.link === false ? "offline" : needs ? "needsyou" : l ? Work.mood(l.id) : "idle");
    const line = $("#bar-line"), sub = $("#bar-sub");
    line.className = "";
    if (B.note) { line.textContent = B.note; sub.textContent = ""; }
    else if (S.link === false) { line.textContent = "Reconnecting…"; sub.textContent = ""; }
    else if (needs) {
      line.textContent = `${needs.agent_name} needs you`; line.className = "warn";
      sub.textContent = tidy((needs.waiting_on || {}).text) || needs.title;
    } else if (B.stream) { line.textContent = tidy(B.stream).slice(-90); sub.textContent = l ? l.name : ""; }
    else if (moving) {
      line.innerHTML = `<span class="shimmer" style="--now:${presenceClock()}"></span>`;
      line.firstChild.textContent = Work.phaseLine(moving);
      sub.textContent = `${moving.agent_name} · ${moving.title}`;
    } else {
      line.textContent = l ? (Work.leadLine(l.id) === "Ready" && B.reply ? tidy(B.reply.text).slice(0, 90) : Work.leadLine(l.id)) : "Connecting…";
      sub.textContent = l ? l.name : "";
    }
    // One small presence for each running task, in that profile's colour.
    $("#bar-strip").innerHTML = live.slice(0, 5).map((t) =>
      `<span title="${t.agent_name}: ${Work.phaseLine(t).replace(/"/g, "&quot;")}">${presence(S.agents[t.agent] || { id: t.agent }, 20, t.phase === "queued" ? "waiting" : t.phase)}</span>`).join("");
    if (!B.open) return;
    const box = $("#bar-more");
    const ids = live.map((t) => t.id).join(",") + "|" + (B.reply ? B.reply.id : "") + "|" + (B.stream ? 1 : 0);
    if (box.dataset.ids !== ids) {
      box.dataset.ids = ids;
      Work.keepFocus(() => {
        box.innerHTML = live.map((t) => Work.card(t, { flush: true })).join("")
          + `<div id="bar-reply"></div>` + (!live.length && !B.reply && !B.stream ? `<div id="bar-empty">Nothing is under way. Ask for something above.</div>` : "");
      });
    } else Work.paint(box);
    const reply = $("#bar-reply");
    if (reply) {
      const text = B.stream || (B.reply ? B.reply.text : "");
      reply.innerHTML = text ? `<span class="who"></span><span class="text"></span>` : "";
      if (text) { reply.querySelector(".who").textContent = l ? l.name : ""; reply.querySelector(".text").innerHTML = Work.linked(text); }
    }
  }

  async function refresh() {
    const state = await (await fetch("/api/state")).json();
    S.user = state.user; S.agents = {};
    for (const a of state.agents) S.agents[a.id] = a;
    try { S.agentState = (await (await fetch("/api/agents-status")).json()).agents || {}; } catch {}
    await Work.load();
    draw();
  }
  function onEvent(ev) {
    const l = lead();
    if (ev.type === "agent_state") {
      S.agentState[ev.agent] = ev.state;
      if (ev.state === "idle") delete S.agentTool[ev.agent];
    } else if (ev.type === "execution") {
      const e = ev.execution;
      if (e && e.kind === "tool") {
        if (e.status === "running") S.agentTool[e.agent] = e.name; else if (S.agentTool[e.agent] === e.name) delete S.agentTool[e.agent];
        Work.step(e);
      }
    } else if (ev.type === "stream" && l && ev.agent === l.id && ev.thread === `dm:${l.id}`) {
      if (ev.reset) B.stream = ""; else if (ev.text) B.stream += ev.text;
    } else if (ev.type === "message" && l && ev.msg.thread === `dm:${l.id}` && ev.msg.sender === l.id && ev.msg.kind === "chat") {
      B.stream = ""; B.reply = ev.msg;
      // A reply to something asked from here opens the bar, so it can be read without the window.
      if (B.asked && Date.now() - B.asked < 10 * 60 * 1000 && !B.open) setOpen(true);
    } else if (ev.type === "typing" && !ev.on && l && ev.agent === l.id) { B.stream = ""; }
    else if (["session_updated", "mission_updated", "approvals_updated", "watch_updated", "cron_updated"].includes(ev.type)) Work.soon();
    else if (["agent_added", "agent_updated", "agent_removed"].includes(ev.type)) { refresh(); return; }
    draw();
  }
  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => { S.link = true; refresh().catch(() => {}); };
    ws.onmessage = (e) => onEvent(JSON.parse(e.data));
    ws.onclose = () => { S.link = false; draw(); setTimeout(connect, 1500); };
  }

  $("#bar-status").addEventListener("click", () => setOpen(!B.open));
  $("#bar-input").addEventListener("input", () => $("#bar-send").classList.toggle("idle", !$("#bar-input").value.trim()));
  $("#bar-ask").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const l = lead(), input = $("#bar-input"), text = input.value.trim();
    if (!l || !text || B.sending) return;
    B.sending = true;
    try {
      const res = await fetch("/api/send", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thread: `dm:${l.id}`, text, attachments: [] }) });
      const out = await res.json().catch(() => ({}));
      if (!res.ok || !out.ok) throw new Error(out.error || "Message was not accepted.");
      input.value = ""; $("#bar-send").classList.add("idle");
      B.asked = Date.now(); B.reply = null;
    } catch (err) { window.MissionDeck.notify(err.message); }
    B.sending = false;
    draw();
  });
  $("#bar-open").addEventListener("click", () => openMain());
  $("#bar-close").addEventListener("click", () => (host() ? host().close() : window.close()));
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // Esc backs out of what is open.
    if (B.open) setOpen(false); else $("#bar-input").blur();
  });
  window.addEventListener("mc:work", draw);
  draw();
  connect();
})();
