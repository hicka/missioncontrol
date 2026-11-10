/* Mission Control: modules.
   Add-ons the agents build: a small program with its own tools and cards. This page
   lists them, shows exactly what a version may reach before it is approved, and draws
   their cards. A card is data, never markup: a tree of typed nodes (server/module_ui.py)
   that is drawn here and nowhere run. */
const MissionModules = (() => {
  const M = { modules: [], cards: [], sandboxed: true, loaded: false, values: {} };
  const h = (v) => esc(String(v ?? ""));
  const icon = (name, cls = "") => `<svg class="i ${cls}"><use href="#i-${name}"/></svg>`;
  const deck = () => window.MissionDeck;
  const TONE = { ok: "good", warn: "warn", bad: "danger", info: "info", muted: "muted", accent: "accent" };
  const tone = (t) => (TONE[t] ? ` tone-${TONE[t]}` : "");
  // Cards name Apple's symbols. The ones with an obvious twin here are drawn; the rest are left out.
  const SYMBOLS = [[/^checkmark/, "check"], [/^exclamationmark|^xmark\.octagon/, "alert"], [/^clock|^timer|^calendar/, "clock"],
    [/^bolt/, "bolt"], [/^globe|^network|^wifi/, "globe"], [/^arrow\.(clockwise|triangle\.2)/, "refresh"], [/^play/, "play"],
    [/^chart/, "growth"], [/^gear|^slider/, "settings"], [/^person/, "users"], [/^doc|^book|^note/, "book"], [/^tray|^envelope/, "inbox"],
    [/^magnifyingglass/, "search"], [/^plus/, "plus"], [/^xmark/, "x"], [/^sparkle|^star|^wand/, "spark"], [/^shield|^lock/, "shield"],
    [/^terminal|^chevron\.left\.forward/, "term"], [/^lightbulb/, "lightbulb"], [/^bubble|^message/, "chat"], [/^pin/, "pin"],
    [/^stop/, "stop"], [/^list|^checklist/, "tasks"], [/^house/, "home"], [/^externaldrive|^server|^cylinder|^memorychip|^cpu/, "memory"]];
  const symbol = (name, cls = "") => {
    const hit = SYMBOLS.find(([rx]) => rx.test(String(name || "")));
    return hit ? icon(hit[1], cls) : "";
  };
  function ago(ts) {
    if (!ts) return "";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }
  const authorName = (id) => S.agents[id]?.name || id || "an agent";
  const needing = () => M.modules.filter((m) => ["failed", "pending"].includes(m.status)).length;

  async function load() {
    try {
      const data = await deck().api("/api/modules");
      M.modules = data.modules || []; M.cards = data.cards || []; M.sandboxed = data.sandboxed !== false; M.loaded = true;
    } catch (err) { M.error = err.message; }
    deck().modulesChanged(needing());
    update();
  }

  /* ---------- cards ---------- */
  function chart(node) {
    const values = node.values || [];
    if (!values.length) return "";
    const W = 220, H = 46;
    const lo = node.min ?? Math.min(...values, 0), hi = node.max ?? Math.max(...values, lo + 1);
    const span = hi - lo || 1;
    const y = (v) => H - 3 - ((Math.min(hi, Math.max(lo, v)) - lo) / span) * (H - 6);
    let marks;
    if (node.kind === "bar") {
      const band = W / values.length, bw = Math.max(1, Math.min(14, band * 0.7));
      marks = values.map((v, i) => `<rect x="${(i * band + (band - bw) / 2).toFixed(1)}" y="${y(v).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, H - 3 - y(v)).toFixed(1)}" rx="1.5"/>`).join("");
    } else {
      const n = Math.max(1, values.length - 1);
      marks = `<path d="${values.map((v, i) => `${i ? "L" : "M"}${(2 + (i / n) * (W - 4)).toFixed(1)} ${y(v).toFixed(1)}`).join(" ")}"/>`;
    }
    return `<figure class="mc-chart ${node.kind === "bar" ? "bar" : "line"}${tone(node.tone)}">${node.label ? `<figcaption>${h(node.label)}</figcaption>` : ""}
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${h(node.label || "chart")}">${marks}</svg></figure>`;
  }
  function draw(node, card) {
    if (!node || typeof node !== "object") return "";
    const kids = () => (node.children || []).map((c) => draw(c, card)).join("");
    switch (node.type) {
      case "stack":
        return `<div class="mc-stack ${node.axis === "h" ? "h" : "v"} align-${h(node.align || "leading")}" style="gap:${Math.min(40, Math.max(0, Number(node.spacing ?? 8)))}px">${kids()}</div>`;
      case "grid":
        return `<div class="mc-grid" style="grid-template-columns:repeat(${Math.min(6, Math.max(1, Number(node.columns || 2)))},minmax(0,1fr));gap:${Math.min(40, Math.max(0, Number(node.spacing ?? 8)))}px">${kids()}</div>`;
      case "text":
        return `<div class="mc-text ${h(node.style || "body")}${tone(node.tone)}"${node.lines ? ` style="-webkit-line-clamp:${Number(node.lines)};display:-webkit-box;-webkit-box-orient:vertical;overflow:hidden"` : ""}>${h(node.text)}</div>`;
      case "stat":
        return `<div class="mc-stat${tone(node.tone)}"><span class="label">${h(node.label)}</span>
          <span class="value">${h(node.value)}${node.unit ? `<small>${h(node.unit)}</small>` : ""}${node.trend ? `<i class="trend ${h(node.trend)}">${{ up: "↑", down: "↓", flat: "→" }[node.trend] || ""}</i>` : ""}</span>
          ${node.detail ? `<span class="detail">${h(node.detail)}</span>` : ""}</div>`;
      case "status":
        return `<div class="mc-status${tone(node.tone)}"><i></i><span>${h(node.label)}</span>${node.detail ? `<small>${h(node.detail)}</small>` : ""}</div>`;
      case "badge": return `<span class="mc-badge${tone(node.tone)}">${h(node.text)}</span>`;
      case "progress":
        return `<div class="mc-progress${tone(node.tone)}">${node.label ? `<span>${h(node.label)}</span>` : ""}<div><i style="width:${Math.round(Math.min(1, Math.max(0, Number(node.value) || 0)) * 100)}%"></i></div></div>`;
      case "chart": return chart(node);
      case "symbol": return `<span class="mc-symbol${tone(node.tone)}" style="--z:${Math.min(64, Math.max(10, Number(node.size || 18)))}px">${symbol(node.name)}</span>`;
      case "table":
        return `<table class="mc-table">${(node.columns || []).length ? `<thead><tr>${node.columns.map((c) => `<th>${h(c)}</th>`).join("")}</tr></thead>` : ""}
          <tbody>${(node.rows || []).map((r) => `<tr>${r.map((c) => `<td>${h(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      case "button":
        return `<button class="btn small ${node.style === "primary" ? "primary" : node.style === "destructive" ? "danger" : ""}" data-mod-act="card" data-action="${h(node.action)}"
          data-value="${h(JSON.stringify(node.value ?? null))}">${symbol(node.symbol)}${h(node.label)}</button>`;
      case "link":
        return /^https?:\/\//i.test(node.url || "") ? `<a class="mc-link" href="${h(node.url)}" target="_blank" rel="noopener noreferrer">${h(node.label)}</a>` : "";
      case "field": {
        const key = `${card.module}/${card.id}`;
        const kept = M.values[key]?.[node.name];
        return `<input class="deck-input mc-field" data-mod-field="${h(node.name)}" placeholder="${h(node.placeholder || "")}" value="${h(kept ?? node.value ?? "")}" aria-label="${h(node.placeholder || node.name)}">`;
      }
      case "toggle":
        return `<label class="mc-toggle"><span>${h(node.label || node.name)}</span><span class="switch"><input type="checkbox" data-mod-toggle="${h(node.name)}" data-action="${h(node.action || "")}" ${node.value ? "checked" : ""}><span></span></span></label>`;
      case "divider": return `<hr class="mc-divider">`;
      case "spacer": return `<span class="mc-spacer"></span>`;
      default: return "";
    }
  }
  function cardHtml(c, { flag = true } = {}) {
    const running = c.module_status === "running";
    return `<article class="mod-card size-${h(c.size || "medium")}${tone(c.tone)}${running ? "" : " resting"}" data-mod-card="${h(c.id)}" data-module="${h(c.module)}">
      <header>${symbol(c.symbol)}<div><strong>${h(c.title || c.module_name)}</strong>${c.subtitle ? `<small>${h(c.subtitle)}</small>` : ""}</div>
        ${running ? "" : `<span class="pill ${h(c.module_status)}">${h(c.module_status)}</span>`}</header>
      <div class="mod-card-body">${c.view ? draw(c.view, c) : `<div class="mc-text caption">Not drawn yet.</div>`}</div>
      <footer><span>${h(c.module_name || c.module)} · ${c.updated ? `updated ${h(ago(c.updated))}` : "not drawn yet"}</span>
        ${flag ? `<label title="The Mac app pins the card to the desktop. Other apps show it here."><input type="checkbox" data-mod-desktop ${c.desktop ? "checked" : ""}>On the desktop</label>` : ""}</footer></article>`;
  }

  /* ---------- the page ---------- */
  function chips(p = {}) {
    const chip = (ic, text, warn) => `<span class="mod-chip${warn ? " warn" : ""}">${icon(ic)}${h(text)}</span>`;
    return `<div class="mod-chips">${chip("globe", p.network ? "Network" : "No network", p.network)}
      ${chip("shield", (p.keys || []).length ? `Keys: ${p.keys.join(", ")}` : "No keys", (p.keys || []).length)}
      ${(p.read || []).length ? chip("book", `Reads: ${p.read.join(", ")}`, true) : ""}</div>`;
  }
  function row(m) {
    const cards = M.cards.filter((c) => c.module === m.slug);
    const acts = [
      m.status === "pending" ? `<button class="btn small primary" data-mod-act="approve" data-slug="${h(m.slug)}">Approve and run</button>`
        : ["running", "starting"].includes(m.status) ? `<button class="btn small" data-mod-act="stop" data-slug="${h(m.slug)}">Stop</button><button class="btn small" data-mod-act="restart" data-slug="${h(m.slug)}">Restart</button>`
        : `<button class="btn small" data-mod-act="start" data-slug="${h(m.slug)}">Start</button>`,
      `<button class="btn small ghost" data-mod-act="code" data-slug="${h(m.slug)}">Code</button>`,
      `<button class="btn small ghost" data-mod-act="log" data-slug="${h(m.slug)}">Log</button>`,
      `<span class="spacer"></span><button class="btn small ghost danger" data-mod-act="remove" data-slug="${h(m.slug)}">Remove…</button>`,
    ].join("");
    return `<section class="card mod-row"><div class="mod-head"><h4>${h(m.name || m.slug)}</h4><span class="pill ${h(m.status)}">${h(m.status)}</span>
        <span class="muted">v${h(m.version)} · built by ${h(authorName(m.author))}</span></div>
      ${m.description ? `<p>${h(m.description)}</p>` : ""}${chips(m.permissions)}
      ${["pending", "stopped"].includes(m.status) && m.reason ? `<div class="mod-reason">Waiting because ${h(m.reason)}.</div>` : ""}
      ${m.error ? `<pre class="out${m.status === "failed" ? " bad" : ""}">${h(m.error)}</pre>` : ""}
      ${(m.tools || []).length ? `<div class="mod-tools"><div class="muted">Tools for the agents</div>${m.tools.map((t) => `<div><code>${h(t.name)}</code><span>${h(t.description || "")}</span></div>`).join("")}</div>` : ""}
      ${cards.length ? `<div class="mod-strip">${cards.map((c) => cardHtml(c)).join("")}</div>` : ""}
      <div class="item-actions">${acts}</div></section>`;
  }
  function frame() {
    return `<div class="view-head"><div><h2>Modules</h2><p>Add-ons the agents build for you: a small program with its own tools and cards.
      Nothing runs until you have seen what it may reach and approved that exact version.</p></div>
      <div class="view-actions"><button class="btn primary" data-act="prefill" data-text="Build me a module that ">${icon("plus")}Build one…</button></div></div>
      <div id="mod-list"><div class="muted">Loading…</div></div>`;
  }
  function update() {
    const box = document.getElementById("mod-list");
    if (!box || !M.loaded) { if (box && M.error) box.innerHTML = `<div class="card"><div class="empty"><span>${h(M.error)}</span></div></div>`; return; }
    if (box.contains(document.activeElement) && document.activeElement.matches("[data-mod-field]")) return;   // someone is typing in a card
    box.innerHTML = (M.sandboxed ? "" : `<div class="set-hint warn" style="margin-bottom:14px">This machine cannot fence a module in, so a module here runs with the same reach as the app. Read the code before approving one.</div>`)
      + (M.modules.length ? M.modules.map(row).join("")
        : `<div class="card"><div class="empty"><strong>No modules yet</strong><span>Ask for one in plain words, like a card that shows the build status, and an agent writes it. You approve it here before it runs.</span></div></div>`);
  }

  async function action(slug, act, body = {}) {
    try { await deck().api(`/api/modules/${encodeURIComponent(slug)}/${act}`, body); }
    catch (err) { deck().notify(err.message, true); }
    await load();
    deck().refresh();
  }
  async function showCode(slug, approve = false) {
    let m;
    try { m = (await deck().api(`/api/modules/${encodeURIComponent(slug)}`)).module; } catch (err) { deck().notify(err.message, true); return; }
    const files = m.files || [];
    deck().inspect(`${m.name || slug}: code`, `<div class="detail-meta">version ${h(m.version)} · ${files.length} file${files.length === 1 ? "" : "s"}</div>${chips(m.permissions)}
      ${m.config && Object.keys(m.config).length ? `<h3>Settings</h3><pre>${h(JSON.stringify(m.config, null, 2))}</pre>` : ""}
      ${files.map((f) => `<h3>${h(f.path)}</h3><pre>${h(f.text)}</pre>`).join("")}
      ${approve && m.status === "pending" ? `<div class="actions"><button class="btn primary" data-mod-act="approve" data-slug="${h(slug)}" data-hash="${h(m.hash)}">Approve and run</button></div>` : ""}`);
  }
  async function showLog(slug) {
    let m;
    try { m = (await deck().api(`/api/modules/${encodeURIComponent(slug)}`)).module; } catch (err) { deck().notify(err.message, true); return; }
    const lines = (m.logs || []).map((l) => `<div class="log-line ${h(l.level)}"><time>${h(new Date(l.ts * 1000).toLocaleTimeString())}</time><span>${h(l.text)}</span></div>`).join("");
    deck().inspect(`${m.name || slug}: log`, `<div class="mod-log">${lines || `<span class="muted">Nothing logged yet.</span>`}</div>
      <div class="actions"><button class="btn" data-mod-act="log" data-slug="${h(slug)}">${icon("refresh")}Refresh</button></div>`);
  }
  // A held module, as the inbox shows it.
  function inboxItem(i, who) {
    const files = (i.files || []).length;
    return {
      sub: `<span class="who">${h(who)}</span> · ${files} file${files === 1 ? "" : "s"} · version ${h(i.version)}${i.reason ? ` · waiting because ${h(i.reason)}` : ""}
        <div style="margin-top:6px;color:var(--text-2);white-space:pre-wrap">${h(i.detail)}</div>${chips(i.permissions)}`,
      actions: `<button class="btn small" data-mod-act="code" data-approve="1" data-slug="${h(i.id)}">View code</button>
        <button class="btn small primary" data-mod-act="approve" data-slug="${h(i.id)}" data-hash="${h(i.hash)}">Approve and run</button>
        <button class="btn small" data-mod-act="decline" data-slug="${h(i.id)}">Decline</button>`,
    };
  }

  function cardOf(el) {
    const box = el.closest("[data-mod-card]");
    return box ? { el: box, slug: box.dataset.module, id: box.dataset.modCard } : null;
  }
  function fieldValues(box) {
    const values = {};
    box.querySelectorAll("[data-mod-field]").forEach((f) => { values[f.dataset.modField] = f.value; });
    box.querySelectorAll("[data-mod-toggle]").forEach((f) => { values[f.dataset.modToggle] = f.checked; });
    return values;
  }
  async function cardAction(card, act, value) {
    try {
      await deck().api(`/api/modules/${encodeURIComponent(card.slug)}/cards/${encodeURIComponent(card.id)}/action`, { action: act, value, values: fieldValues(card.el) });
    } catch (err) { deck().notify(err.message, true); }
  }
  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-mod-act]");
    if (!b || b.disabled) return;
    const d = b.dataset;
    if (d.modAct === "card") {
      const card = cardOf(b);
      let value = null;
      try { value = JSON.parse(d.value || "null"); } catch {}
      b.disabled = true;
      if (card) await cardAction(card, d.action, value);
      b.disabled = false;
    } else if (d.modAct === "code") showCode(d.slug, d.approve === "1");
    else if (d.modAct === "log") showLog(d.slug);
    else if (d.modAct === "remove") {
      if (!confirmOnce(b, "Its code, its data and its cards go. Remove?")) return;
      b.disabled = true; await action(d.slug, "remove");
    } else if (d.modAct === "approve") {
      // Approval names the exact version that was read, so a later build cannot ride on it.
      const hash = d.hash || M.modules.find((m) => m.slug === d.slug)?.hash;
      b.disabled = true; await action(d.slug, "approve", { version: hash });
      document.getElementById("inspector-close")?.click();
    } else { b.disabled = true; await action(d.slug, d.modAct); }
  });
  document.addEventListener("change", async (ev) => {
    const card = cardOf(ev.target);
    if (!card) return;
    if (ev.target.matches("[data-mod-desktop]")) {
      try { await deck().api(`/api/modules/${encodeURIComponent(card.slug)}/cards/${encodeURIComponent(card.id)}`, { desktop: ev.target.checked }, "PUT"); }
      catch (err) { deck().notify(err.message, true); ev.target.checked = !ev.target.checked; }
    } else if (ev.target.matches("[data-mod-toggle]") && ev.target.dataset.action) {
      await cardAction(card, ev.target.dataset.action, ev.target.checked);
    }
  });
  document.addEventListener("input", (ev) => {
    if (!ev.target.matches?.("[data-mod-field]")) return;
    const card = cardOf(ev.target);
    if (card) (M.values[`${card.slug}/${card.id}`] ||= {})[ev.target.dataset.modField] = ev.target.value;
  });
  window.addEventListener("mc:event", ({ detail: ev }) => {
    if (ev.type === "modules_updated" || ev.type === "module_cards") load();
    else if (ev.type === "module_card") {
      if (ev.card) {
        const i = M.cards.findIndex((c) => c.module === ev.card.module && c.id === ev.card.id);
        if (i >= 0) M.cards[i] = { ...M.cards[i], ...ev.card }; else M.cards.push(ev.card);
      } else if (ev.removed) M.cards = M.cards.filter((c) => !(c.module === ev.module && c.id === ev.id));
      update();
    }
  });
  window.addEventListener("mc:ready", load);
  window.addEventListener("mc:refresh", load);

  return { frame, update, load, inboxItem, needing };
})();
