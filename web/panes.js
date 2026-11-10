/* Mission Control: settings panes drawn from what the server says, not from markup:
   the profiles, updates, mail cleanup, and the runtime with its services. */
const MorePanes = (() => {
  const h = (v) => esc(String(v ?? ""));
  const pane = (id) => document.getElementById(`pane-${id}`);
  const say = (text, error) => window.MissionDeck?.notify(text, error);
  async function call(path, body, method) {
    const opts = body === undefined && !method ? {} : { method: method || "POST", headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) };
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch {}
    if (!res.ok || data.ok === false) throw new Error(data.error || (typeof data.detail === "string" ? data.detail : "That did not work. Please try again."));
    return data;
  }
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "never");
  const failed = (id, err) => { pane(id).innerHTML = `<div class="set-hint warn">${h(err.message)}</div>`; };

  /* ---------- profiles ---------- */
  function profiles() {
    const agents = Object.values(S.agents).sort((a, b) => (a.order ?? 99) - (b.order ?? 99));
    pane("profiles").innerHTML = `<div class="set-hint top">A profile is a role a task can run as: its own instructions, folder, model and limits. The first one is the lead, the one you talk to.</div>
      <div class="set-card list">${agents.map((a) => `<button class="item clickable" data-pane-act="edit-profile" data-id="${h(a.id)}">${presence(a, 28, "idle", true)}
        <div class="item-main"><div class="item-title">${h(a.name)}${a.id === S.imessageAgent && S.imessageEnabled ? ` <span class="pill info">iMessage</span>` : ""}</div>
        <div class="item-sub clamp">${h([a.llm?.model, a.blurb || String(a.prompt || "").replace(/\s+/g, " ").slice(0, 70)].filter(Boolean).join(" · "))}</div></div>
        <svg class="i"><use href="#i-chev"/></svg></button>`).join("")}</div>
      <div class="set-footer"><button class="btn primary" data-pane-act="new-profile">New profile</button></div>`;
  }

  /* ---------- updates ---------- */
  const UPDATE_STATE = { app: "This app updates itself.", none: "Updates are not available for this install.", blocked: "An update is waiting, but cannot be installed yet.",
    current: "Up to date.", available: "An update is available.", unknown: "Not checked yet." };
  async function updates(fresh) {
    let u;
    try { u = fresh || await call("/api/updates"); } catch (err) { failed("updates", err); return; }
    const source = u.state !== "app";
    pane("updates").innerHTML = `<div class="set-card">
        <div class="set-row"><div class="set-row-label">${h(UPDATE_STATE[u.state] || u.state)}</div>
          <div class="set-row-note">${h(u.note || "")}${u.target ? ` Latest: ${h(u.target)}.` : ""} Last checked ${h(when(u.checked))}.</div>
          <button class="btn" data-pane-act="update-check">Check now</button>
          ${u.state === "available" ? `<button class="btn primary" data-pane-act="update-install">Update now</button>` : ""}</div>
        ${source ? `<div class="set-row"><div class="set-row-label">Update on its own</div>
          <div class="set-row-note">Only ever fast-forwards a clean checkout, waits until nothing is running, and puts the previous version back if the new one does not come up.</div>
          <label class="switch"><input type="checkbox" id="upd-auto" ${u.auto ? "checked" : ""}><span></span></label></div>
        <div class="set-row"><div class="set-row-label">Follow</div>
          <select class="set-control" id="upd-channel"><option value="release" ${u.channel !== "master" ? "selected" : ""}>Tagged releases</option><option value="master" ${u.channel === "master" ? "selected" : ""}>Every change on master</option></select></div>` : ""}
      </div><div class="set-status" id="upd-status"></div>`;
    const put = async (body) => { try { await call("/api/updates", body, "PUT"); updates(); } catch (err) { say(err.message, true); updates(); } };
    document.getElementById("upd-auto")?.addEventListener("change", (e) => put({ auto: e.target.checked }));
    document.getElementById("upd-channel")?.addEventListener("change", (e) => put({ channel: e.target.value }));
  }

  /* ---------- mail cleanup ---------- */
  let mailTimer;
  async function mail(fresh) {
    clearTimeout(mailTimer);
    let m, laya = {};
    try { m = fresh || await call("/api/mail/sweep"); } catch (err) { failed("mail", err); return; }
    try { laya = await call("/api/laya"); } catch {}
    const st = m.settings || {}, last = m.last;
    const lastLine = !last ? "It has not run yet."
      : last.error ? `The last sweep stopped: ${last.error}`
      : `Last sweep ${when(last.ts)}${last.dry_run ? " (a dry run)" : ""}: looked at ${last.scanned ?? 0}, ${last.dry_run ? `would archive ${last.archive ?? 0}` : `archived ${last.archived ?? 0}`}, kept ${last.kept ?? 0}, held ${last.held ?? 0}.`;
    const senders = (list, title, action) => (list || []).length ? `<div class="set-row stack"><div class="set-row-label">${title}</div>
      ${list.map((s) => `<div class="pane-line"><span>${h(typeof s === "string" ? s : s.sender)}</span><button class="btn small ghost" data-pane-act="mail-sender" data-sender="${h(typeof s === "string" ? s : s.sender)}" data-action="${action}">Remove</button></div>`).join("")}</div>` : "";
    pane("mail").innerHTML = `<div class="set-hint top">Archives bulk mail nobody reads, judged sender by sender on this machine. It only ever moves mail to the archive folder, and never mail from a person.</div>
      <div class="set-card">
        <div class="set-row"><div class="set-row-label">Clean up mail</div>
          <div class="set-row-note">The judge is ${laya.enabled ? (laya.health ? "running" : `waiting${laya.problem ? `: ${h(laya.problem)}` : ""}`) : "off"}.</div>
          <label class="switch"><input type="checkbox" id="mail-enabled" ${st.enabled ? "checked" : ""}><span></span></label></div>
        <div class="set-row"><div class="set-row-label">Dry run</div><div class="set-row-note">Decide and report, but move nothing.</div>
          <label class="switch"><input type="checkbox" id="mail-dry" ${st.dry_run ? "checked" : ""}><span></span></label></div>
        <div class="set-row"><div class="set-row-label">Every (minutes)</div><input id="mail-every" class="set-control" type="number" min="15" max="1440" value="${h(st.every_minutes)}"></div>
        <div class="set-row"><div class="set-row-label">Archive when this sure it is bulk</div><input id="mail-bulk" type="range" min="0.5" max="1" step="0.05" value="${h(st.bulk_at)}"><span class="set-status" id="mail-bulk-v">${Math.round(st.bulk_at * 100)}%</span></div>
        <div class="set-row"><div class="set-row-label">Keep when this likely to matter</div><input id="mail-keep" type="range" min="0" max="1" step="0.05" value="${h(st.keep_at)}"><span class="set-status" id="mail-keep-v">${Math.round(st.keep_at * 100)}%</span></div>
        <div class="set-footer"><span class="set-status">${h(lastLine)} ${m.archived_total || 0} archived in all, ${m.senders_judged || 0} senders judged.</span>
          <button class="btn" data-pane-act="mail-run" ${m.running ? "disabled" : ""}>${m.running ? "Running…" : "Run now"}</button>
          <button class="btn primary" data-pane-act="mail-save">Save</button></div>
        ${senders(m.keep, "Always kept", "keep")}${senders(m.always, "Always archived", "always")}
      </div>`;
    for (const k of ["bulk", "keep"]) document.getElementById(`mail-${k}`).addEventListener("input", (e) => { document.getElementById(`mail-${k}-v`).textContent = `${Math.round(e.target.value * 100)}%`; });
    // Turning the dry run off is the moment it starts moving mail.
    document.getElementById("mail-dry").addEventListener("change", (e) => {
      if (!e.target.checked && !confirm("With the dry run off, the next sweep moves mail to the archive folder. Continue?")) e.target.checked = true;
    });
    if (m.running && !pane("mail").classList.contains("hidden")) mailTimer = setTimeout(() => mail(), 3000);
  }
  const mailBody = () => ({ enabled: document.getElementById("mail-enabled").checked, dry_run: document.getElementById("mail-dry").checked,
    every_minutes: Math.min(1440, Math.max(15, Number(document.getElementById("mail-every").value) || 60)),
    bulk_at: Number(document.getElementById("mail-bulk").value), keep_at: Number(document.getElementById("mail-keep").value) });

  /* ---------- runtime ---------- */
  let rtTab = "services";
  async function runtime() {
    let r;
    try { r = await call("/api/runtime"); }
    catch (err) { pane("runtime").innerHTML = `<div class="set-hint warn">The runtime can only be managed from the machine it runs on. ${h(err.message)}</div>`; return; }
    const services = r.services || [], procs = r.processes || [], agents = r.harness?.agents || [], tasks = [...(r.tasks || []), ...(r.recent_tasks || [])];
    const online = services.filter((s) => s.status === "running").length;
    const mb = Math.round(procs.reduce((n, p) => n + (p.memory_mb || 0), 0));
    const stat = (v, label) => `<div class="kstat"><strong>${h(v)}</strong><span>${h(label)}</span></div>`;
    const tabs = { services: "Services", processes: "Processes", harness: "Harness", tasks: "Tasks", log: "Log" };
    const body = {
      services: () => `<div class="set-card list">${services.map((s) => `<div class="item"><div class="item-main"><div class="item-title">${h(s.name)} <span class="pill ${h(s.status)}">${h(s.status)}</span></div>
          <div class="item-sub">${h(s.description)}${s.error ? ` · <span style="color:var(--danger)">${h(s.error)}</span>` : ""}</div></div>
          <div class="item-meta">${s.status === "running" ? `<button class="btn small" data-pane-act="service" data-id="${h(s.id)}" data-action="restart">Restart</button><button class="btn small" data-pane-act="service" data-id="${h(s.id)}" data-action="stop">Stop</button>`
            : `<button class="btn small" data-pane-act="service" data-id="${h(s.id)}" data-action="start">Start</button>`}</div></div>`).join("")}</div>`,
      processes: () => `<table class="data"><thead><tr><th>Process</th><th>PID</th><th>Memory</th><th>Belongs to</th><th>Status</th></tr></thead><tbody>
        ${procs.map((p) => `<tr><td>${h(p.name)}</td><td>${h(p.pid)}</td><td>${Math.round(p.memory_mb || 0)} MB</td><td>${h(p.service || p.source || "")}${p.owned ? "" : " (outside the runtime)"}</td><td>${h(p.status)}</td></tr>`).join("")}</tbody></table>`,
      harness: () => `<div class="set-hint">${h(r.harness?.validation && typeof r.harness.validation === "string" ? r.harness.validation : "")}</div><table class="data"><thead><tr><th>Profile</th><th>State</th><th>Queued</th><th>Tool rounds</th><th>Tool calls</th></tr></thead><tbody>
        ${agents.map((a) => `<tr><td>${h(a.name)}</td><td>${h(a.state)}</td><td>${h(a.queued ?? 0)}</td><td>${h(a.max_tool_rounds ?? "")}</td><td>${h(a.max_tool_calls ?? "")}</td></tr>`).join("")}</tbody></table>`,
      tasks: () => tasks.length ? `<table class="data"><thead><tr><th>Task</th><th>Service</th><th>Status</th></tr></thead><tbody>
        ${tasks.slice(0, 80).map((t) => `<tr><td>${h(t.label || t.id)}</td><td>${h(t.service)}</td><td>${h(t.status)}</td></tr>`).join("")}</tbody></table>` : `<div class="set-hint">No background tasks.</div>`,
      log: () => `<pre class="out" id="rt-log" style="max-height:420px">Loading…</pre>`,
    };
    pane("runtime").innerHTML = `<div class="set-hint top">The engine behind this window: its services, the processes it started, and what each profile may do. Data folder: <code>${h(r.data_home)}</code> · ports ${h(Object.values(r.ports || {}).join(", "))} · pid ${h(r.pid)}</div>
      <div class="kstats">${stat(`${online} of ${services.length}`, "services online")}${stat(procs.length, `processes, ${procs.filter((p) => !p.owned).length} outside`)}${stat(agents.length, "profiles in the harness")}${stat(`${mb} MB`, "memory")}</div>
      <div class="toolbar"><div class="tabs">${Object.entries(tabs).map(([k, v]) => `<button class="${rtTab === k ? "on" : ""}" data-pane-act="rt-tab" data-tab="${k}">${v}</button>`).join("")}</div>
        <span class="spacer" style="flex:1"></span><button class="btn small" data-pane-act="rt-refresh">Refresh</button></div>${body[rtTab]()}`;
    if (rtTab === "log") {
      try { document.getElementById("rt-log").textContent = (await call("/api/runtime/logs")).text || "Nothing logged."; }
      catch (err) { document.getElementById("rt-log").textContent = err.message; }
    }
  }

  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-pane-act]");
    if (!b || b.disabled) return;
    const d = b.dataset;
    try {
      switch (d.paneAct) {
        case "edit-profile": closeSettings(); openEditModal(d.id); break;
        case "new-profile": closeSettings(); openModal(); break;
        case "update-check": b.disabled = true; b.textContent = "Checking…"; updates(await call("/api/updates/check", {})); break;
        case "update-install":
          if (!confirmOnce(b, "Install and restart?")) return;
          b.disabled = true; updates(await call("/api/updates/check?install=true", {})); break;
        case "mail-save": mail(await call("/api/mail/sweep", mailBody(), "PUT")); say("Saved."); break;
        case "mail-run": { b.disabled = true; const out = await call("/api/mail/sweep/run", {}); say(out.dry_run ? "A dry run has started. Nothing will be moved." : "A sweep has started."); mail(); break; }
        case "mail-sender": await call("/api/mail/sweep/sender", { sender: d.sender, action: d.action, restore: true }); mail(); break;
        case "service": b.disabled = true; await call(`/api/runtime/services/${encodeURIComponent(d.id)}/${d.action}`, {}); runtime(); break;
        case "rt-tab": rtTab = d.tab; runtime(); break;
        case "rt-refresh": runtime(); break;
      }
    } catch (err) { say(err.message, true); b.disabled = false; }
  });

  /* ---------- the floating bar ---------- */
  async function desktop() {
    const box = document.getElementById("desktop-bar");
    try { box.checked = !!(await call("/api/desktop")).floating_bar; } catch {}
  }
  document.getElementById("desktop-bar").addEventListener("change", async (e) => {
    try {
      await call("/api/desktop", { floating_bar: e.target.checked }, "PUT");
      document.getElementById("desktop-status").textContent = e.target.checked ? "It opens the next time the desktop app starts." : "Off.";
    } catch (err) { say(err.message, true); e.target.checked = !e.target.checked; }
  });
  document.getElementById("desktop-bar-open").addEventListener("click", () =>
    window.open("/bar", "mc-bar", `popup,width=680,height=60,left=${Math.round((screen.availWidth - 680) / 2)},top=${screen.availHeight - 160}`));

  return { desktop, open(name) { ({ profiles, updates, mail, runtime })[name]?.(); } };
})();
