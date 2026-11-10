/* The workspace around the chat: home, inbox, tasks, schedule, knowledge, the
   team getting better, and each agent's profile. Everything here is a
   projection of server state; live events only decide when to look again. */
(() => {
  const h = (v) => esc(String(v ?? ""));
  const icon = (name, cls = "") => `<svg class="i ${cls}"><use href="#i-${name}"/></svg>`;
  const VIEWS = {
    home: "Home", inbox: "Inbox", missions: "Tasks", activity: "Activity", schedule: "Schedule",
    knowledge: "Knowledge", evolution: "Evolution", modules: "Modules",
  };
  const VIEW_ICONS = { home: "home", inbox: "inbox", missions: "tasks", activity: "bolt", schedule: "clock", knowledge: "book",
                       evolution: "growth", modules: "puzzle" };
  const kept = (key, fallback) => { try { return localStorage.getItem(key) || fallback; } catch { return fallback; } };
  const keep = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
  const D = {
    view: "home", loaded: false, sending: false, refreshing: false, again: false,
    data: { agents: {}, missions: [], executions: [], watches: [], skills: [], activity: [],
            counts: {}, inbox: [], evolution: {}, jobs: [] },
    commands: [], evo: null, evoFilter: "live", taskFilter: kept("mc-task-filter", "active"), knowTab: "memory",
    memQuery: "", memSeq: 0, showTable: false, panelAgent: null, paletteIndex: 0,
    kernel: null, eventFilter: kept("mc-event-filter", "all"), kernelTimer: null, answers: {}, paletteHits: null, paletteSeq: 0,
  };
  let refreshTimer, panelTimer, restoreFocus;
  const now = () => Date.now() / 1000;
  const agentName = (id) => S.agents[id]?.name || ({ "*": "Everywhere", selfimprove: "Self-repair" }[id] || id) || "Someone";
  const firstName = () => ((S.user && S.user.name) || "").split(" ")[0];
  const lead = () => Object.values(S.agents).sort((a, b) => (a.order ?? 99) - (b.order ?? 99))[0];
  const openMissions = () => D.data.missions.filter((m) => ["running", "queued", "waiting"].includes(m.status));
  const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
  function ago(ts) {
    if (!ts) return "";
    const s = Math.max(0, now() - ts);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }
  const pct = (x) => (x === null || x === undefined) ? "—" : `${Math.round(x * 100)}%`;
  const pill = (status, label) => `<span class="pill ${h(status)}">${h(label || String(status).replaceAll("_", " "))}</span>`;

  async function api(path, body, method) {
    const opts = body === undefined && !method ? {} : {
      method: method || "POST", headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) };
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || (typeof data.detail === "string" ? data.detail : "That did not work. Please try again."));
    }
    return data;
  }

  /* ---------- toasts ---------- */
  function notify(message, error = false, action = null) {
    let wrap = document.querySelector(".toast-wrap");
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "toast-wrap"; document.body.append(wrap); }
    const el = document.createElement("div");
    el.className = `toast ${error ? "error" : ""}`;
    el.setAttribute("role", error ? "alert" : "status");
    const text = document.createElement("span");
    text.textContent = message;
    el.append(text);
    if (action) {
      const b = document.createElement("button");
      b.textContent = action.label;
      b.addEventListener("click", () => { el.remove(); action.run(); });
      el.append(b);
    }
    wrap.append(el);
    while (wrap.children.length > 3) wrap.firstChild.remove();
    setTimeout(() => el.remove(), error ? 9000 : 5500);
  }

  /* ---------- routing ---------- */
  const MORE = ["activity", "schedule", "knowledge", "evolution", "modules"];
  function setNav(view) {
    document.querySelectorAll("#sidebar button[data-deck]").forEach((b) => {
      const on = b.dataset.deck === view;
      b.classList.toggle("active", on);
      b.setAttribute("aria-current", on ? "page" : "false");
    });
    applyMore();
  }
  // More stays folded until asked for, and opens by itself for the view it holds.
  const moreWanted = () => { try { return localStorage.getItem("mc-more") === "open"; } catch { return false; } };
  function applyMore() {
    const open = moreWanted() || MORE.includes(D.view) || !!D.moreNeeds;
    $("#more-nav").classList.toggle("hidden", !open);
    $("#more-toggle").setAttribute("aria-expanded", String(open));
  }
  function show(view) {
    if (view === "overview" || view === "network") view = "home";
    if (view === "skills" || view === "memory") { D.knowTab = view; view = "knowledge"; }
    if (view === "watches") view = "schedule";
    if (!VIEWS[view]) view = "home";
    D.view = view;
    document.body.classList.add("deck-mode");
    document.body.classList.remove("nav-open");
    $("#control").classList.remove("hidden");
    $("#chat").classList.add("hidden");
    setNav(view);
    $("#deck-title").textContent = VIEWS[view];
    $("#deck-scroll").scrollTop = 0;
    mount();
    if (typeof renderSidebar === "function") renderSidebar();
  }
  function showChat() {
    D.view = "chat";
    document.body.classList.remove("deck-mode");
    document.body.classList.remove("nav-open");
    $("#control").classList.add("hidden");
    $("#chat").classList.remove("hidden");
    D.panelForced = false;
    setNav(null);
  }

  /* ---------- data ---------- */
  async function refresh() {
    if (D.refreshing) { D.again = true; return; }
    D.refreshing = true;
    try {
      D.data = { ...D.data, ...(await api("/api/control")) };
      D.loaded = true;
      for (const [aid, v] of Object.entries(D.data.agents || {})) {
        S.agentState[aid] = v.state;
        if (v.tool) S.agentTool[aid] = v.tool; else delete S.agentTool[aid];
      }
      badges();
      if (D.view !== "chat") update();
    } catch (err) {
      if (!D.loaded && D.view !== "chat") {
        $("#deck-content").innerHTML = `<div class="empty"><strong>Waiting for the runtime</strong><span>${h(err.message)}</span>
          <button class="btn" data-act="retry">Try again</button></div>`;
      }
    } finally {
      D.refreshing = false;
      if (D.again) { D.again = false; scheduleRefresh(); }
    }
  }
  function scheduleRefresh(delay = 450) { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, delay); }
  function badges() {
    const n = (D.data.inbox || []).length;
    const inbox = $("#nav-inbox-count");
    inbox.textContent = n; inbox.classList.toggle("hidden", !n);
    const open = openMissions().length;
    const tasks = $("#nav-mission-count");
    tasks.textContent = open; tasks.classList.toggle("hidden", !open);
    const working = (D.data.workers || []).filter((w) => w.status === "working").length;
    const busy = $("#nav-activity-count");
    busy.textContent = working; busy.classList.toggle("hidden", !working);
    const watching = (D.data.watches || []).filter((w) => w.status === "active").length;
    const sched = $("#nav-watch-count");
    sched.textContent = watching; sched.classList.toggle("hidden", !watching);
    const evo = D.data.evolution || {};
    const mark = $("#nav-evolution-mark");
    mark.textContent = evo.trial ? `${evo.trial}` : "";
    mark.title = evo.trial ? `${evo.trial} lessons on trial` : "";
    mark.classList.toggle("hidden", !evo.trial);
    document.title = n ? `(${n}) Mission Control` : "Mission Control";
  }

  /* ---------- view lifecycle: mount draws the frame once, update fills it ---------- */
  function mount() {
    const c = $("#deck-content");
    const frames = { home: homeFrame, inbox: inboxFrame, missions: tasksFrame, activity: activityFrame, schedule: scheduleFrame,
                     knowledge: knowledgeFrame, evolution: evolutionFrame, modules: MissionModules.frame };
    c.innerHTML = frames[D.view]();
    if (D.view === "home") wireComposer();
    if (D.view === "evolution") loadEvolution();
    if (D.view === "knowledge") loadKnowledge();
    if (D.view === "activity") loadKernel();
    if (D.view === "modules") { MissionModules.update(); MissionModules.load(); }
    update();
  }
  function update() {
    if (!D.loaded) return;
    ({ home: updateHome, inbox: updateInbox, missions: updateTasks, schedule: updateSchedule,
       knowledge: updateKnowledge, evolution: () => {}, activity: updateActivity })[D.view]?.();
  }

  /* ---------- home ---------- */
  function greeting() {
    const hr = new Date().getHours();
    const part = hr < 5 ? "Working late" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
    return firstName() && firstName() !== "You" ? `${part}, ${firstName()}` : part;
  }
  // The first screen: somewhere to hand over a task, what needs the owner, the work
  // under way as it happens, and who is on the team. Counts and charts live on
  // their own screens.
  function homeFrame() {
    return `<section class="home-hero">
        <div class="home-glow" id="home-glow"></div>
        <h2 class="reveal">${h(greeting())}</h2>
        <div class="home-sub reveal" id="home-sub"></div>
        <div class="composer-lg reveal">
          <div id="home-options" class="composer-options hidden" role="listbox" aria-label="Suggestions"></div>
          <textarea id="home-input" rows="2" placeholder="Hand over a task, or ask anything…" aria-label="New task"></textarea>
          <div class="composer-row">
            <span class="route-chip" id="home-route"></span>
            <span class="spacer"></span>
            <button class="round-send" id="home-send" aria-label="Send" title="Send" disabled>${icon("up")}</button>
          </div>
        </div>
        <div class="suggestions reveal">
          <button data-act="run-command" data-command="/status">${icon("users")}What is running?</button>
          <button data-act="prefill" data-text="Summarize what got done today, and what is still open.">${icon("tasks")}Today's summary</button>
          <button data-act="show" data-view="knowledge" data-tab="skills">${icon("lightbulb")}Run a skill</button>
        </div>
        <div id="home-feedback"></div>
      </section>
      <section class="section" id="home-needs"></section>
      <section class="section hidden" id="home-work-section">
        <div class="section-head"><h3>Under way</h3><span class="spacer"></span><button class="link" data-act="show" data-view="missions">All tasks ${icon("chev")}</button></div>
        <div id="home-work" data-work-zone="home"></div>
      </section>
      <section class="section">
        <div class="section-head"><h3>Team</h3><span class="count" id="home-team-count"></span></div>
        <div class="team-strip" id="home-team"></div>
      </section>`;
  }
  function busyAgents() {
    return Object.entries(D.data.agents || {}).filter(([, v]) => v.state && v.state !== "idle").map(([id]) => id);
  }
  function updateHome() {
    const busy = busyAgents();
    const inbox = D.data.inbox || [];
    const evo = D.data.evolution || {};
    const bits = [];
    // what the kernel says is open, so this can never read "all quiet" over a waiting task
    const workers = D.data.workers || [];
    const working = workers.filter((w) => w.status === "working").length;
    const waiting = workers.filter((w) => WORKER_OPEN.includes(w.status)).length - working;
    if (working) bits.push(`<b>${plural(working, "task")}</b> working`);
    if (waiting) bits.push(working ? `<b>${waiting}</b> more open` : `<b>${plural(waiting, "task")}</b> open`);
    if (!working && !waiting) bits.push("All quiet");
    if (inbox.length) bits.push(`<b>${inbox.length}</b> ${inbox.length === 1 ? "thing needs" : "things need"} you`);
    if (evo.week && evo.week.clean_rate !== null && evo.week.clean_rate !== undefined) bits.push(`<b>${pct(evo.week.clean_rate)}</b> clean runs this week`);
    $("#home-sub").innerHTML = bits.map((b) => `<span>${b}</span>`).join('<span class="sep">·</span>');
    const glow = $("#home-glow");
    const colors = busy.map((id) => colorOf(S.agents[id]));
    glow.style.setProperty("--g1", colors[0] || "transparent");
    glow.style.setProperty("--g2", colors[1] || colors[0] || "transparent");
    glow.classList.toggle("on", busy.length > 0);
    route();
    // needs you
    // A summary only: deciding happens in the inbox, where a held command is
    // shown in full before anyone can run it.
    $("#home-needs").innerHTML = inbox.length ? `
      <div class="section-head"><h3>Needs you</h3><span class="count">${inbox.length}</span><span class="spacer"></span>
        <button class="link" data-act="show" data-view="inbox">Open inbox ${icon("chev")}</button></div>
      <div class="list card">${inbox.slice(0, 4).map((i) => {
        const k = KIND[i.kind] || { icon: "inbox", tone: "" };
        return `<button class="item" data-act="show" data-view="inbox"><span class="item-icon ${k.tone}">${icon(k.icon)}</span>
          <div class="item-main"><div class="item-title ${i.kind === "command" ? "mono" : ""}" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${h(i.title.split("\n")[0])}</div>
          <div class="item-sub">${h(k.label)}${i.agent && i.agent !== "*" ? ` · ${h(agentName(i.agent))}` : ""}</div></div>
          <div class="item-meta">${h(ago(i.ts))}${icon("chev")}</div></button>`;
      }).join("")}${inbox.length > 4 ? `<button class="item" data-act="show" data-view="inbox"><div class="item-main"><div class="item-sub">and ${inbox.length - 4} more</div></div></button>` : ""}</div>` : "";
    updateHomeWork(true);
    updateTeam();
  }
  // Work in flight, drawn the way the conversation draws it. Nothing here when all is quiet.
  function updateHomeWork(layout) {
    const box = $("#home-work");
    if (!box) return;
    const live = Work.live();
    const ids = live.map((t) => t.id).join(",");
    $("#home-work-section").classList.toggle("hidden", !live.length);
    if (layout && box.dataset.ids !== ids) {
      box.dataset.ids = ids;
      Work.keepFocus(() => { box.innerHTML = live.map((t) => Work.card(t, { flush: true })).join(""); });
    } else Work.paint(box);
  }
  // The team in a line: who they are and whether they are on something. A profile's
  // chat is a click away, and its numbers are in its own panel.
  function updateTeam() {
    const box = $("#home-team");
    if (!box) return;
    const agents = Object.values(S.agents).sort((a, b) => (a.order ?? 99) - (b.order ?? 99));
    $("#home-team-count").textContent = agents.length;
    box.innerHTML = agents.map(teamChip).join("") +
      `<button class="team-chip add" data-act="new-agent">${icon("plus")}<span>Add a profile</span></button>`;
  }
  function teamChip(a) {
    const busy = Work.busy(a.id);
    const doing = busy ? Work.doing(a.id) : "";
    return `<button class="team-chip${busy ? " busy" : ""}" data-act="chat" data-agent="${h(a.id)}" title="Opens the chat with ${h(a.name)}">
      ${presence(a, 26, moodOf(a.id))}
      <span class="tc-text"><span class="name">${h(a.name)}</span>${doing ? `<span class="doing shimmer" style="--now:${presenceClock()}">${h(doing)}</span>` : ""}</span>
      ${busy ? `<i class="work-spin" style="--now:${presenceClock()}"></i>` : ""}</button>`;
  }
  function toolDetail(e) {
    const args = (e.detail || {}).arguments;
    if (!args) return "";
    try {
      const a = typeof args === "string" ? JSON.parse(args) : args;
      return String(a.command || a.url || a.query || a.action || a.text || a.name || "").slice(0, 120);
    } catch { return String(args).slice(0, 120); }
  }
  function sparkline(values, w = 120, hgt = 30) {
    const pts = values.map((v, i) => [i, v]).filter(([, v]) => v !== null && v !== undefined);
    if (pts.length < 2) return `<svg class="spark" viewBox="0 0 ${w} ${hgt}" width="${w}" height="${hgt}" aria-hidden="true"></svg>`;
    const n = values.length - 1 || 1;
    const lo = Math.min(...pts.map((p) => p[1])), hi = Math.max(...pts.map((p) => p[1]));
    const span = hi - lo || 1;
    const xy = pts.map(([i, v]) => [2 + (i / n) * (w - 6), hgt - 4 - ((v - lo) / span) * (hgt - 8)]);
    const path = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
    const [lx, ly] = xy[xy.length - 1];
    return `<svg class="spark" viewBox="0 0 ${w} ${hgt}" width="${w}" height="${hgt}" role="img" aria-label="trend over ${values.length} days">
      <path d="${path}" fill="none" stroke="var(--text-4)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="4" fill="var(--text)" stroke="var(--surface)" stroke-width="2"/></svg>`;
  }

  // the home composer: a task for the lead, or for whoever is @mentioned
  function route() {
    const input = $("#home-input"), chip = $("#home-route");
    if (!input || !chip) return;
    const v = input.value.trim();
    const m = v.match(/^@([\w-]+)/);
    const a = m && S.agents[m[1]] ? S.agents[m[1]] : lead();
    if (v.startsWith("/")) {
      chip.innerHTML = `${icon("term")}<span>Command · runs locally</span>`;
    } else if (a) {
      chip.innerHTML = `${presence(a, 18, moodOf(a.id))}<span>${m && S.agents[m[1]] ? "" : "Lead · "}${h(a.name)}</span>`;
    }
    $("#home-send").disabled = !v || D.sending;
  }
  function wireComposer() {
    const input = $("#home-input");
    attachSuggest(input, $("#home-options"));
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = `${Math.min(220, input.scrollHeight)}px`;
      route();
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendTask(); }
    });
    $("#home-send").addEventListener("click", sendTask);
    route();
  }
  function feedback(text, error = false, thread = null) {
    const box = $("#home-feedback");
    if (!box) { notify(text, error); return; }
    box.innerHTML = `<div class="feedback ${error ? "error" : ""}"><span class="grow"></span>${thread ? `<button class="btn small" data-act="thread" data-thread="${h(thread)}">Open</button>` : ""}<button class="icon-btn" data-act="dismiss" aria-label="Dismiss">${icon("x")}</button></div>`;
    box.querySelector(".grow").textContent = text;
  }
  async function sendTask(explicit) {
    const input = $("#home-input");
    const text = (explicit ?? input?.value ?? "").trim();
    if (!text || D.sending) return;
    D.sending = true; route();
    try {
      const result = await api("/api/command", { text });
      if (input && explicit === undefined && input.value.trim() === text) { input.value = ""; input.style.height = "auto"; }
      if (result.mission) {
        feedback(`Task started with ${agentName(result.mission.agent)}. Follow it in your chat or under Tasks.`, false, result.mission.thread || "group:team");
      } else if (result.reply) {
        feedback(result.reply);
      }
      await refresh();
    } catch (err) {
      feedback(err.message, true);
    } finally {
      D.sending = false; route();
    }
  }
  function prefill(text) {
    if (D.view !== "home") show("home");
    const input = $("#home-input");
    input.value = text; input.focus();
    input.dispatchEvent(new Event("input"));
  }

  /* ---------- inbox ---------- */
  const KIND = {
    command: { icon: "shield", tone: "warn", label: "Command held for you" },
    tool: { icon: "shield", tone: "warn", label: "Tool call held for you" },
    job: { icon: "clock", tone: "info", label: "Recurring job wants to start" },
    proposal: { icon: "branch", tone: "info", label: "Proposed change to Mission Control" },
    signin: { icon: "globe", tone: "warn", label: "Website sign-in" },
    lesson: { icon: "flask", tone: "warn", label: "Lesson needs a decision" },
    question: { icon: "inbox", tone: "warn", label: "A worker needs your answer" },
    module: { icon: "puzzle", tone: "info", label: "A module wants to run" },
  };
  const reasonHtml = (i) => (i.reason ? `<div class="held-reason">${icon("shield")}<span>Held because ${h(i.reason)}</span></div>` : "");
  // Three near-identical held commands are impossible to tell apart, so say
  // which one was already answered and what moved since.
  function priorHtml(prior) {
    if (!prior) return "";
    const when = new Date(prior.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const verb = prior.status === "denied" ? "declined" : "approved";
    if (prior.identical) return `<div style="margin-top:6px;color:var(--warn)">Identical to the command you ${verb} at ${h(when)}. Running it runs it again.</div>`;
    const lines = (prior.diff || []).map((l) => `<div class="approval-diff-line ${l[0] === "+" ? "add" : "del"}">${h(l)}</div>`).join("");
    return `<details style="margin-top:6px"><summary>Differs from the command you ${verb} at ${h(when)}</summary>${lines || "<div>only whitespace changed</div>"}</details>`;
  }
  function inboxItem(i) {
    const k = KIND[i.kind] || { icon: "inbox", tone: "", label: i.kind };
    const who = i.agent && i.agent !== "*" ? agentName(i.agent) : (i.kind === "lesson" ? "Everywhere" : "");
    let sub = "", actions = "";
    if (i.kind === "command") {
      sub = `<span class="who">${h(who)}</span> wants to run this in ${h(i.detail)}. Running it runs exactly what is shown.${reasonHtml(i)}${priorHtml(i.prior)}`;
      actions = `<button class="btn small primary" data-act="approval" data-id="${h(i.id)}" data-do="approve">Run it</button>
        <button class="btn small" data-act="approval" data-id="${h(i.id)}" data-do="deny">Decline</button>`;
    } else if (i.kind === "tool") {
      sub = `<span class="who">${h(who)}</span> wants to make this change through the ${h(i.detail)} MCP server, which marks it as one that cannot be taken back. Approving sends exactly what is shown.${reasonHtml(i)}`;
      actions = `<button class="btn small primary" data-act="approval" data-id="${h(i.id)}" data-do="approve">Approve</button>
        <button class="btn small" data-act="approval" data-id="${h(i.id)}" data-do="deny">Decline</button>`;
    } else if (i.kind === "job") {
      sub = `<span class="who">${h(who)}</span> · ${h(i.schedule)}${i.max_runs ? ` · stops after ${i.max_runs} runs` : " · runs until you stop it"}
        <div style="margin-top:6px;color:var(--text-2);white-space:pre-wrap">${h(i.detail)}</div>
        ${i.monitor_cmd ? `<div style="margin-top:8px">Before every run it executes this check in your shell:</div><pre class="out cmd">${h(i.monitor_cmd)}</pre>` : ""}`;
      actions = `<button class="btn small primary" data-act="job" data-id="${h(i.id)}" data-do="approve">Approve</button>
        <button class="btn small" data-act="job" data-id="${h(i.id)}" data-do="delete">Reject</button>`;
    } else if (i.kind === "proposal") {
      const st = i.stat || {};
      sub = `<span class="who">${h(who)}</span> · ${(st.files || []).length} files · +${st.added || 0}/-${st.removed || 0} · tests green<br>${h(i.detail)}`;
      actions = `<button class="btn small" data-act="diff" data-id="${h(i.id)}">View diff</button>
        <button class="btn small primary" data-act="proposal" data-id="${h(i.id)}" data-do="approve">Approve and restart</button>
        <button class="btn small" data-act="proposal" data-id="${h(i.id)}" data-do="reject">Reject</button>`;
    } else if (i.kind === "question") {
      sub = `<span class="who">${h(who)}</span> stopped on this while working on: ${h(i.detail)}
        <textarea class="answer-box" id="answer-${h(i.id)}" rows="2" style="margin-top:8px" data-answer="${h(i.id)}" placeholder="Your answer goes straight to this worker.">${h(D.answers[i.id] || "")}</textarea>`;
      actions = `<button class="btn small primary" data-act="answer" data-id="${h(i.id)}">Send</button>`;
    } else if (i.kind === "module") {
      ({ sub, actions } = MissionModules.inboxItem(i, who));
    } else if (i.kind === "signin") {
      sub = `<span class="who">${h(who)}</span> is waiting. ${h(i.detail)}`;
      actions = `<button class="btn small primary" data-act="settings" data-tab="browser">Open sign-in</button>`;
    } else if (i.kind === "lesson") {
      sub = `${who ? `<span class="who">${h(who)}</span> · ` : ""}${h(i.detail)}`;
      const ownerRule = ["owner", "owner-rule"].includes(i.source);
      const act = (to, label, primary = false) =>
        `<button class="btn small ${primary ? "primary" : ""}" data-act="lesson" data-id="${h(i.id)}" data-do="${to}">${label}</button>`;
      // What can be done depends on why it is here: a lesson waiting for an
      // OK, one the regression guard held back, or one the review flagged.
      actions = i.reason === "proposed" ? act("active", "Use it", true) + act("retired", "Discard")
        : i.reason === "suspended" ? act(ownerRule ? "active" : "trial", "Restore") + act("retired", "Retire")
        : i.status === "active" ? act("active", "Keep it", true) + act("retired", "Retire")
        : act("active", "Keep it as a rule", true) + act("retired", "Dismiss");
      actions += `<button class="btn small ghost" data-act="show" data-view="evolution">Review</button>`;
    }
    return `<div class="item"><span class="item-icon ${k.tone}">${icon(k.icon)}</span>
      <div class="item-main"><div class="muted" style="font-size:12px;margin-bottom:2px">${h(k.label)}</div>
        ${i.kind === "command" || i.kind === "tool" ? `<pre class="out cmd">${h(i.title)}</pre>` : `<div class="item-title">${h(i.title)}</div>`}
        <div class="item-sub">${sub}</div><div class="item-actions">${actions}</div></div>
      <div class="item-meta">${h(ago(i.ts))}</div></div>`;
  }
  function inboxFrame() {
    return `<div class="view-head"><div><h2>Inbox</h2><p>Everything waiting on you, in one place: commands the guard held,
      jobs asking to start, proposed changes, sign-ins, questions from workers, and lessons that need a decision.</p></div></div><div id="inbox-list"></div>`;
  }
  function updateInbox() {
    const items = D.data.inbox || [];
    // A refresh must not take a half-written answer away: the words are kept, and the
    // list is left alone while someone is typing in it and nothing new has arrived.
    const ids = items.map((i) => `${i.kind}:${i.id}`).join(",");
    const box = $("#inbox-list");
    if (box.dataset.ids === ids && box.contains(document.activeElement) && document.activeElement.matches("textarea")) return;
    box.dataset.ids = ids;
    $("#inbox-list").innerHTML = items.length ? `<div class="list card">${items.map(inboxItem).join("")}</div>`
      : `<div class="card"><div class="empty">${teamFaces()}<strong>Nothing needs you</strong>
          <span>When the agent needs your say-so, it lands here and on your phone.</span></div></div>`;
  }
  function teamFaces() {
    const agents = Object.values(S.agents).sort((a, b) => (a.order ?? 99) - (b.order ?? 99)).slice(0, 4);
    return `<span class="presence-stack">${agents.map((a) => presence(a, 36, moodOf(a.id))).join("")}</span>`;
  }

  /* ---------- tasks ---------- */
  function taskRow(m) {
    const resumable = ["waiting", "failed", "cancelled"].includes(m.status);
    return `<div class="item clickable" data-act="mission" data-id="${h(m.id)}" role="button" tabindex="0">${presence(S.agents[m.agent], 26, ["running", "queued"].includes(m.status) ? moodOf(m.agent) : "idle", true)}
      <div class="item-main"><div class="item-title clamp">${h(m.objective)}</div>
        <div class="item-sub">${h(agentName(m.agent))} · updated ${h(ago(m.updated))}</div></div>
      <div class="item-meta">${pill(m.status)}${resumable ? `<button class="btn small" data-act="resume" data-id="${h(m.id)}">Resume</button>` : ""}</div></div>`;
  }
  function tasksFrame() {
    return `<div class="view-head"><div><h2>Tasks</h2><p>Every objective you have handed over, what it runs as, and where it stands.
      Tasks survive restarts and wait for you to resume them.</p></div>
      <div class="view-actions"><button class="btn primary" data-act="new-task">${icon("plus")}New task</button></div></div>
      <div class="toolbar"><div class="tabs" id="task-tabs"></div></div><div id="task-list" data-work-zone="tasks"></div>`;
  }
  function updateTasks() {
    const all = D.data.missions || [];
    const groups = {
      active: all.filter((m) => ["running", "queued"].includes(m.status)),
      waiting: all.filter((m) => m.status === "waiting"),
      done: all.filter((m) => ["completed", "failed", "cancelled"].includes(m.status)),
      all,
    };
    const names = { active: "In progress", waiting: "Waiting", done: "Finished", all: "All" };
    $("#task-tabs").innerHTML = Object.keys(groups).map((k) =>
      `<button class="${D.taskFilter === k ? "on" : ""}" data-act="task-filter" data-filter="${k}">${names[k]}<b>${groups[k].length}</b></button>`).join("");
    const list = groups[D.taskFilter] || all;
    // A task under way is drawn as its card, the same one the conversation shows.
    const cards = list.filter((m) => Work.get(m.id)?.live);
    const rows = list.filter((m) => !cards.includes(m));
    Work.keepFocus(() => {
    $("#task-list").innerHTML = list.length ? cards.map((m) => Work.card(Work.get(m.id), { flush: true, detail: true })).join("")
        + (rows.length ? `<div class="list card${cards.length ? " after-work" : ""}">${rows.map(taskRow).join("")}</div>` : "")
      : `<div class="card"><div class="empty">${teamFaces()}<strong>${D.taskFilter === "done" ? "Nothing finished yet" : "No tasks here"}</strong>
          <span>Describe an outcome on Home and a worker takes it on.</span></div></div>`;
    });
  }

  /* ---------- activity: what reached the agent, and what became of it ---------- */
  const EVENT_ICONS = { github: "branch", hook: "bolt", cron: "clock", heartbeat: "clock", brief: "clock",
                        watch: "clock", imessage: "users", telegram: "users", whatsapp: "users", email: "users",
                        gui: "users", worker: "users", fleet: "globe", approval: "shield", whatsapp_link: "users" };
  const EVENT_GROUPS = {
    all: () => true,
    acted: (e) => ["routed", "passed", "answered"].includes(e.disposition),
    quiet: (e) => ["ignored", "read", "logged"].includes(e.disposition),
    waiting: (e) => ["new", "queued"].includes(e.disposition),
  };
  const WORKER_OPEN = ["queued", "working", "waiting", "blocked", "failed", "stalled"];
  const sourceOf = (s) => String(s || "").split(":")[0];
  const oneLine = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const clip = (s, n) => { const t = oneLine(s); return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t; };
  function activityFrame() {
    return `<div class="view-head"><div><h2>Activity</h2><p>Everything that reached the agent, what became of it, and the rules it decides by.</p></div>
      <button class="btn small" id="act-pause" data-act="pause-toggle">Pause</button></div>
      <div class="kstats" id="act-stats"></div>
      <div class="section-head"><h3>Working now</h3></div><div id="act-workers"></div>
      <div class="section-head"><h3>Events</h3><span class="spacer"></span><div class="tabs" id="act-event-tabs"></div></div><div id="act-events"></div>
      <div class="section-head"><h3>What it acts on unprompted</h3></div>
      <p class="kernel-note">The agent answers you always. Everything else it reads against these rules, and otherwise stays quiet. Text from outside can never change them.</p>
      <div id="act-policies"></div>
      <div class="policy-add"><textarea id="policy-text" rows="2" placeholder="Add a rule in plain words, like: when someone reports a bug in a repo, try to reproduce it quietly and only tell me if you find a simple fix."></textarea>
        <button class="btn small" data-act="policy-add">Add rule</button></div>
      <div class="section-head"><h3>Where events come from</h3></div><div id="act-sources"></div>
      <div class="section-head"><h3>Watchdog</h3></div><div id="act-findings"></div>`;
  }
  async function loadKernel() {
    try {
      D.kernel = await api("/api/kernel");
      if (D.view === "activity") updateActivity();
      const pause = $("#act-pause");
      if (pause) pause.textContent = D.kernel.paused ? "Paused: carry on" : "Pause";
    } catch (err) {
      if (D.view === "activity") $("#act-stats").innerHTML = `<div class="empty"><span>${h(err.message)}</span></div>`;
    }
  }
  function scheduleKernel() {
    if (D.view !== "activity") return;
    clearTimeout(D.kernelTimer);
    D.kernelTimer = setTimeout(loadKernel, 350);
  }
  function workerRow(w) {
    return `<button class="item clickable" data-act="worker" data-id="${h(w.id)}">${presence(S.agents[w.profile] || {}, 28, w.status === "working" ? moodOf(w.profile) : "idle", true)}
      <div class="item-main"><div class="item-title">${h(w.title || "Task")}</div>
      <div class="item-sub">${h(agentName(w.profile))}${w.direct ? " · direct line" : ""}${w.strength === "inferred" ? " · not asked for directly" : ""}${w.speculative ? " · acting ahead" : ""} · ${h(ago(w.updated))}${w.summary ? ` · ${h(clip(w.summary, 110))}` : ""}</div></div>${pill(w.status)}</button>`;
  }
  function updateActivity() {
    const k = D.kernel;
    if (!k || !$("#act-stats")) return;
    const counts = k.events_24h || {};
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const quiet = (counts.ignored || 0) + (counts.read || 0) + (counts.logged || 0);
    const stat = (value, label, note = "") => `<div class="kstat"><strong>${h(value)}</strong><span>${h(label)}</span>${note ? `<em>${h(note)}</em>` : ""}</div>`;
    $("#act-stats").innerHTML = [
      stat(k.router.alive ? (k.router.state === "idle" ? "Listening" : "Deciding") : "Stopped", k.router.name,
           k.router.pending ? `${k.router.pending} waiting` : "nothing waiting"),
      stat(`${k.slots.used} of ${k.slots.limit}`, "workers running"),
      stat(total, "events today", total ? `${quiet} needed nothing` : ""),
      stat(counts.passed || 0, "went straight to a worker", "no decision spent"),
    ].join("");
    const open = (k.sessions || []).filter((w) => WORKER_OPEN.includes(w.status));
    $("#act-workers").innerHTML = open.length ? `<div class="list card">${open.map(workerRow).join("")}</div>`
      : `<div class="card"><div class="empty"><span>Nothing running. Tasks you hand over show up here while they are worked on.</span></div></div>`;
    const events = k.events || [];
    const names = { all: "All", acted: "Acted on", quiet: "Left alone", waiting: "Waiting" };
    $("#act-event-tabs").innerHTML = Object.keys(EVENT_GROUPS).map((g) =>
      `<button class="${D.eventFilter === g ? "on" : ""}" data-act="event-filter" data-filter="${g}">${names[g]}<b>${events.filter(EVENT_GROUPS[g]).length}</b></button>`).join("");
    const shown = events.filter(EVENT_GROUPS[D.eventFilter] || EVENT_GROUPS.all);
    $("#act-events").innerHTML = shown.length ? `<div class="list card">${shown.slice(0, 40).map((e) => `<button class="item clickable" data-act="recall" data-id="${h(e.id)}">
        <span class="item-icon">${icon(EVENT_ICONS[sourceOf(e.source)] || "bolt")}</span>
        <div class="item-main"><div class="item-title">${h(clip(e.text, 130))}</div>
        <div class="item-sub">${e.from_owner ? "You" : h(e.actor || sourceOf(e.source))} · ${h(e.thread)} · ${h(ago(e.ts))}${e.detail ? ` · ${h(clip(e.detail, 70))}` : ""}</div></div>${pill(e.disposition)}</button>`).join("")}</div>`
      : `<div class="card"><div class="empty"><span>Nothing here yet.</span></div></div>`;
    const policies = k.policies || [];
    $("#act-policies").innerHTML = policies.length ? `<div class="list card">${policies.map((p) => `<div class="item${p.enabled ? "" : " off"}">
        <div class="item-main"><div class="item-title wrap">${h(p.text)}</div><div class="item-sub">${p.author === "system" ? "Starting rule" : "Your rule"}</div></div>
        <button class="btn small" data-act="policy" data-action="${p.enabled ? "disable" : "enable"}" data-id="${h(p.id)}" aria-pressed="${p.enabled ? "true" : "false"}">${p.enabled ? "On" : "Off"}</button>
        <button class="btn small ghost" data-act="policy" data-action="remove" data-id="${h(p.id)}" aria-label="Remove rule">${icon("x")}</button></div>`).join("")}</div>`
      : `<div class="card"><div class="empty"><span>No rules, so it only acts on what you ask.</span></div></div>`;
    const labels = { explicit: "Only when asked", candidate: "Read and judge", ignore: "Ignore" };
    const locked = new Set(["gui", "imessage", "telegram", "whatsapp", "email"]);
    const sources = Object.entries(k.sources || {}).filter(([s]) => !locked.has(s)).sort();
    $("#act-sources").innerHTML = `<div class="list card">${sources.map(([s, v]) => `<div class="item"><span class="item-icon">${icon(EVENT_ICONS[sourceOf(s)] || "bolt")}</span>
        <div class="item-main"><div class="item-title">${h({ hook: "Webhooks", fleet: "Your other machines", github: "GitHub", whatsapp_link: "Your WhatsApp (read-only)" }[s] || s)}</div></div>
        <select class="source-select" data-source="${h(s)}" aria-label="How to treat ${h(s)}">${Object.entries(labels).map(([value, label]) => `<option value="${value}"${value === v ? " selected" : ""}>${label}</option>`).join("")}</select></div>`).join("")}</div>`;
    $("#act-sources").querySelectorAll("select").forEach((sel) => sel.addEventListener("change", async () => {
      await policyCall({ action: "source", source: sel.dataset.source, admission: sel.value });
    }));
    const findings = k.findings || [];
    const stuck = (k.outbox || []);
    $("#act-findings").innerHTML = (findings.length || stuck.length) ? `<div class="list card">
        ${stuck.map((o) => `<div class="item"><span class="item-icon warn">${icon("bolt")}</span><div class="item-main"><div class="item-title">A message to ${h(o.channel)} has not gone out</div><div class="item-sub">${o.attempts} tries · ${h(o.error || "")}</div></div></div>`).join("")}
        ${findings.slice(0, 12).map((f) => `<button class="item clickable" data-act="${f.session.startsWith("w_") ? "worker" : "none"}" data-id="${h(f.session)}"><span class="item-icon">${icon("shield")}</span>
          <div class="item-main"><div class="item-title">${h(f.verdict.replaceAll("_", " "))}: ${h(f.action || "noted")}</div><div class="item-sub">${h(f.session)} · ${h(ago(f.ts))}${f.reason ? ` · ${h(clip(f.reason, 110))}` : ""}</div></div></button>`).join("")}</div>`
      : `<div class="card"><div class="empty"><span>Nothing has needed stepping in. Ask it anything with /watchdog in any chat.</span></div></div>`;
  }
  async function policyCall(body) {
    try {
      const out = await api("/api/kernel/policies", body);
      if (!out.ok) notify(out.result, true);
    } catch (err) { notify(err.message, true); }
    loadKernel();
  }
  function transcriptSteps(rows) {
    const out = [];
    for (const row of rows) {
      const blocks = Array.isArray(row.content) ? row.content : [{ type: "text", text: row.content }];
      for (const b of blocks) {
        if (b.type === "tool_use") out.push(`<div class="step call"><b>${h(b.name.replaceAll("_", " "))}</b><code>${h(oneLine(JSON.stringify(b.input)).slice(0, 180))}</code></div>`);
        else if (b.type === "tool_result") out.push(`<div class="step result${b.is_error ? " bad" : ""}">${h(String(typeof b.content === "string" ? b.content : JSON.stringify(b.content)).slice(0, 400))}</div>`);
        else if (b.type === "text" && row.role === "assistant") out.push(`<div class="step said">${h(b.text.slice(0, 600))}</div>`);
      }
    }
    return out.slice(-24).join("");
  }
  async function inspectWorker(id) {
    let d;
    try { d = await api(`/api/kernel/sessions/${encodeURIComponent(id)}`); } catch (err) { notify(err.message, true); return; }
    const s = d.session;
    const running = !["ended"].includes(s.status);
    inspect(`${agentName(s.profile)}: ${s.title || "Task"}`, `${pill(s.status)}
      ${s.brief ? `<h3>Router's note</h3><pre>${h(s.brief)}</pre>` : ""}
      ${s.summary ? `<h3>Where it stands</h3><pre>${h(s.summary)}</pre>` : ""}
      ${d.reports.length ? `<h3>Reports</h3><div class="list card">${d.reports.slice(0, 6).map((r) => `<div class="item"><div class="item-main"><div class="item-title wrap">${h(clip(r.summary, 220))}</div>${r.question ? `<div class="item-sub">Asks: ${h(r.question)}</div>` : ""}<div class="item-sub">${h(ago(r.ts))}</div></div>${pill(r.status)}</div>`).join("")}</div>` : ""}
      ${d.jobs.length ? `<h3>Background jobs</h3><div class="list card">${d.jobs.slice(0, 6).map((j) => `<div class="item"><div class="item-main"><div class="item-title">${h(j.tool)}</div><div class="item-sub">${h(j.label)}</div></div>${pill(j.status)}</div>`).join("")}</div>` : ""}
      ${d.subscriptions.length ? `<h3>Following</h3><div class="detail-meta">${d.subscriptions.map((t) => h(t.thread)).join(" · ")}</div>` : ""}
      <h3>Latest steps</h3><div class="steps">${transcriptSteps(d.transcript) || "<span class='muted'>Nothing yet.</span>"}</div>
      <div class="detail-meta">${h(s.id)}${s.mission ? ` · task ${h(s.mission)}` : ""} · ${s.strength === "inferred" ? "started from an event, so outward commands wait for you" : "started by you"}</div>
      ${running ? `<h3>Step in</h3><textarea id="step-in-text" rows="2" placeholder="Tell this worker something directly. It reads it at its next step."></textarea>
      <div class="actions"><button class="btn primary" data-act="step-in" data-id="${h(s.id)}">Send</button>
        <button class="btn" data-act="worker-action" data-action="restart" data-id="${h(s.id)}">Restart</button>
        <button class="btn danger" data-act="worker-action" data-action="stop" data-id="${h(s.id)}">Stop</button></div>` : ""}`);
  }

  /* ---------- schedule ---------- */
  function scheduleFrame() {
    return `<div class="view-head"><div><h2>Schedule</h2><p>Recurring jobs run only after you approve them. Watches follow something
      already in flight and stop themselves when it resolves.</p></div>
      <div class="view-actions"><button class="btn" data-act="settings" data-tab="jobs">${icon("settings")}Heartbeat and daily brief</button></div></div>
      <section><div class="section-head"><h3>Scheduled jobs</h3><span class="count" id="job-count"></span></div><div id="job-list"></div></section>
      <section class="section"><div class="section-head"><h3>Watches</h3><span class="count" id="watch-count"></span></div><div id="watch-list"></div></section>`;
  }
  function nextRun(ts) {
    if (!ts) return "not scheduled";
    return new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  }
  function updateSchedule() {
    const jobs = D.data.jobs || [];
    $("#job-count").textContent = jobs.length || "";
    $("#job-list").innerHTML = jobs.length ? `<div class="list card">${jobs.map((j) => {
      const acts = [
        j.status === "pending" ? `<button class="btn small primary" data-act="job" data-id="${j.id}" data-do="approve">Approve</button>` : "",
        j.status === "active" ? `<button class="btn small" data-act="job" data-id="${j.id}" data-do="pause">Pause</button>` : "",
        j.status === "paused" ? `<button class="btn small" data-act="job" data-id="${j.id}" data-do="resume">Resume</button>` : "",
        `<button class="btn small ghost danger" data-act="job" data-id="${j.id}" data-do="delete">Delete</button>`].join("");
      return `<div class="item">${presence(S.agents[j.agent], 26, "idle", true)}<div class="item-main">
        <div class="item-title">${h(j.name)}</div>
        <div class="item-sub">${h(agentName(j.agent))} · ${h(j.schedule)} · next ${h(nextRun(j.next_run))}${j.max_runs ? ` · ${j.runs || 0}/${j.max_runs} runs` : ""}</div>
        <div class="item-sub ${j.status === "pending" ? "" : "clamp"}" style="white-space:pre-wrap">${h(j.task)}</div>
        ${j.monitor_cmd ? `<div class="item-sub" style="margin-top:6px">Check before each run:</div><pre class="out cmd">${h(j.monitor_cmd)}</pre>` : ""}
        ${j.note ? `<div class="item-sub">Notes: ${h(j.note)}</div>` : ""}
        <div class="item-actions">${acts}</div></div>
        <div class="item-meta">${pill(j.status)}</div></div>`;
    }).join("")}</div>` : `<div class="card"><div class="empty compact"><span>No scheduled jobs. Ask it to check something every morning, and approve it here.</span></div></div>`;
    const watches = [...(D.data.watches || [])].sort((a, b) => (a.status === "active" ? 0 : 1) - (b.status === "active" ? 0 : 1) || b.ts - a.ts).slice(0, 24);
    $("#watch-count").textContent = watches.filter((w) => w.status === "active").length || "";
    $("#watch-list").innerHTML = watches.length ? `<div class="list card">${watches.map((w) => `<div class="item">${presence(S.agents[w.agent], 26, "idle", true)}
      <div class="item-main"><div class="item-title">${h(w.name)}</div>
        <div class="item-sub">${h(agentName(w.agent))} · ${w.checks}/${w.max_checks} checks · every ${w.interval}s${w.goal ? ` · waiting for: ${h(w.goal)}` : ""}</div>
        <div class="progress"><span style="width:${Math.min(100, 100 * w.checks / Math.max(1, w.max_checks))}%"></span></div>
        ${w.last_out ? `<pre class="out" style="margin-top:8px;max-height:120px">${h(String(w.last_out).slice(0, 1200))}</pre>` : ""}
        ${w.status === "active" ? `<div class="item-actions"><button class="btn small ghost danger" data-act="cancel-watch" data-id="${w.id}">Stop watching</button></div>` : ""}</div>
      <div class="item-meta">${pill(w.status)}</div></div>`).join("")}</div>`
      : `<div class="card"><div class="empty compact"><span>No watches. A task arms one to follow a CI run, a deploy or a status page.</span></div></div>`;
  }

  /* ---------- knowledge ---------- */
  function knowledgeFrame() {
    return `<div class="view-head"><div><h2>Knowledge</h2><p>What it knows and how it works: shared memory every task reads,
      procedures it saved as skills, and step-by-step playbooks for websites.</p></div></div>
      <div class="toolbar"><div class="tabs" id="know-tabs"></div></div><div id="know-body"></div>`;
  }
  function knowledgeTabs() {
    const tabs = { memory: "Memory", skills: "Skills", playbooks: "Playbooks" };
    $("#know-tabs").innerHTML = Object.entries(tabs).map(([k, v]) =>
      `<button class="${D.knowTab === k ? "on" : ""}" data-act="know-tab" data-tab="${k}">${v}</button>`).join("");
  }
  function loadKnowledge() {
    knowledgeTabs();
    const body = $("#know-body");
    if (D.knowTab === "memory") {
      body.innerHTML = `<div class="toolbar"><input class="deck-input" id="mem-search" type="search" placeholder="Search facts, decisions, names" value="${h(D.memQuery)}" aria-label="Search memory">
        <button class="btn" data-act="remember">${icon("plus")}Remember something</button></div><div id="mem-results"><div class="muted">Loading…</div></div>`;
      $("#mem-search").addEventListener("input", (e) => { D.memQuery = e.target.value; clearTimeout(D.memTimer); D.memTimer = setTimeout(loadMemory, 180); });
      loadMemory();
    } else if (D.knowTab === "playbooks") {
      body.innerHTML = `<div id="pb-results"><div class="muted">Loading…</div></div>`;
      loadPlaybooks();
    } else {
      body.innerHTML = `<div id="skill-results"></div>`;
      updateKnowledge();
    }
  }
  function updateKnowledge() {
    if (D.knowTab !== "skills" || !$("#skill-results")) return;
    const skills = D.data.skills || [];
    $("#skill-results").innerHTML = skills.length ? `<div class="skill-grid">${skills.map((s) => `<article class="card skill-card">
        <h4>${h(s.name)}</h4><p>${h(s.description || "A saved procedure.")}</p>
        ${s.avoid ? `<div class="avoid">Not for: ${h(s.avoid)}</div>` : ""}
        <div class="foot">${s.valid ? pill("completed", "Valid") : pill("failed", "Needs attention")}<span>${s.uses ? `used ${s.uses}×` : "not used yet"}</span><span class="spacer"></span>
          <button class="btn small ghost" data-act="skill" data-slug="${h(s.slug)}">Open</button>
          <button class="btn small" data-act="run-skill" data-slug="${h(s.slug)}" ${s.valid ? "" : "disabled"}>Run</button></div></article>`).join("")}</div>`
      : `<div class="card"><div class="empty"><strong>No skills yet</strong><span>When a task works out a procedure worth keeping, it is saved here.</span></div></div>`;
  }
  async function loadMemory() {
    const seq = ++D.memSeq;
    try {
      const data = await api(`/api/memory?q=${encodeURIComponent(D.memQuery)}`);
      if (seq !== D.memSeq || !$("#mem-results")) return;
      const rows = D.memQuery ? data.memories : [...data.memories].reverse();
      $("#mem-results").innerHTML = rows.length ? `<div class="list card">${rows.slice(0, 200).map((m) => `<div class="item" data-mem="${m.id}">
          <div class="item-main"><div class="item-sub" style="margin:0 0 4px"><span class="mem-chip">${h(m.kind || "fact")}</span> · ${h(agentName(m.author) || m.author)} · ${h(ago(m.ts))}</div>
          <div class="item-title" style="font-weight:400" data-mem-text>${h(m.text)}</div></div>
          <div class="item-meta"><button class="icon-btn" data-act="mem-edit" data-id="${m.id}" aria-label="Edit">${icon("edit")}</button>
          <button class="icon-btn danger" data-act="mem-del" data-id="${m.id}" aria-label="Delete">${icon("x")}</button></div></div>`).join("")}</div>`
        : `<div class="card"><div class="empty compact"><span>${D.memQuery ? "Nothing matches." : "Nothing saved yet."}</span></div></div>`;
    } catch (err) { if ($("#mem-results")) $("#mem-results").textContent = err.message; }
  }
  async function loadPlaybooks() {
    try {
      const data = await api("/api/playbooks");
      if (!$("#pb-results")) return;
      $("#pb-results").innerHTML = data.playbooks.length ? `<div class="list card">${data.playbooks.map((p) => `<div class="item">
          <span class="item-icon">${icon("globe")}</span><div class="item-main"><div class="item-title">${h(p.title)}</div>
          <div class="item-sub">${h(p.site)} · ${h(agentName(p.author))} · ${h(ago(p.ts))}</div></div>
          <div class="item-meta"><button class="btn small ghost danger" data-act="pb-del" data-site="${h(p.site)}" data-title="${h(p.title)}">Delete</button></div></div>`).join("")}</div>`
        : `<div class="card"><div class="empty compact"><span>No playbooks yet. A task saves one after it works out how to do something on a website.</span></div></div>`;
    } catch (err) { if ($("#pb-results")) $("#pb-results").textContent = err.message; }
  }

  /* ---------- evolution ---------- */
  function evolutionFrame() {
    return `<div class="view-head"><div><h2>Getting better</h2><p>Your corrections and its repeated mistakes become lessons.
      Each lesson is tried first and kept only if the mistake stops happening; one that does not help is retired on its own.
      Lessons can make an agent more careful, never give it more permission.</p></div>
      <div class="view-actions" id="evo-actions"></div></div><div id="evo-body"><div class="muted">Loading…</div></div>`;
  }
  async function loadEvolution() {
    try {
      D.evo = await api("/api/evolution");
      if (D.view === "evolution") renderEvolution();
    } catch (err) {
      if ($("#evo-body")) $("#evo-body").innerHTML = `<div class="empty"><strong>Could not load</strong><span>${h(err.message)}</span></div>`;
    }
  }
  function kpi(label, value, unit, delta, goodWhenUp, spark) {
    let d = delta === false ? "" : `<div class="delta">vs previous 7 days: no data yet</div>`;
    if (typeof delta === "number" && !Number.isNaN(delta)) {
      const good = delta === 0 ? null : (delta > 0) === goodWhenUp;
      const sign = delta > 0 ? "+" : delta < 0 ? "−" : "±";
      d = `<div class="delta ${good === null ? "" : good ? "good" : "bad"}">${good === null ? "" : icon(good ? "check" : "alert")}${sign}${Math.abs(delta)}${unit} vs previous 7 days</div>`;
    }
    return `<div class="card kpi"><div class="label">${h(label)}</div><div class="value">${value}</div>${d}${spark || ""}</div>`;
  }
  function renderEvolution() {
    const e = D.evo;
    if (!e) return;
    $("#evo-actions").innerHTML = `<label class="switch-row">Learning <span class="switch"><input type="checkbox" id="evo-enabled" ${e.enabled ? "checked" : ""}><span></span></span></label>
      <button class="btn" data-act="evo-review">${icon("refresh")}Run review now</button>`;
    $("#evo-enabled").addEventListener("change", async (ev) => {
      try { await api("/api/evolution", { enabled: ev.target.checked }, "PUT"); notify(ev.target.checked ? "Learning is on." : "Learning is paused. Lessons already learned stay in use."); }
      catch (err) { notify(err.message, true); ev.target.checked = !ev.target.checked; }
    });
    const card = e.scorecard || {}, w = card.week || {}, p = card.previous_week || {};
    const days = card.days || [];
    const last14 = days.slice(-14);
    const diffPts = (a, b) => (a === null || a === undefined || b === null || b === undefined) ? null : Math.round((a - b) * 100);
    const diffN = (a, b) => (b === null || b === undefined || (p.runs === 0 && p.tool_calls === 0)) ? null : a - b;
    const counts = e.counts || {};
    const kpis = `<div class="kpis">
      ${kpi("Clean runs", w.clean_rate === null || w.clean_rate === undefined ? `<span class="muted" style="font-size:16px;font-weight:500">Measuring…</span>` : `${Math.round(w.clean_rate * 100)}<small>%</small>`, " pts", w.runs ? diffPts(w.clean_rate, p.clean_rate) : false, true,
          sparkline(last14.map((d) => d.clean_rate), 200, 30))}
      ${kpi("Your corrections", `${w.corrections ?? 0}`, "", diffN(w.corrections ?? 0, p.corrections), false,
          sparkline(last14.map((d) => d.corrections), 200, 30))}
      ${kpi("Tool error rate", w.error_rate === null || w.error_rate === undefined ? "—" : `${(w.error_rate * 100).toFixed(1)}<small>%</small>`, " pts", diffPts(w.error_rate, p.error_rate), false,
          sparkline(last14.map((d) => d.error_rate), 200, 30))}
      ${kpi("Lessons in use", `${counts.active || 0}`, "", false, true, `<div class="delta">${plural(counts.trial || 0, "lesson")} on trial · ${counts.retired || 0} retired</div>`)}
    </div>`;
    const charts = `<div class="card chart-card section">
      <div class="chart-title"><h4>Daily, last 30 days</h4><span>Clean runs are runs that finished, were not corrected and did not flail.</span>
        <span style="margin-left:auto"></span><button class="table-toggle" data-act="evo-table">${D.showTable ? "Show charts" : "View as table"}</button></div>
      <div id="evo-charts"></div></div>`;
    const lessons = e.lessons || [];
    const filters = { live: ["trial", "active"], trial: ["trial"], active: ["active"], held: ["proposed", "suspended"], retired: ["retired"] };
    const names = { live: "In use", trial: "On trial", active: "Adopted", held: "Waiting on you", retired: "Retired" };
    const shown = lessons.filter((l) => filters[D.evoFilter].includes(l.status));
    const agentOpts = Object.values(S.agents).sort((a, b) => (a.order ?? 99) - (b.order ?? 99))
      .map((a) => `<option value="${h(a.id)}">${h(a.name)}</option>`).join("");
    const lessonCard = `<section class="section"><div class="section-head"><h3>Lessons</h3><span class="spacer"></span>
        <div class="tabs">${Object.keys(filters).map((k) => `<button class="${D.evoFilter === k ? "on" : ""}" data-act="evo-filter" data-filter="${k}">${names[k]}<b>${lessons.filter((l) => filters[k].includes(l.status)).length}</b></button>`).join("")}</div></div>
      <div class="card">${shown.length ? shown.map(lessonRow).join("") : `<div class="empty compact"><span>${D.evoFilter === "live" ? "No lessons yet. They come from your corrections and from failures that keep repeating." : "Nothing here."}</span></div>`}
        <form class="add-lesson" id="lesson-form"><input class="deck-input" id="lesson-text" maxlength="280" placeholder="Teach it something yourself, e.g. 'acmeshop lives in ~/Sites/acmeshop'">
          <select class="deck-input" id="lesson-scope"><option value="*">Everywhere</option>${agentOpts}</select>
          <button class="btn primary" type="submit">Add lesson</button></form></div></section>`;
    const clusters = e.clusters || [];
    const clusterCard = `<section class="section"><div class="section-head"><h3>Mistakes that keep repeating</h3><span class="count">last 14 days</span></div>
      <div class="list card">${clusters.length ? clusters.map((c) => {
        const st = CLUSTER[c.state] || CLUSTER.open;
        return `<div class="item"><span class="item-icon ${st[0]}">${icon(st[1])}</span>
        <div class="item-main"><div class="cluster-sig">${h(c.signature)}</div>
        <div class="item-sub">${c.count} times · ${h(Object.keys(c.agents || {}).map(agentName).join(", "))} · last ${h(ago(c.last))}</div></div>
        <div class="item-meta">${pill(st[2], st[3])}</div></div>`; }).join("")
        : `<div class="empty compact"><span>No failure has repeated three times in two weeks.</span></div>`}</div></section>`;
    const r = e.last_review;
    const rep = r?.report || {};
    const review = `<section class="section"><div class="section-head"><h3>Last review</h3><span class="count">${r ? h(new Date(r.ts * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })) : "not run yet"}</span>
        <span class="spacer"></span><span class="count">${e.calls_today}/${e.daily_calls} learning calls today · next review after ${h((e.settings || {}).review_at || "03:30")}</span></div>
      <div class="card pad" style="font-size:13px;color:var(--text-2);display:flex;gap:22px;flex-wrap:wrap">
        ${r ? `<span><b style="color:var(--text)">${(rep.adopted || []).length}</b> adopted</span><span><b style="color:var(--text)">${(rep.retired || []).length}</b> retired</span>
        <span><b style="color:var(--text)">${(rep.learned || []).length}</b> learned from failures</span>
        <span>${rep.regression ? `${icon("alert")} Regression guard fired: ${pct(rep.regression.baseline)} → ${pct(rep.regression.rate)}` : "Regression guard held"}</span>`
        : "The first review runs a few minutes after the app starts, then every night."}</div>
      ${changelog(rep)}</section>`;
    $("#evo-body").innerHTML = kpis + charts + lessonCard + clusterCard + review;
    $("#lesson-form").addEventListener("submit", addLesson);
    drawCharts();
  }
  // Drawn at the width they will be shown at: a stretched viewBox would
  // stretch the axis labels along with the bars.
  function drawCharts() {
    const box = $("#evo-charts");
    if (!box || !D.evo) return;
    const days = D.evo.scorecard?.days || [];
    if (D.showTable) { box.innerHTML = evoTable(days); return; }
    const width = Math.max(320, box.clientWidth || 760);
    const measured = days.some((d) => d.clean_rate !== null && d.clean_rate !== undefined);
    box.innerHTML = (measured ? barChart(days, "clean_rate", { title: "Clean runs", series: "s1", rate: true }, width)
        : `<div class="muted" style="font-size:12.5px;margin-top:12px">Clean runs are counted from today, as each run finishes.
            The two charts below already have history.</div>`)
      + barChart(days, "error_rate", { title: "Tool calls that failed", series: "s2", rate: true }, width)
      + barChart(days, "corrections", { title: "Your corrections", series: "s2" }, width);
    wireCharts();
  }
  // What the night changed, in words: the daily record of the team getting better.
  function changelog(rep) {
    const rows = [
      ...(rep.adopted || []).map((x) => ["check", "good", "Adopted", x.text]),
      ...(rep.learned || []).map((x) => ["spark", "info", "Learned", x.text]),
      ...(rep.retired || []).map((x) => ["x", "", "Retired, it did not help", x.text]),
      ...((rep.regression || {}).suspended || []).map((x) => ["alert", "warn", "Suspended", x.text]),
      ...(rep.attention || []).map((x) => ["alert", "warn", "Needs you", `${x.text} (${x.why})`]),
    ];
    if (!rows.length) return "";
    return `<div class="list card" style="margin-top:10px">${rows.map(([ic, tone, label, text]) => `<div class="item">
      <span class="item-icon ${tone}">${icon(ic)}</span><div class="item-main"><div class="item-sub" style="margin:0 0 2px">${h(label)}</div>
      <div class="item-title" style="font-weight:400">${h(text)}</div></div></div>`).join("")}</div>`;
  }
  const CLUSTER = {
    lesson: ["good", "check", "adopted", "Lesson in place"],
    held: ["warn", "flask", "pending", "Lesson waiting on you"],
    owner: ["", "x", "retired", "You retired its lesson"],
    "gave-up": ["danger", "alert", "failed", "Two lessons failed: needs a fix"],
    open: ["warn", "alert", "pending", "No lesson yet"],
  };
  const SOURCE = { failure: "From repeated failures", correction: "From your correction", "owner-rule": "Your standing rule", owner: "Written by you" };
  function lessonRow(l) {
    const scope = l.agent === "*" ? `${icon("users")}<span>Everywhere</span>` : `${presence(S.agents[l.agent], 16, "idle", true)}<span>${h(agentName(l.agent))}</span>`;
    const statusPill = { trial: pill("trial", "On trial"), active: pill("adopted", "Adopted"), retired: pill("retired", "Retired"),
      suspended: pill("suspended", "Suspended"), proposed: pill("pending", "Waiting for your OK") }[l.status];
    const ev = l.evidence || {};
    const evidence = (ev.corrections || []).length ? `<pre class="out">${h(ev.corrections.join("\n\n"))}</pre>`
      : (ev.samples || []).length ? `<pre class="out">${h(ev.samples.map((s) => `${s.agent}: ${s.call}\n  → ${s.error}${s.then_worked ? `\n  then worked: ${s.then_worked}` : ""}`).join("\n\n"))}</pre>` : "";
    const acts = [
      `<button class="icon-btn ${l.pinned ? "active" : ""}" data-act="lesson-pin" data-id="${l.id}" data-pinned="${l.pinned ? 1 : 0}" title="${l.pinned ? "Unpin" : "Pin: always shown first"}" aria-label="Pin">${icon("pin")}</button>`,
      l.status === "trial" ? `<button class="btn small ghost" data-act="lesson" data-id="${l.id}" data-do="active">Adopt</button>` : "",
      l.status === "proposed" ? `<button class="btn small" data-act="lesson" data-id="${l.id}" data-do="active">Use it</button>` : "",
      ["retired", "suspended"].includes(l.status) ? `<button class="btn small ghost" data-act="lesson" data-id="${l.id}" data-do="${["owner", "owner-rule"].includes(l.source) ? "active" : "trial"}">Restore</button>` : "",
      l.status !== "retired" ? `<button class="btn small ghost danger" data-act="lesson" data-id="${l.id}" data-do="retired">Retire</button>` : "",
    ].join("");
    return `<div class="lesson ${h(l.status)}"><div style="flex:1;min-width:0">
      <div class="text"><span class="lesson-id">L${l.id}</span> ${h(l.text)}</div>
      <div class="meta"><span class="scope">${scope}</span>${statusPill}<span>${h(SOURCE[l.source] || l.source)}</span><span>${h(ago(l.ts))}</span>
        ${l.recurrences ? `<span>${icon("alert")} recurred ${l.recurrences}×</span>` : ""}</div>
      ${l.note ? `<div class="note">${h(l.note)}</div>` : ""}
      ${evidence ? `<details><summary>Why this lesson exists</summary>${evidence}</details>` : ""}
    </div><div class="actions">${acts}</div></div>`;
  }
  async function addLesson(ev) {
    ev.preventDefault();
    const text = $("#lesson-text").value.trim();
    if (!text) return;
    try {
      await api("/api/lessons", { agent: $("#lesson-scope").value, text });
      $("#lesson-text").value = "";
      notify("Lesson added. It applies from the next turn.");
      loadEvolution();
    } catch (err) { notify(err.message, true); }
  }
  function evoTable(days) {
    return `<table class="data"><thead><tr><th>Day</th><th>Runs</th><th>Clean</th><th>Tool calls</th><th>Failed</th><th>Corrections</th><th>Tokens</th></tr></thead><tbody>
      ${[...days].reverse().map((d) => `<tr><td>${h(d.day)}</td><td>${d.runs}</td><td>${pct(d.clean_rate)}</td><td>${d.tool_calls}</td>
        <td>${d.error_rate === null ? "—" : `${(d.error_rate * 100).toFixed(1)}%`}</td><td>${d.corrections}</td><td>${(d.tokens || 0).toLocaleString()}</td></tr>`).join("")}</tbody></table>`;
  }
  // One measure per chart, one axis each: a clean-run rate and a count of
  // corrections on the same axes would invent a relationship between them.
  function barChart(days, key, { title, series, rate = false }, W = 760) {
    const H = 112, L = 34, B = 18, T = 8;
    const vals = days.map((d) => d[key]);
    const max = rate ? 1 : Math.max(2, ...vals.filter((v) => v !== null && v !== undefined));
    const nice = rate ? 1 : Math.ceil(max / 2) * 2;
    const band = (W - L) / days.length, bw = Math.min(14, band * 0.62);
    const y = (v) => T + (H - B - T) * (1 - v / nice);
    const grid = [0, 0.5, 1].map((f) => {
      const v = nice * f, yy = y(v).toFixed(1);
      return `<line x1="${L}" x2="${W}" y1="${yy}" y2="${yy}"/><text x="${L - 6}" y="${(+yy + 3.5).toFixed(1)}" text-anchor="end">${rate ? `${Math.round(v * 100)}%` : Math.round(v)}</text>`;
    }).join("");
    const bars = days.map((d, i) => {
      const v = d[key], x = L + i * band + (band - bw) / 2;
      const hitX = (L + i * band).toFixed(1);
      const hit = `<rect class="hit" x="${hitX}" y="${T}" width="${band.toFixed(1)}" height="${H - B - T + 4}" data-chart="${h(key)}" data-i="${i}" tabindex="-1"/>`;
      if (v === null || v === undefined) return `<rect class="empty-day" x="${x.toFixed(1)}" y="${(H - B - 2).toFixed(1)}" width="${bw.toFixed(1)}" height="2" rx="1"/>${hit}`;
      const top = y(v), hgt = Math.max(0, H - B - top);
      if (hgt < 0.5) return `<rect class="empty-day" x="${x.toFixed(1)}" y="${(H - B - 2).toFixed(1)}" width="${bw.toFixed(1)}" height="2" rx="1"/>${hit}`;
      const r = Math.min(4, bw / 2, hgt);
      const path = `M${x.toFixed(1)} ${(H - B).toFixed(1)} V${(top + r).toFixed(1)} Q${x.toFixed(1)} ${top.toFixed(1)} ${(x + r).toFixed(1)} ${top.toFixed(1)} H${(x + bw - r).toFixed(1)} Q${(x + bw).toFixed(1)} ${top.toFixed(1)} ${(x + bw).toFixed(1)} ${(top + r).toFixed(1)} V${(H - B).toFixed(1)} Z`;
      return `<path class="bar ${series}" d="${path}"/>${hit}`;
    }).join("");
    const label = (i) => new Date(days[i].day + "T12:00:00").toLocaleDateString([], { month: "short", day: "numeric" });
    const ticks = [0, Math.floor(days.length / 2), days.length - 1].map((i, n) =>
      `<text x="${(L + i * band + band / 2).toFixed(1)}" y="${H - 3}" text-anchor="${["start", "middle", "end"][n]}">${h(label(i))}</text>`).join("");
    return `<div style="margin-top:12px"><div class="muted" style="font-size:12px;margin-bottom:2px">${h(title)}</div>
      <svg class="chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${h(title)} per day">
      <g class="grid">${grid}</g><g class="axis"><line x1="${L}" x2="${W}" y1="${H - B}" y2="${H - B}"/></g>${bars}${ticks}</svg></div>`;
  }
  function wireCharts() {
    let tip = document.querySelector(".chart-tip");
    if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip hidden"; document.body.append(tip); }
    const days = D.evo?.scorecard?.days || [];
    const label = { clean_rate: "clean runs", error_rate: "tool calls failed", corrections: "corrections" };
    document.querySelectorAll("#evo-charts .hit").forEach((el) => {
      el.addEventListener("pointermove", (ev) => {
        const d = days[+el.dataset.i], key = el.dataset.chart;
        if (!d) return;
        const v = d[key];
        const value = v === null || v === undefined ? "No runs" : key === "corrections" ? String(v) : `${Math.round(v * 100)}%`;
        const extra = key === "clean_rate" && d.runs ? `${d.clean} of ${d.runs} runs` : key === "error_rate" && d.tool_calls ? `${d.tool_errors} of ${d.tool_calls} calls` : "";
        tip.innerHTML = "";
        const strong = document.createElement("div"); strong.className = "v"; strong.textContent = value;
        const k = document.createElement("div"); k.className = "k"; k.textContent = `${label[key]} · ${new Date(d.day + "T12:00:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}`;
        tip.append(strong, k);
        if (extra) { const x = document.createElement("div"); x.className = "k"; x.textContent = extra; tip.append(x); }
        tip.classList.remove("hidden");
        tip.style.left = `${Math.min(innerWidth - 180, ev.clientX + 14)}px`;
        tip.style.top = `${ev.clientY - 54}px`;
        el.previousElementSibling?.classList.add("hover");
      });
      el.addEventListener("pointerleave", () => { tip.classList.add("hidden"); el.previousElementSibling?.classList.remove("hover"); });
    });
  }

  /* ---------- agent profile panel ---------- */
  // On a wide window the panel follows the owner's last choice; on a narrow
  // one it covers the chat, so it opens only when asked for and closes after.
  const wide = () => innerWidth > 1180;
  const panelOpen = () => { try { return localStorage.getItem("mc-panel") !== "closed"; } catch { return true; } };
  function setPanel(open) {
    if (wide()) { try { localStorage.setItem("mc-panel", open ? "open" : "closed"); } catch {} }
    else D.panelForced = open;
    applyPanel();
  }
  function applyPanel() {
    const t = typeof currentThread === "function" ? currentThread() : null;
    const show = !!t && t.kind === "dm" && D.view === "chat" && (wide() ? panelOpen() : !!D.panelForced);
    $("#agent-panel").classList.toggle("hidden", !show);
    $("#panel-btn").classList.toggle("active", show);
    $("#panel-btn").classList.toggle("hidden", !t || t.kind !== "dm");
    if (show) loadPanel(t.agent);
  }
  async function loadPanel(aid) {
    D.panelAgent = aid;
    try {
      const p = await api(`/api/agents/${encodeURIComponent(aid)}/profile`);
      if (D.panelAgent === aid) renderPanel(p);
    } catch (err) {
      $("#agent-panel").innerHTML = `<div class="ap-empty">${h(err.message)}</div>`;
    }
  }
  function renderPanel(p) {
    const a = S.agents[p.agent.id] || p.agent;
    const st = p.state || "idle";
    const s = p.stats || {};
    const row = (ic, title, sub) => `<div class="ap-row">${icon(ic)}<div class="t">${h(title)}${sub ? `<small>${h(sub)}</small>` : ""}</div></div>`;
    const now = [
      ...p.now.map((e) => row("bolt", `Running ${e.name.replaceAll("_", " ")}`, toolDetail(e))),
      ...(p.queued ? [row("inbox", `${plural(p.queued, "message")} waiting in its queue`)] : []),
    ];
    const scheduled = [
      ...p.scheduled.jobs.map((j) => row("clock", j.name, `${j.schedule} · ${j.status}`)),
      ...p.scheduled.watches.map((w) => row("refresh", w.name, `watch · ${w.checks}/${w.max_checks} checks`)),
    ];
    $("#agent-panel").innerHTML = `<div class="ap-head">${presence(a, 64, moodOf(a.id))}
        <h3>${h(a.name)}</h3><div class="role">${h(p.agent.role || "A role tasks can run as")}</div>
        <span class="model" title="${p.agent.own_model ? "Its own model" : "The default model"}">${h(p.agent.model || "default model")}</span>
        <div class="ap-actions"><button class="btn small" data-act="edit-agent" data-agent="${h(a.id)}">${icon("edit")}Edit</button>
          ${st !== "idle" ? `<button class="btn small danger" data-act="stop-agent" data-agent="${h(a.id)}">${icon("stop", "fill")}Stop</button>` : ""}</div></div>
      <div class="ap-stats"><div><b>${s.clean_rate === null || s.clean_rate === undefined ? "—" : pct(s.clean_rate)}</b><span>clean runs</span></div>
        <div><b>${s.runs ?? 0}</b><span>runs this week</span></div><div><b>${s.corrections ?? 0}</b><span>corrections</span></div></div>
      <div class="ap-section"><h4>Now</h4>${now.join("") || `<div class="ap-empty">${h(st === "idle" ? "Idle" : activityOf(a.id))}</div>`}</div>
      <div class="ap-section"><h4>Active <span>${p.active.length || ""}</span></h4>${p.active.map((m) => row("tasks", m.objective.slice(0, 140), `${m.status} · ${ago(m.updated)}`)).join("") || `<div class="ap-empty">No open tasks</div>`}</div>
      <div class="ap-section"><h4>Scheduled <span>${scheduled.length || ""}</span></h4>${scheduled.join("") || `<div class="ap-empty">Nothing scheduled</div>`}</div>
      <div class="ap-section"><h4>Done this week <span>${p.done.length || ""}</span></h4>${p.done.map((m) => row(m.status === "completed" ? "check" : "alert", m.objective.slice(0, 140), `${m.status} · ${ago(m.updated)}`)).join("") || `<div class="ap-empty">Nothing finished yet this week</div>`}</div>
      <div class="ap-section"><h4>Lessons <span>${p.lessons.length || ""}</span></h4>${p.lessons.slice(0, 8).map((l) => row(l.status === "active" ? "check" : "flask", l.text, `${l.agent === "*" ? "everywhere" : "own"} · ${l.status === "active" ? "adopted" : "on trial"}`)).join("") || `<div class="ap-empty">No lessons yet</div>`}</div>`;
  }
  function refreshPanelSoon() {
    if (D.view !== "chat" || $("#agent-panel").classList.contains("hidden") || !D.panelAgent) return;
    clearTimeout(panelTimer);
    panelTimer = setTimeout(() => loadPanel(D.panelAgent), 600);
  }

  /* ---------- inspector ---------- */
  function inspect(title, html) {
    restoreFocus = document.activeElement;
    $("#inspector-title").textContent = title;
    $("#inspector-content").innerHTML = html;
    $("#execution-inspector").classList.remove("hidden");
    $("#inspector-close").focus();
  }
  function closeInspector() { $("#execution-inspector").classList.add("hidden"); restoreFocus?.focus?.(); }
  function inspectMission(id) {
    const m = (D.data.missions || []).find((x) => x.id === id);
    if (!m) return;
    const records = (D.data.executions || []).filter((e) => e.mission === id);
    const worker = (D.data.workers || []).find((w) => w.mission === id);
    inspect("Task", `${pill(m.status)}<h3>Objective</h3><pre>${h(m.objective)}</pre>
      <h3>Latest result or checkpoint</h3><pre>${h(m.result || "No result has been reported yet.")}</pre>
      ${records.length ? `<h3>Steps recorded</h3><div class="list card">${records.slice(0, 12).map((e) => `<button class="item" data-act="execution" data-id="${h(e.id)}">
        <span class="item-icon">${icon("bolt")}</span><div class="item-main"><div class="item-title">${h(e.name.replaceAll("_", " "))}</div><div class="item-sub">${h(agentName(e.agent))} · ${h(ago(e.ts))}</div></div>${pill(e.status)}</button>`).join("")}</div>` : ""}
      <div class="detail-meta">${h(agentName(m.agent))} · task ${h(m.id)} · started ${h(new Date(m.ts * 1000).toLocaleString())}</div>
      <div class="actions">${worker ? `<button class="btn primary" data-act="worker" data-id="${h(worker.id)}">Open worker</button>` : ""}
        <button class="btn${worker ? "" : " primary"}" data-act="thread" data-thread="${h(m.thread)}">Open conversation</button>
        ${["waiting", "failed", "cancelled"].includes(m.status) ? `<button class="btn" data-act="resume" data-id="${h(m.id)}">Resume</button>` : ""}</div>`);
  }
  function inspectExecution(id) {
    const e = (D.data.executions || []).find((x) => x.id === id);
    if (!e) return;
    inspect(e.name.replaceAll("_", " "), `${pill(e.status)}<div class="detail-meta">${h(agentName(e.agent))} · ${h(e.kind)} · ${h(new Date(e.ts * 1000).toLocaleTimeString())}
      · ${Math.max(0, e.updated - e.ts).toFixed(1)}s${e.mission ? ` · task ${h(e.mission)}` : ""}</div>
      <h3>Inputs</h3><pre>${h(JSON.stringify(e.detail, null, 2))}</pre><h3>Result</h3><pre>${h(e.result || (e.status === "running" ? "Running…" : "No output recorded."))}</pre>`);
  }
  async function inspectSkill(slug, run = false) {
    const s = (D.data.skills || []).find((x) => x.slug === slug);
    if (!s) return;
    if (!run) {
      try {
        const data = await api(`/api/skills/${encodeURIComponent(slug)}`);
        inspect(s.name, `<pre>${h(data.content)}</pre><h3>Contract</h3><pre>${h(JSON.stringify(s.contract, null, 2))}</pre>${s.error ? `<p class="skill-error">${h(s.error)}</p>` : ""}
          <div class="actions"><button class="btn" data-act="settings" data-tab="memory">Edit in settings</button></div>`);
      } catch (err) { notify(err.message, true); }
      return;
    }
    const sample = {};
    for (const [name, spec] of Object.entries(s.contract?.inputs?.properties || {})) {
      sample[name] = spec.default ?? spec.examples?.[0] ?? (spec.type === "number" || spec.type === "integer" ? 1
        : spec.type === "boolean" ? false : spec.type === "array" ? [] : spec.type === "object" ? {} : "");
    }
    inspect(`Run ${s.name}`, `<p style="font-size:13px;color:var(--text-2)">${h(s.description)}</p>
      <div class="detail-meta">${s.contract.max_tool_calls} tool calls · ${s.contract.timeout_seconds}s · version ${h(s.contract.version)}</div>
      <h3>Inputs</h3><textarea id="skill-input-json" spellcheck="false">${h(JSON.stringify(sample, null, 2))}</textarea>
      <div class="actions"><button class="btn primary" id="skill-launch">${icon("play", "fill")}Start</button></div><div class="skill-error" id="skill-launch-error" role="alert"></div>`);
    $("#skill-launch").addEventListener("click", async () => {
      const b = $("#skill-launch"); b.disabled = true;
      try {
        const inputs = JSON.parse($("#skill-input-json").value);
        const result = await api("/api/command", { text: `/skill ${slug} ${JSON.stringify(inputs)}` });
        closeInspector(); show("missions"); notify(`Skill started as task ${result.mission.id}.`); refresh();
      } catch (err) { $("#skill-launch-error").textContent = err.message; }
      finally { b.disabled = false; }
    });
  }
  // A patch as it reads: what is added, what goes, and the lines that only say where.
  function patchHtml(diff) {
    if (!diff) return `<pre>(empty)</pre>`;
    return `<pre class="patch">${String(diff).split("\n").map((line) => {
      const cls = /^(\+\+\+|---|@@|diff |index )/.test(line) ? "meta" : line[0] === "+" ? "add" : line[0] === "-" ? "del" : "";
      return `<span class="${cls}">${h(line) || " "}</span>`;
    }).join("\n")}</pre>`;
  }
  async function showDiff(id) {
    try {
      const d = await api(`/api/improvements/${id}/diff`);
      inspect(d.title || "Proposed change", patchHtml(d.diff));
    } catch (err) { notify(err.message, true); }
  }

  /* ---------- palette ---------- */
  const ACTIONS = [
    { title: "New task", icon: "plus", run: () => { show("home"); setTimeout(() => $("#home-input")?.focus(), 30); } },
    { title: "New agent", icon: "users", run: () => openModal() },
    { title: "Settings", icon: "settings", run: () => openSettings() },
    { title: "Switch theme", icon: "moon", run: () => cycleTheme() },
    { title: "Run the learning review now", icon: "growth", run: () => runReview() },
  ];
  function paletteItems() {
    const q = $("#palette-search").value.toLowerCase().trim();
    const out = [];
    const views = Object.entries(VIEWS).map(([id, title]) => ({ group: "Go to", title, icon: VIEW_ICONS[id], run: () => show(id) }));
    const agents = Object.values(S.agents).sort((a, b) => (a.order ?? 99) - (b.order ?? 99)).map((a) => ({
      group: "Profiles", title: a.name, sub: a.blurb, face: a, run: () => openThread(`dm:${a.id}`) }));
    agents.unshift({ group: "Profiles", title: "Group chat", sub: "The agent, or any profile you @mention", icon: "users", run: () => openThread("group:team") });
    const commands = D.commands.map((c) => ({ group: "Commands", title: c.usage, sub: c.description, icon: "term",
      run: () => ["help", "status", "agents", "watches"].includes(c.name) ? runCommand(`/${c.name}`) : prefill(`/${c.name} `) }));
    for (const item of [...views, ...agents, ...ACTIONS.map((a) => ({ ...a, group: "Actions" })), ...commands]) {
      if (!q || `${item.title} ${item.sub || ""}`.toLowerCase().includes(q)) out.push(item);
    }
    // What was said, found by the server: choosing one opens the chat at that message.
    for (const m of (q && D.paletteHits) || []) {
      const thread = threadOfHit(m);
      if (!thread) continue;
      const t = S.threads.find((x) => x.id === thread);
      const who = m.sender === "user" ? "You" : agentName(m.sender);
      out.push({ group: "Messages", title: clip(m.text, 90), sub: `${who} · ${t.name} · ${sidebarTime(m.ts)}`, icon: "chat", run: () => openThread(thread, m.id) });
    }
    return out;
  }
  function renderPalette() {
    const items = paletteItems();
    D.paletteIndex = Math.min(D.paletteIndex, Math.max(0, items.length - 1));
    let group = null;
    $("#palette-results").innerHTML = items.map((it, i) => {
      const head = it.group !== group ? `<div class="palette-group">${h((group = it.group))}</div>` : "";
      return `${head}<button class="palette-option ${i === D.paletteIndex ? "selected" : ""}" role="option" aria-selected="${i === D.paletteIndex}" data-palette="${i}">
        ${it.face ? presence(it.face, 20, "idle", true) : icon(it.icon || "chev")}<strong>${h(it.title)}</strong>${it.sub ? `<small>${h(it.sub)}</small>` : ""}</button>`;
    }).join("") || `<div class="empty compact"><span>No matches.</span></div>`;
    $("#palette-results .selected")?.scrollIntoView({ block: "nearest" });
  }
  function openPalette(query = "") {
    restoreFocus = document.activeElement;
    D.paletteIndex = 0; D.paletteHits = null;
    $("#command-palette").classList.remove("hidden");
    $("#palette-search").value = query;
    renderPalette();
    $("#palette-search").focus();
  }
  function closePalette() { $("#command-palette").classList.add("hidden"); restoreFocus?.focus?.(); }
  function choosePalette(i) {
    const item = paletteItems()[i];
    if (!item) return;
    closePalette();
    item.run();
  }
  async function runCommand(text) {
    try {
      const r = await api("/api/command", { text });
      if (D.view === "home") feedback(r.reply || "Done.");
      else notify(r.reply || "Done.");
    } catch (err) { notify(err.message, true); }
  }
  async function runReview() {
    try {
      await api("/api/evolution/review", {});
      notify("Review started. The Evolution page updates when it finishes.");
    } catch (err) { notify(err.message, true); }
  }

  /* ---------- theme ---------- */
  function theme() { try { return localStorage.getItem("mc-theme") || "auto"; } catch { return "auto"; } }
  function applyTheme() {
    const t = theme();
    if (t === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    const ic = { auto: "auto", light: "sun", dark: "moon" }[t];
    const label = { auto: "Theme: follows the system", light: "Theme: light", dark: "Theme: dark" }[t];
    $("#theme-btn").innerHTML = icon(ic);
    $("#theme-btn").title = label;
    $("#theme-btn").setAttribute("aria-label", label);
  }
  function cycleTheme() {
    const next = { auto: "light", light: "dark", dark: "auto" }[theme()];
    try { localStorage.setItem("mc-theme", next); } catch {}
    applyTheme();
    notify({ auto: "Theme follows your system.", light: "Light theme.", dark: "Dark theme." }[next]);
  }

  /* ---------- one click handler for every view ---------- */
  async function act(b) {
    const d = b.dataset;
    switch (d.act) {
      case "show": if (d.tab) D.knowTab = d.tab; show(d.view); break;
      case "chat": openThread(`dm:${d.agent}`); break;
      case "thread": closeInspector(); openThread(d.thread); break;
      case "new-agent": openModal(); break;
      case "new-task": show("home"); setTimeout(() => $("#home-input")?.focus(), 30); break;
      case "prefill": prefill(d.text); break;
      case "run-command": runCommand(d.command); break;
      case "dismiss": b.closest(".feedback")?.remove(); break;
      case "retry": refresh(); break;
      case "settings": openSettings(d.tab); break;
      case "mission": inspectMission(d.id); break;
      case "execution": inspectExecution(d.id); break;
      case "skill": inspectSkill(d.slug); break;
      case "run-skill": inspectSkill(d.slug, true); break;
      case "diff": showDiff(d.id); break;
      case "task-filter": D.taskFilter = d.filter; keep("mc-task-filter", d.filter); updateTasks(); break;
      case "event-filter": D.eventFilter = d.filter; keep("mc-event-filter", d.filter); updateActivity(); break;
      case "worker": await inspectWorker(d.id); break;
      case "recall": {
        const out = await api(`/api/kernel/recall/${encodeURIComponent(d.id)}`);
        inspect("Event", `<pre>${h(out.text)}</pre>`);
        break;
      }
      case "policy": await policyCall({ action: d.action, id: d.id }); break;
      case "policy-add": {
        const box = $("#policy-text");
        const text = box.value.trim();
        if (!text) { box.focus(); break; }
        await policyCall({ action: "add", text });
        box.value = "";
        break;
      }
      case "answer": {
        const box = document.getElementById(`answer-${d.id}`);
        const text = (box?.value || "").trim();
        if (!text) break;
        b.disabled = true;
        try { await api(`/api/kernel/sessions/${encodeURIComponent(d.id)}/message`, { text }); delete D.answers[d.id]; notify("Sent."); refresh(); }
        catch (err) { notify(err.message, true); b.disabled = false; }
        break;
      }
      case "pause-toggle":
        b.disabled = true;
        try { await api(D.kernel?.paused ? "/api/kernel/resume" : "/api/kernel/pause", {}); await loadKernel(); }
        catch (err) { notify(err.message, true); }
        b.disabled = false;
        break;
      case "step-in": {
        const box = $("#step-in-text");
        const text = box.value.trim();
        if (!text) { box.focus(); break; }
        b.disabled = true;
        try {
          await api(`/api/kernel/sessions/${encodeURIComponent(d.id)}/message`, { text });
          notify("Sent");
          await inspectWorker(d.id);
        } catch (err) { notify(err.message, true); }
        b.disabled = false;
        break;
      }
      case "worker-action": {
        await api(`/api/kernel/sessions/${encodeURIComponent(d.id)}/${d.action}`, {});
        notify(d.action === "stop" ? "Stopped" : "Restarted");
        closeInspector();
        scheduleRefresh();
        scheduleKernel();
        break;
      }
      case "know-tab": D.knowTab = d.tab; loadKnowledge(); break;
      case "evo-filter": D.evoFilter = d.filter; renderEvolution(); break;
      case "evo-table":
        D.showTable = !D.showTable;
        b.textContent = D.showTable ? "Show charts" : "View as table";
        drawCharts();
        break;
      case "evo-review": b.disabled = true; await runReview(); setTimeout(() => { b.disabled = false; }, 4000); break;
      case "edit-agent": openEditModal(d.agent); break;
      case "stop-agent": await fetch(`/api/agents/${d.agent}/stop`, { method: "POST" }); notify(`Stopped ${agentName(d.agent)}.`); break;
      case "resume":
        b.disabled = true;
        try { await api("/api/command", { text: `/resume ${d.id}` }); closeInspector(); notify("Resumed."); refresh(); }
        catch (err) { notify(err.message, true); b.disabled = false; }
        break;
      case "approval":
        b.disabled = true;
        try { await api(`/api/approvals/${d.id}/${d.do}`, {}); notify(d.do === "approve" ? "Ran it. The task has the output." : "Declined. The agent has been told."); refresh(); }
        catch (err) { notify(err.message, true); b.disabled = false; }
        break;
      case "job":
        b.disabled = true;
        try { await api(`/api/cron/${d.id}/${d.do}`, {}); await pollPendingJobs(); refresh(); }
        catch (err) { notify(err.message, true); b.disabled = false; }
        break;
      case "proposal":
        if (d.do === "approve" && !confirmOnce(b, "Approve and restart?")) return;
        b.disabled = true;
        try {
          await api(`/api/improvements/${d.id}/${d.do}`, {});
          if (d.do === "approve") { setLink(false); notify("Applying the change and restarting. If it does not come back, it rolls back by itself."); }
          refresh();
        } catch (err) { notify(err.message, true); b.disabled = false; }
        break;
      case "lesson":
        b.disabled = true;
        try { await api(`/api/lessons/${d.id}`, { status: d.do }, "PUT"); loadEvolution(); refresh(); }
        catch (err) { notify(err.message, true); b.disabled = false; }
        break;
      case "lesson-pin":
        try { await api(`/api/lessons/${d.id}`, { pinned: d.pinned !== "1" }, "PUT"); loadEvolution(); }
        catch (err) { notify(err.message, true); }
        break;
      case "cancel-watch":
        b.disabled = true;
        try { await api(`/api/watches/${d.id}/cancel`, {}); refresh(); } catch (err) { notify(err.message, true); b.disabled = false; }
        break;
      case "remember": prefill("/remember "); break;
      case "mem-del":
        if (!confirmOnce(b, "Delete?")) return;
        await fetch(`/api/memory/${d.id}`, { method: "DELETE" }); loadMemory(); break;
      case "mem-edit": {
        const row = b.closest("[data-mem]"), textEl = row.querySelector("[data-mem-text]");
        if (row.querySelector("textarea")) return;
        const ta = document.createElement("textarea");
        ta.className = "deck-input"; ta.style.width = "100%"; ta.rows = 3; ta.value = textEl.textContent;
        const save = document.createElement("button"); save.className = "btn small primary"; save.textContent = "Save"; save.style.marginTop = "8px";
        textEl.replaceWith(ta); ta.after(save); ta.focus();
        save.addEventListener("click", async () => {
          try { await api(`/api/memory/${d.id}`, { text: ta.value }, "PUT"); loadMemory(); } catch (err) { notify(err.message, true); }
        });
        break;
      }
      case "pb-del":
        if (!confirmOnce(b, "Delete?")) return;
        await api("/api/playbooks/delete", { site: d.site, title: d.title }); loadPlaybooks(); break;
    }
  }
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-act]");
    if (b && !b.disabled) { act(b); return; }
    const p = ev.target.closest("[data-palette]");
    if (p) choosePalette(Number(p.dataset.palette));
  });
  document.addEventListener("keydown", (ev) => {
    const row = ev.target.closest?.(".item.clickable[data-act]");
    if (row && (ev.key === "Enter" || ev.key === " ") && ev.target === row) { ev.preventDefault(); act(row); }
  });
  document.querySelectorAll("#sidebar button[data-deck]").forEach((b) => b.addEventListener("click", () => show(b.dataset.deck)));
  $("#more-toggle").addEventListener("click", () => {
    const open = $("#more-nav").classList.contains("hidden");
    try { localStorage.setItem("mc-more", open ? "open" : "closed"); } catch {}
    if (!open && MORE.includes(D.view)) return;      // the view in front lives there
    $("#more-nav").classList.toggle("hidden", !open);
    $("#more-toggle").setAttribute("aria-expanded", String(open));
  });
  $("#search-toggle").addEventListener("click", () => {
    const open = $("#search-wrap").classList.toggle("hidden") === false;
    $("#search-toggle").setAttribute("aria-expanded", String(open));
    if (open) $("#search").focus();
    else { $("#search").value = ""; $("#search").dispatchEvent(new Event("input")); }
  });
  $("#palette-btn").addEventListener("click", () => openPalette());
  $("#palette-close").addEventListener("click", closePalette);
  $("#inspector-close").addEventListener("click", closeInspector);
  $("#command-palette").addEventListener("click", (e) => { if (e.target.id === "command-palette") closePalette(); });
  $("#execution-inspector").addEventListener("click", (e) => { if (e.target.id === "execution-inspector") closeInspector(); });
  let paletteTimer;
  $("#palette-search").addEventListener("input", () => {
    D.paletteIndex = 0; renderPalette();
    const q = $("#palette-search").value.trim();
    clearTimeout(paletteTimer);
    if (q.length < 2) { D.paletteHits = null; return; }
    paletteTimer = setTimeout(async () => {
      const seq = ++D.paletteSeq;
      try {
        const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
        if (seq !== D.paletteSeq) return;
        D.paletteHits = (data.results || []).slice(0, 12);
      } catch { D.paletteHits = null; }
      if (!$("#command-palette").classList.contains("hidden")) renderPalette();
    }, 180);
  });
  document.addEventListener("input", (e) => { if (e.target.dataset?.answer) D.answers[e.target.dataset.answer] = e.target.value; });
  $("#palette-search").addEventListener("keydown", (e) => {
    const n = paletteItems().length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      D.paletteIndex = (D.paletteIndex + (e.key === "ArrowDown" ? 1 : -1) + n) % Math.max(1, n);
      renderPalette();
    }
    if (e.key === "Enter") { e.preventDefault(); choosePalette(D.paletteIndex); }
  });
  $("#theme-btn").addEventListener("click", cycleTheme);
  $("#panel-btn").addEventListener("click", () => setPanel($("#agent-panel").classList.contains("hidden")));
  document.querySelectorAll("[data-menu]").forEach((b) => b.addEventListener("click", () => document.body.classList.add("nav-open")));
  $("#scrim").addEventListener("click", () => document.body.classList.remove("nav-open"));
  $("#side-close").addEventListener("click", () => document.body.classList.remove("nav-open"));
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
    if (e.key === "Escape") {
      if (!$("#command-palette").classList.contains("hidden")) closePalette();
      else if (!$("#execution-inspector").classList.contains("hidden")) closeInspector();
      else document.body.classList.remove("nav-open");
    }
    // keep keyboard focus inside an open dialog
    if (e.key === "Tab") {
      const dialog = !$("#command-palette").classList.contains("hidden") ? $("#command-palette")
        : !$("#execution-inspector").classList.contains("hidden") ? $("#execution-inspector") : null;
      if (!dialog) return;
      const nodes = [...dialog.querySelectorAll("button:not(:disabled), input, textarea, [tabindex='0']")];
      const first = nodes[0], last = nodes.at(-1);
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
    }
  });

  /* ---------- live events ---------- */
  window.addEventListener("mc:event", ({ detail: ev }) => {
    if (ev.type === "agent_state") {
      D.data.agents[ev.agent] = { ...(D.data.agents[ev.agent] || {}), state: ev.state };
      if (D.view === "home") { updateTeam(); route(); }
      if (D.panelAgent === ev.agent) refreshPanelSoon();
    }
    if (ev.type === "execution") {
      const r = ev.execution, i = (D.data.executions || []).findIndex((x) => x.id === r.id);
      if (i >= 0) D.data.executions[i] = r; else (D.data.executions = [r, ...(D.data.executions || [])].slice(0, 120));
      if (r.agent === D.panelAgent) refreshPanelSoon();
    }
    if (["mission_updated", "watch_updated", "memory_updated", "skills_updated", "agent_added", "agent_updated",
         "agent_removed", "cron_updated", "approvals_updated", "improvements_updated", "browser_auth_updated",
         "evolution_updated"].includes(ev.type)) {
      scheduleRefresh();
      refreshPanelSoon();
    }
    if (["trigger", "session_updated", "watchdog", "kernel_updated", "mission_updated"].includes(ev.type)) {
      scheduleKernel();
      if (ev.type === "session_updated") scheduleRefresh();
    }
    if (ev.type === "evolution_updated" && D.view === "evolution") loadEvolution();
    if (ev.type === "memory_updated" && D.view === "knowledge" && D.knowTab === "memory") loadMemory();
  });
  window.addEventListener("mc:thread", () => applyPanel());
  // A step landed, or a task came or went: the cards on this page follow.
  window.addEventListener("mc:work", ({ detail }) => {
    if (D.view === "home") { updateHomeWork(detail.shape); updateTeam(); }
    else if (D.view === "missions") { if (detail.shape) updateTasks(); else Work.paint($("#task-list")); }
  });
  let resizeTimer;
  window.addEventListener("resize", () => {
    if (D.view === "chat") applyPanel();
    if (D.view === "evolution") { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawCharts, 150); }
  });
  window.addEventListener("mc:connection", ({ detail: online }) => {
    const el = $("#deck-connection");
    el.classList.toggle("on", online);
    el.querySelector("span").textContent = online ? "Connected" : "Reconnecting…";
  });
  window.addEventListener("mc:refresh", () => scheduleRefresh());
  window.addEventListener("mc:ready", () => {
    refresh();
    api("/api/commands").then((x) => { D.commands = x.commands; }).catch(() => {});
  });
  // A quiet poll so ages and counts stay honest without a live event.
  setInterval(() => { if (!document.hidden && D.loaded && D.view !== "chat") refresh(); }, 20000);
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  $("#palette-btn").title = `Search and commands (${mac ? "⌘K" : "Ctrl K"})`;

  applyTheme();
  // A module that failed or waits for approval opens More by itself and marks its row.
  function modulesChanged(needs) {
    D.moreNeeds = needs;
    const mark = $("#nav-module-count");
    mark.textContent = needs; mark.classList.toggle("hidden", !needs);
    applyMore();
  }
  window.MissionDeck = { show, showChat, notify, refresh, api, inspect, prefill, modulesChanged };
  show("home");
})();
