/* Mission Control: chat frontend */
const S = {
  user: null, agents: {}, threads: [], current: null,
  msgs: [], unread: {}, typing: {}, stream: {}, eventsOpen: {}, ws: null,
  imessageAgent: null, imessageEnabled: false,
  agentState: {}, agentTool: {}, toolsOpen: {}, jobs: [], pendingFiles: [],
  hasMore: false, atLatest: true, loadingOlder: false, anchorId: null,
  searchHits: null, searchSeq: 0,
};

const $ = (sel) => document.querySelector(sel);

/* ---------- characters ----------
   Every agent is a small creature in its own colour. The shape belongs to the
   agent and never changes; the face belongs to the moment: blinking when idle,
   glancing up while it thinks, focused under a turning ring while it works,
   delighted for a few seconds when it finishes, asleep after hours of quiet. */
const SHAPES = ["round", "squircle", "pebble", "bean", "drop"];
function hashOf(s) {
  let h = 0;
  for (const ch of String(s || "")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
}
function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function colorOf(agent) {
  return /^#[0-9a-f]{6}$/i.test(agent?.color || "") ? agent.color : "#8e8e93";
}
// kept for older callers: the colour an agent shows as, readable on any theme
function accentFor(agent) { return colorOf(agent); }
function faceSVG(agent, size = 28, state = "idle") {
  const hex = colorOf(agent);
  const lum = luminance(hex);
  const shape = agent?.shape === "hex" ? "hex" : SHAPES[hashOf(agent?.id || agent?.name) % SHAPES.length];
  const cls = ["bot", `shape-${shape}`, `st-${state}`];
  // A near-black agent would vanish on the dark theme, so it takes the
  // theme's own ink instead; a merely dark one keeps its colour with light eyes.
  if (lum < 0.05) cls.push("inkless");
  else if (lum < 0.16) cls.push("dark-ink");
  if (size < 22) cls.push("tiny");
  if (size >= 40) cls.push("big");
  const blink = (hashOf(agent?.id || agent?.name) % 55) / 10;
  // set inline, so the theme's ink has to be named here too: a class cannot
  // override an inline custom property
  const body = lum < 0.05 ? "var(--bot-dark)" : hex;
  return `<span class="${cls.join(" ")}" style="--s:${size}px;--c:${body};--blink:-${blink}s" aria-hidden="true">` +
    `<span class="bot-body"></span><i class="eye l"></i><i class="eye r"></i><i class="mouth"></i></span>`;
}
function avatarSVG(agent, size = 44) { return faceSVG(agent, size); }
function teamTile(size = 28) {
  return `<span class="team-tile" style="--s:${size}px" aria-hidden="true"><svg class="i"><use href="#i-users"/></svg></span>`;
}
function groupAvatar(members, size = 44) { return teamTile(size); }
function moodOf(aid) {
  const st = S.agentState[aid] || "idle";
  if (st !== "idle") return st;
  if (S.flash?.[aid]) return "happy";
  if (S.link === false) return "sad";
  const t = S.threads.find((x) => x.id === `dm:${aid}`);
  if (t?.last?.ts && Date.now() / 1000 - t.last.ts > 24 * 3600) return "sleepy";
  return "idle";
}
function stateAvatar(aid, size = 44) { return faceSVG(S.agents[aid], size, moodOf(aid)); }
function threadAvatar(t, size = 44) {
  return t.kind === "group" ? teamTile(size) : stateAvatar(t.agent, size);
}
const STATE_LABEL = { thinking: "Thinking…", doing: "Working…" };
function activityOf(aid) {
  const st = S.agentState[aid] || "idle";
  if (st === "thinking") return "Thinking…";
  if (st === "doing") return S.agentTool[aid] ? `Working · ${S.agentTool[aid].replaceAll("_", " ")}` : "Working…";
  return "";
}
function updateHeaderStatus() {
  const t = currentThread();
  const el = $("#header-status");
  if (!el) return;
  if (t && t.kind === "dm") {
    const st = S.agentState[t.agent] || "idle";
    el.textContent = activityOf(t.agent) || S.agents[t.agent]?.blurb || "";
    el.className = `st-text st-${st}`;
    $("#stop-btn").classList.toggle("hidden", !(st === "thinking" || st === "doing"));
    $("#header-avatar").innerHTML = threadAvatar(t, 30);
  } else if (t && t.kind === "group") {
    const busy = (t.members || []).filter((a) => (S.agentState[a] || "idle") !== "idle");
    el.textContent = busy.length ? `${busy.length} working…` : `${(t.members || []).length} agents`;
    el.className = `st-text ${busy.length ? "st-doing" : ""}`;
    $("#stop-btn").classList.add("hidden");
  } else {
    el.textContent = "";
    $("#stop-btn").classList.add("hidden");
  }
}

/* ---------- text formatting ---------- */
function esc(s) {
  s = String(s ?? "");
  return s.replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}
// One line, no links: for previews that sit inside buttons.
function fmtInline(s) {
  return String(s ?? "").split(/(`[^`\n]+`)/g).map((part, i) =>
    i % 2 ? `<code>${esc(part.slice(1, -1))}</code>`
          : esc(part).replace(/(^|[\s(])#(\d{2,5})\b/g, '$1<span class="pr">#$2</span>')).join("");
}
const URL_RX = /https?:\/\/[^\s<>"]+/g;
function mdInline(s) {
  const plain = (t) => esc(t).replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])#(\d{2,5})\b/g, '$1<span class="pr">#$2</span>');
  return String(s ?? "").split(/(`[^`\n]+`)/g).map((part, i) => {
    if (i % 2) return `<code>${esc(part.slice(1, -1))}</code>`;
    // Links are found before escaping so an ampersand in a query string is
    // part of the link, and trailing punctuation is not.
    let out = "", last = 0;
    for (const m of part.matchAll(URL_RX)) {
      let url = m[0];
      const trail = url.match(/[.,;:!?)\]}'"]+$/);
      if (trail) url = url.slice(0, -trail[0].length);
      out += plain(part.slice(last, m.index)) +
        `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`;
      last = m.index + url.length;
    }
    return out + plain(part.slice(last));
  }).join("");
}
// Agents write markdown-ish text: paragraphs, lists, fenced code, a heading
// now and then. Everything is escaped first; only these few shapes come back.
function fmt(s) {
  const lines = String(s ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [], list = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(mdInline).join("<br>")}</p>`); para = []; } };
  const flushList = () => {
    if (list) { out.push(`<${list.type}>${list.items.map((x) => `<li>${mdInline(x)}</li>`).join("")}</${list.type}>`); list = null; }
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```[\w+-]*\s*$/.test(line)) {
      flushPara(); flushList();
      const body = [];
      while (++i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i]);
      out.push(`<pre><code>${esc(body.join("\n"))}</code></pre>`);
      continue;
    }
    const bullet = line.match(/^\s*[-*•]\s+(.*)$/);
    const num = line.match(/^\s*\d{1,3}[.)]\s+(.*)$/);
    const head = line.match(/^\s*#{1,4}\s+(.+)$/);
    if (bullet || num) {
      flushPara();
      const type = bullet ? "ul" : "ol";
      if (!list || list.type !== type) { flushList(); list = { type, items: [] }; }
      list.items.push((bullet || num)[1]);
    } else if (head) {
      flushPara(); flushList();
      out.push(`<span class="md-h">${mdInline(head[1])}</span>`);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else if (list && /^\s{2,}\S/.test(line)) {
      list.items[list.items.length - 1] += " " + line.trim();
    } else {
      flushList();
      para.push(line);
    }
  }
  flushPara(); flushList();
  return out.join("");
}

/* ---------- time ---------- */
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function fmtDay(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = (startOf(now) - startOf(d)) / 86400000;
  if (diff === 0) return `<b>Today</b> ${fmtTime(ts)}`;
  if (diff === 1) return `<b>Yesterday</b> ${fmtTime(ts)}`;
  return `<b>${d.toLocaleDateString([], { month: "short", day: "numeric" })}</b> ${fmtTime(ts)}`;
}
function sidebarTime(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = (startOf(now) - startOf(d)) / 86400000;
  if (diff === 0) return fmtTime(ts);
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/* ---------- sidebar ---------- */
function previewOf(t) {
  const m = t.last;
  if (!m) return t.agent ? esc(S.agents[t.agent]?.blurb || "No messages yet") : "The agent, and any profile you @mention";
  let text = m.text.replace(/\s+/g, " ").slice(0, 90);
  // a bare NO_REPLY from older history says nothing worth previewing
  if (/^\W*no[_ ]?reply\W*$/i.test(text)) return t.agent ? esc(S.agents[t.agent]?.blurb || "") : "";
  if (m.kind === "tool") text = (m.meta.tool === "terminal" ? "$ " : "") + text;
  if (isBeat(m)) return "Heartbeat";
  if (m.kind === "sys") text = `${m.sender}: ${eventSummary(m).gist}`;
  if (m.sender === "user") text = `You: ${text}`;
  else if (t.kind === "group") {
    const a = S.agents[m.sender];
    if (a) text = `${a.name.split(" ")[0]}: ${text}`;
  }
  return fmtInline(text || "Sent a file");
}
function threadOfHit(m) {
  // a2a traffic lives inside the DM views of the agents involved
  const id = m.thread.startsWith("a2a:") ? `dm:${m.sender}` : m.thread;
  return S.threads.find((t) => t.id === id) ? id : null;
}

function hitsHtml() {
  if (!S.searchHits) return "";
  if (!S.searchHits.length) {
    return `<div class="hits"><div class="hits-head">Messages</div>
      <div class="hits-empty">Nothing found</div></div>`;
  }
  const rows = S.searchHits.map((m) => {
    const id = threadOfHit(m);
    if (!id) return "";
    const t = S.threads.find((x) => x.id === id);
    // in a DM the speaker is the conversation, so naming both just repeats it
    const who = m.sender === "user" ? "you"
      : (id === `dm:${m.sender}` ? "" : (S.agents[m.sender]?.name || m.sender));
    const text = m.text.replace(/\s+/g, " ").slice(0, 120);
    return `<button class="hit-row" data-id="${esc(id)}" data-mid="${m.id}">
      <div class="hit-top">${who ? `<span class="hit-who">${esc(who)}</span>` : ""}
        <span class="hit-where">${esc(t.name)}</span>
        <span class="hit-when">${sidebarTime(m.ts)}</span></div>
      <div class="hit-text">${fmtInline(text)}</div>
    </button>`;
  }).join("");
  return `<div class="hits"><div class="hits-head">Messages</div>${rows}</div>`;
}

// The team, in roster order: where an agent sits in the list should not move
// every time it speaks. The team chat leads.
function renderSidebar() {
  const q = ($("#search").value || "").toLowerCase();
  const list = $("#convo-list");
  // One agent to talk to, and below it the specialists you can step in with directly.
  const main = Object.values(S.agents).sort((a, b) => (a.order ?? 99) - (b.order ?? 99))[0]?.id;
  const rank = (t) => t.agent === main ? -1 : t.kind === "group" ? 1000 : (S.agents[t.agent]?.order ?? 99);
  const inChat = !document.body.classList.contains("deck-mode");
  list.innerHTML = [...S.threads]
    .sort((a, b) => rank(a) - rank(b))
    .filter((t) => !q || t.name.toLowerCase().includes(q) ||
      (S.agents[t.agent]?.blurb || "").toLowerCase().includes(q))
    .map((t) => {
      const doing = t.agent ? activityOf(t.agent) : "";
      const divider = t.agent === main && !q ? `<div class="convo-divider-slot"></div>` : "";
      return `<button class="convo ${inChat && S.current === t.id ? "selected" : ""}" data-id="${esc(t.id)}">
        <span class="convo-face">${threadAvatar(t, 28)}</span>
        <span class="convo-body">
          <span class="convo-name">${esc(t.name)}</span>
          <span class="convo-preview ${doing ? "busy" : ""}">${doing ? esc(doing) : previewOf(t)}</span>
        </span>
        ${S.unread[t.id] ? '<span class="unread-dot" aria-label="unread"></span>' : ""}
      </button>${divider}`;
    })
    .join("") + hitsHtml();
  list.querySelector(".convo-divider-slot")?.insertAdjacentHTML("beforebegin", '<div class="convo-divider">Step in directly</div>');
  list.querySelector(".convo-divider-slot")?.remove();
  list.querySelectorAll(".convo").forEach((el) =>
    el.addEventListener("click", () => { document.body.classList.remove("nav-open"); openThread(el.dataset.id); }));
  list.querySelectorAll(".hit-row").forEach((el) =>
    el.addEventListener("click", () => { document.body.classList.remove("nav-open"); openThread(el.dataset.id, Number(el.dataset.mid)); }));
}

let searchTimer = null;
function onSearchInput() {
  const q = $("#search").value.trim();
  clearTimeout(searchTimer);
  if (!q) {
    S.searchHits = null;
    renderSidebar();
    return;
  }
  renderSidebar();
  searchTimer = setTimeout(async () => {
    const seq = ++S.searchSeq;
    try {
      const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
      const data = await res.json();
      if (seq !== S.searchSeq) return;        // a later keystroke already won
      S.searchHits = data.results || [];
    } catch (e) {
      if (seq !== S.searchSeq) return;
      S.searchHits = [];
    }
    renderSidebar();
  }, 180);
}

/* ---------- messages ---------- */
function currentThread() { return S.threads.find((t) => t.id === S.current); }

const isBeat = (m) => m.kind === "sys" && m.sender === "heartbeat";

function toolGroupHtml(group) {
  const key = group[0].id;
  // Claude Code runs are worth watching: open unless the owner folded them
  const open = S.toolsOpen[key] ?? group.some((g) => g.meta?.tool === "claude");
  const n = group.length;
  const chips = group.map((g) => {
    const label = g.meta.tool === "terminal" ? "$ " + esc(g.text) : esc(g.text);
    return `<div class="tool-line">${label.slice(0, 400)}</div>`;
  }).join("");
  return `<div class="tool-group ${open ? "open" : ""}" data-key="${key}">
    <button class="tool-summary" aria-expanded="${open}">
      <svg class="i"><use href="#i-bolt"/></svg>
      ${n} step${n === 1 ? "" : "s"}
      <span class="tool-chevron">&#8250;</span>
    </button>
    <div class="tool-calls">${chips}</div>
  </div>`;
}

// System events are written for the agent they wake (an approval's output, a
// liveness nudge, a watch result). The owner gets one readable line with the
// gist, and the full text behind a disclosure, newlines intact.
function eventSummary(m) {
  const text = String(m.text || "").trim();
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s);
  if (m.sender === "approval") {
    const cmd = text.match(/`([^`]+)`/);
    const ran = /and it has been run/.test(text);
    const rest = text.split(/\n\s*\n/).slice(1).join("\n\n").replace(/\n\nCarry on from here, or reply exactly NO_REPLY\.?$/, "");
    return { gist: "you approved " + (cmd ? "`" + clip(cmd[1], 60) + "`" : "a command") + (ran ? ", and it ran" : ""),
             detail: ran ? rest : "" };
  }
  if (m.sender === "watchdog") return { gist: text, detail: "" };
  if (m.sender === "liveness") {
    const ask = text.match(/The last thing the owner asked was:\s*([\s\S]*?)(?:\n\n|$)/);
    return { gist: "nudged to finish" + (ask ? ": " + clip(ask[1].trim(), 140) : " the last request"), detail: text };
  }
  const first = text.split(/\n\s*\n/)[0].replace(/\s+/g, " ");
  const gist = clip(first, 180);
  return { gist, detail: gist === text ? "" : text };
}

function eventHtml(m) {
  const { gist, detail } = eventSummary(m);
  const line = `<span class="event-src">${esc(m.sender)}</span>${fmtInline(gist)}`;
  if (!detail) return `<div class="event-line">${line}</div>`;
  const open = S.eventsOpen[m.id] ? " open" : "";
  return `<details class="event-line has-detail" data-key="${m.id}"${open}>
    <summary>${line}<span class="tool-chevron">&#8250;</span></summary>
    <pre class="event-detail">${esc(detail)}</pre>
  </details>`;
}

function headFor(sender, ts, state = "idle") {
  const agent = S.agents[sender];
  const name = agent ? agent.name : sender;
  const face = agent ? faceSVG(agent, 28, state)
    : faceSVG({ id: sender, name: sender, color: "#8e8e93" }, 28, state);
  return `<div class="line-head">${face}<span class="who">${esc(name)}</span>` +
    (ts ? `<span class="when">${fmtTime(ts)}</span>` : "") + `</div>`;
}

function renderMessages(keep) {
  const t = currentThread();
  if (!t) return;
  const el = $("#messages");
  let html = "", prev = null;
  const msgs = S.msgs;
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    if (!prev || m.ts - prev.ts > 1800) html += `<div class="day-sep">${fmtDay(m.ts)}</div>`;

    if (isBeat(m)) {
      // a run of pulses, with the tool calls they triggered, is one beating heart
      const beats = [m], tools = [];
      while (i + 1 < msgs.length && (isBeat(msgs[i + 1]) || msgs[i + 1].kind === "tool")) {
        const n = msgs[++i];
        if (n.kind === "tool") tools.push(n); else beats.push(n);
      }
      const nb = beats.length, nt = tools.length;
      html += `<div class="hb-row"><span class="hb-heart">&#9829;</span>${nb} heartbeat${nb === 1 ? "" : "s"}` +
        (nt ? ` &middot; ${nt} step${nt === 1 ? "" : "s"}` : "") + `</div>`;
      if (nt) html += toolGroupHtml(tools);
      prev = msgs[i]; continue;
    }

    if (m.kind === "tool") {
      const group = [m];
      while (i + 1 < msgs.length && msgs[i + 1].kind === "tool" &&
             msgs[i + 1].sender === m.sender && msgs[i + 1].ts - msgs[i].ts < 1800) {
        group.push(msgs[++i]);
      }
      html += toolGroupHtml(group);
      prev = group[group.length - 1]; continue;
    }

    if (m.kind === "approval") {
      const jid = m.meta.job_id;
      const job = S.jobs.find((j) => j.id === jid);
      const status = job ? job.status : "removed";
      const agentName = S.agents[m.meta.agent]?.name || m.meta.agent;
      const actions = status === "pending"
        ? `<div class="appr-actions">
             <button class="btn danger appr-btn" data-id="${jid}" data-act="delete">Reject</button>
             <button class="btn primary appr-btn" data-id="${jid}" data-act="approve">Approve</button>
           </div>`
        : `<div class="appr-resolved ${status}">${status === "removed" ? "rejected" : status}</div>`;
      html += `<div class="appr-card">
        <div class="appr-head"><svg class="i"><use href="#i-clock"/></svg>Scheduled job wants your approval</div>
        <div class="appr-name">${esc(m.meta.name || "")}</div>
        <div class="appr-meta">${esc(agentName)} &middot; ${esc(m.meta.schedule || "")}</div>
        <div class="appr-task">${esc(m.meta.task || "")}</div>
        ${job && job.monitor_cmd ? `<div class="appr-meta" style="margin-top:8px">Before every run it executes this check in your shell:</div><pre class="out cmd">${esc(job.monitor_cmd)}</pre>` : ""}
        ${actions}
      </div>`;
      prev = m; continue;
    }

    if (m.kind === "improvement") {
      const iid = m.meta.improvement_id;
      const rec = (S.improvements || []).find((x) => x.id === iid);
      const status = rec ? rec.status : "removed";
      const who = S.agents[m.meta.agent]?.name || m.meta.agent;
      const files = (m.meta.files || []).slice(0, 8);
      const actions = status === "pending"
        ? `<div class="appr-actions">
             <button class="btn ghost imp-btn" data-id="${iid}" data-act="diff">View diff</button>
             <button class="btn danger imp-btn" data-id="${iid}" data-act="reject">Reject</button>
             <button class="btn primary imp-btn" data-id="${iid}" data-act="approve">Approve &amp; restart</button>
           </div>`
        : `<div class="appr-resolved ${esc(status)}">${esc(status)}</div>`;
      html += `<div class="appr-card imp-card">
        <div class="appr-head"><svg class="i"><use href="#i-branch"/></svg>A change to Mission Control wants your approval</div>
        <div class="appr-name">${esc(m.meta.title || "")}</div>
        <div class="appr-meta">${esc(who)} &middot; ${files.length} file${files.length === 1 ? "" : "s"} &middot; +${m.meta.added || 0}/-${m.meta.removed || 0} &middot; tests green</div>
        <div class="appr-task">${fmt(m.meta.rationale || "")}</div>
        <div class="imp-files">${files.map((f) => `<code>${esc(f)}</code>`).join(" ")}</div>
        ${actions}
      </div>`;
      prev = m; continue;
    }

    if (m.kind === "sys") {
      if (m.sender !== "setup") html += eventHtml(m);
      prev = m; continue;
    }

    if (m.kind === "a2a" && t.kind === "dm") {
      const me = t.agent;
      const other = m.sender === me ? m.meta.to : m.sender;
      const otherAgent = S.agents[other] || { id: other, name: other, color: "#8e8e93", shape: "face" };
      const dir = m.sender === me ? "Messaged" : "From";
      html += `<div class="interstitial">${dir} ${faceSVG(otherAgent, 18)} <span>${esc(otherAgent.name)}</span>
        <span class="when">${fmtTime(m.ts)}</span></div>`;
      html += `<div class="row in a2a"><div class="bubble">${fmt(m.text)}</div></div>`;
      prev = m; continue;
    }

    const out = m.sender === "user";
    const newGroup = !prev || prev.sender !== m.sender || prev.kind !== m.kind || m.ts - prev.ts > 300;
    let attHtml = "";
    for (const a of (m.meta?.attachments || [])) {
      const saveAs = esc(a.name.replace(/^[0-9a-f]{8}-/, ""));
      if (a.kind === "image") {
        attHtml += `<img class="att-img" src="${esc(a.url)}" alt="${saveAs}" title="${saveAs}"
            data-att="${esc(a.url)}" data-name="${saveAs}" loading="lazy">`;
      } else {
        attHtml += `<span class="att-file" data-att="${esc(a.url)}" data-name="${saveAs}"><svg class="i"><use href="#i-clip"/></svg>${saveAs}</span>`;
      }
    }
    const head = !out && newGroup ? headFor(m.sender, m.ts) : "";
    html += `<div class="row ${out ? "out" : "in"} ${newGroup ? "first-of-group" : ""}" data-mid="${m.id}"
      ${out ? `title="${esc(fmtTime(m.ts))}"` : ""}>${head}<div class="bubble">${attHtml}${fmt(m.text)}</div></div>`;
    prev = m;
  }
  for (const [key, on] of Object.entries(S.typing)) {
    const [aid, thread] = key.split("|");
    if (!on) continue;
    const visible = thread === S.current ||
      (t.kind === "dm" && t.agent === aid && thread.startsWith("a2a:"));
    if (visible) {
      const draft = S.stream[key];
      if (draft && draft.text) {
        html += `<div class="row in streaming first-of-group">${headFor(aid, 0, S.agentState[aid] || "thinking")}<div class="bubble">${fmt(draft.text)}</div></div>`;
      } else if (draft && draft.thinking) {
        // the model's reasoning while it has not started the answer: the tail of it, dimmed
        const tail = draft.thinking.length > 600 ? "…" + draft.thinking.slice(-600) : draft.thinking;
        html += `<div class="row in streaming thought first-of-group">${headFor(aid, 0, S.agentState[aid] || "thinking")}<div class="bubble">${esc(tail)}</div></div>`;
      } else {
        html += `<div class="row in typing first-of-group">${headFor(aid, 0, S.agentState[aid] || "thinking")}<div class="bubble">
          <span class="dot"></span><span class="dot"></span><span class="dot"></span></div></div>`;
      }
      break;
    }
  }
  if (S.hasMore) {
    html = `<div class="load-older"${S.loadingOlder ? ' data-loading="1"' : ""}>` +
      (S.loadingOlder ? "Loading earlier messages…" : "Earlier messages") + `</div>` + html;
  }
  if (!msgs.length && !S.hasMore) {
    const a = t.agent ? S.agents[t.agent] : null;
    html = `<div class="empty">${a ? faceSVG(a, 56, "happy") : teamTile(56)}
      <strong>${esc(a ? a.name : "Group chat")}</strong>
      <span>${esc(a ? (a.blurb || "Ready when you are.") : "Messages here reach the agent, or any profile you @mention.")}</span></div>`;
  }
  const sc = $("#scroll");
  const before = { h: sc.scrollHeight, top: sc.scrollTop };
  el.innerHTML = html;
  el.querySelectorAll(".imp-btn").forEach((b) =>
    b.addEventListener("click", () => onImprovement(b.dataset.id, b.dataset.act, b)));
  el.querySelectorAll(".appr-btn").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      await fetch(`/api/cron/${b.dataset.id}/${b.dataset.act}`, { method: "POST" });
      await pollPendingJobs();
      renderMessages();
    }));
  el.querySelectorAll("details.event-line").forEach((d) =>
    d.addEventListener("toggle", () => { S.eventsOpen[d.dataset.key] = d.open; }));
  el.querySelectorAll(".tool-summary").forEach((b) =>
    b.addEventListener("click", () => {
      const g = b.parentElement;
      const key = g.dataset.key;
      S.toolsOpen[key] = !g.classList.contains("open");
      g.classList.toggle("open", S.toolsOpen[key]);
      b.setAttribute("aria-expanded", String(S.toolsOpen[key]));
    }));
  if (keep === "anchor" && S.anchorId != null) {
    // opened from a search hit: put that message on screen and mark it
    const hit = el.querySelector(`[data-mid="${S.anchorId}"]`);
    if (hit) {
      hit.classList.add("hit");
      hit.scrollIntoView({ block: "center" });
    } else {
      sc.scrollTop = sc.scrollHeight;
    }
  } else if (keep === "top") {
    // older messages were prepended: hold the reader where they were
    sc.scrollTop = sc.scrollHeight - before.h + before.top;
  } else {
    sc.scrollTop = sc.scrollHeight;
  }
  $("#to-latest").classList.toggle("hidden", S.atLatest);
}

async function loadOlder() {
  if (S.loadingOlder || !S.hasMore || !S.msgs.length) return;
  S.loadingOlder = true;
  const oldest = S.msgs[0];
  const id = S.current;
  renderMessages("top");
  try {
    const page = await fetchThreadPage(id, `before_ts=${oldest.ts}&before_id=${oldest.id}`);
    if (S.current !== id) return;              // the reader moved on mid-flight
    S.msgs = page.messages.concat(S.msgs);
    S.hasMore = page.has_more;
  } catch (e) {
    console.error("could not load earlier messages", e);
  } finally {
    S.loadingOlder = false;
    if (S.current === id) renderMessages("top");
  }
}

async function fetchThreadPage(id, query = "") {
  const res = await fetch(`/api/thread/${encodeURIComponent(id)}${query ? "?" + query : ""}`);
  if (!res.ok) throw new Error(`thread ${res.status}`);
  return res.json();
}

async function openThread(id, atId = null) {
  window.MissionDeck?.showChat();
  S.current = id;
  delete S.unread[id];
  const t = currentThread();
  if (!t) return;
  $("#header-avatar").innerHTML = threadAvatar(t, 30);
  $("#header-name").textContent = t.name;
  updateHeaderStatus();
  $("#edit-agent-btn").classList.toggle("hidden", t.kind !== "dm");
  $("#header-badge").classList.toggle(
    "hidden", !(t.kind === "dm" && t.agent === S.imessageAgent && S.imessageEnabled));
  $("#input").placeholder = t.kind === "dm" ? `Message ${t.name}` : "Message the agent, or @mention a profile";
  window.dispatchEvent(new CustomEvent("mc:thread", { detail: t }));
  S.anchorId = atId;
  S.loadingOlder = false;
  const page = await fetchThreadPage(id, atId ? `at_id=${atId}` : "");
  if (S.current !== id) return;
  S.msgs = page.messages;
  S.hasMore = page.has_more;
  S.atLatest = page.at_latest;
  renderSidebar();
  renderMessages(atId ? "anchor" : null);
  $("#input").focus();
}

/* ---------- events ---------- */
function belongsToCurrent(m) {
  const t = currentThread();
  if (!t) return false;
  if (m.thread === t.id) return true;
  if (t.kind === "dm" && m.kind === "a2a") {
    return m.sender === t.agent || m.meta.to === t.agent;
  }
  return false;
}
function threadForSidebar(m) {
  if (m.thread.startsWith("a2a:")) {
    return null; // a2a traffic surfaces inside DM views, previews stay on DMs
  }
  return S.threads.find((t) => t.id === m.thread);
}

/* ---------- new agent modal ---------- */
const SWATCH_COLORS = ["#7a68c9", "#e0559b", "#d6493f", "#f5a623", "#3aa655",
  "#30c8bd", "#3f7bd9", "#8e8e93", "#1d1d1f"];
let naColor = SWATCH_COLORS[0];
let naMode = "new";     // "new" | "edit"
let naEditId = null;

function naPreview() {
  const a = naEditId ? S.agents[naEditId] : null;
  $("#na-preview").innerHTML = avatarSVG({ color: naColor, shape: a?.shape || "face",
    id: naEditId || ($("#na-name").value || "").toLowerCase().replace(/[^a-z0-9]+/g, "") }, 44);
}
function renderSwatches() {
  $("#na-swatches").innerHTML = SWATCH_COLORS.map((c) =>
    `<span class="swatch ${c === naColor ? "selected" : ""}" data-c="${c}"
       style="background:${c}"></span>`).join("");
  $("#na-swatches").querySelectorAll(".swatch").forEach((el) =>
    el.addEventListener("click", () => {
      naColor = el.dataset.c;
      $("#na-swatches").querySelectorAll(".swatch").forEach((s) =>
        s.classList.toggle("selected", s === el));
      naPreview();
    }));
}
function openModal() {
  naMode = "new"; naEditId = null;
  $("#na-policy").classList.add("hidden");
  $("#modal-backdrop").classList.remove("hidden");
  $("#new-agent-modal h2").textContent = "New profile";
  $("#na-create").textContent = "Create";
  $("#na-remove").classList.add("hidden");
  $("#na-error").textContent = "";
  naColor = SWATCH_COLORS[Object.keys(S.agents).length % SWATCH_COLORS.length];
  renderSwatches(); naPreview();
  $("#na-name").value = ""; $("#na-prompt").value = ""; $("#na-cwd").value = "";
  fillModelPicker(null);
  setImsgCheckbox(false, false);
  $("#na-name").focus();
}
function openEditModal(aid) {
  const a = S.agents[aid];
  if (!a) return;
  naMode = "edit"; naEditId = aid;
  $("#na-policy").classList.remove("hidden");
  $("#na-policy").open = false;
  $("#na-rounds").value = a.max_tool_rounds || 40;
  $("#na-calls").value = a.max_tool_calls || 160;
  $("#na-tools").value = a.tools === null || a.tools === undefined ? "" : a.tools.join(", ") || "none";
  $("#na-skills").value = a.skills === null || a.skills === undefined ? "" : a.skills.join(", ") || "none";
  $("#modal-backdrop").classList.remove("hidden");
  $("#new-agent-modal h2").textContent = "Edit profile";
  $("#na-create").textContent = "Save";
  const rm = $("#na-remove");
  rm.classList.remove("hidden", "confirm");
  rm.textContent = "Remove profile";
  $("#na-error").textContent = "";
  naColor = a.color;
  renderSwatches(); naPreview();
  $("#na-name").value = a.name;
  $("#na-prompt").value = a.prompt || "";
  $("#na-cwd").value = a.cwd && a.cwd !== "~" ? a.cwd : "";
  fillModelPicker(a.llm);
  const isDefault = S.imessageAgent === aid;
  setImsgCheckbox(isDefault, isDefault);
  $("#na-name").focus();
}
async function fillModelPicker(override) {
  const sel = $("#na-provider"), input = $("#na-model");
  const full = SET.full || await fetchFull().catch(() => null);
  const presets = (full && full.llm && full.llm.presets) || {};
  const teamModel = (full && full.llm && full.llm.model) || "the default model";
  sel.innerHTML = `<option value="">Default (${esc(teamModel)})</option>` +
    Object.entries(presets)
      .map(([k, p]) => `<option value="${esc(k)}">${esc(p.label)}</option>`).join("");
  sel.value = (override && override.provider) || "";
  input.value = (override && override.model) || "";
  syncModelPicker();
  if (sel.value) refreshModels("#na-model-options", sel.value, "");
}

function syncModelPicker() {
  const sel = $("#na-provider"), input = $("#na-model"), hint = $("#na-model-hint");
  const full = SET.full, presets = (full && full.llm && full.llm.presets) || {};
  const preset = presets[sel.value];
  input.disabled = !sel.value;
  input.placeholder = sel.value ? "model" : "uses the default model";
  if (!sel.value) {
    hint.className = "na-hint";
    hint.textContent = "";
    return;
  }
  // a provider whose key is missing would fail on this agent's first turn
  const needsKey = preset && preset.key_env && !(full.llm.key_env === preset.key_env && full.llm.key_set);
  hint.className = needsKey ? "na-hint warn" : "na-hint";
  hint.textContent = needsKey
    ? `${preset.label} needs ${preset.key_env} in Settings > Model before this agent can use it.`
    : "This profile thinks with this model; the others are unaffected.";
}

function setImsgCheckbox(checked, locked) {
  const box = $("#na-imsg"), row = $("#na-imsg-row");
  box.checked = checked;
  box.disabled = locked;
  row.classList.toggle("locked", locked);
  row.title = locked
    ? "This agent is the iMessage responder. Check this box on another agent to move it."
    : "Route texts from your phone to this agent";
}
function closeModal() { $("#modal-backdrop").classList.add("hidden"); }

async function submitModal() {
  const name = $("#na-name").value.trim();
  const prompt = $("#na-prompt").value.trim();
  if (!name || !prompt) {
    $("#na-error").textContent = "Name and role description are both required.";
    return;
  }
  $("#na-create").disabled = true;
  try {
    const provider = $("#na-provider").value;
    const payload = { name, prompt, color: naColor, cwd: $("#na-cwd").value.trim() || "~",
                      llm: provider ? { provider, model: $("#na-model").value.trim() } : {} };
    if (provider && !payload.llm.model) {
      $("#na-error").textContent = "Pick a model for that provider, or go back to the default.";
      return;
    }
    let res, data, aid;
    if (naMode === "new") {
      res = await fetch("/api/agents", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload) });
      data = await res.json();
      if (!data.ok) { $("#na-error").textContent = data.error || "Failed."; return; }
      aid = data.agent.id;
    } else {
      aid = naEditId;
      const allowlist = (selector) => {
        const raw = $(selector).value.trim();
        return !raw ? null : raw.toLowerCase() === "none" ? [] : raw.split(",").map(s => s.trim()).filter(Boolean);
      };
      payload.tools = allowlist("#na-tools");
      payload.skills = allowlist("#na-skills");
      payload.max_tool_rounds = Number($("#na-rounds").value);
      payload.max_tool_calls = Number($("#na-calls").value);
      res = await fetch(`/api/agents/${aid}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload) });
      data = await res.json();
      if (!data.ok) { $("#na-error").textContent = data.error || "Failed."; return; }
    }
    if ($("#na-imsg").checked && !$("#na-imsg").disabled) {
      await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ imessage_default_agent: aid }) });
      S.imessageAgent = aid;
    }
    closeModal();
    await refreshState();
    openThread(`dm:${aid}`);
  } finally {
    $("#na-create").disabled = false;
  }
}

async function removeAgent() {
  const rm = $("#na-remove");
  if (!rm.classList.contains("confirm")) {
    rm.classList.add("confirm");
    rm.textContent = "Really remove?";
    return;
  }
  const res = await fetch(`/api/agents/${naEditId}`, { method: "DELETE" });
  const data = await res.json();
  if (!data.ok) { $("#na-error").textContent = data.error || "Failed."; return; }
  closeModal();
  await refreshState();
  const first = [...S.threads].sort((a, b) => (b.last?.ts || 0) - (a.last?.ts || 0))[0];
  if (first) openThread(first.id);
}

async function refreshState() {
  const [stateRes, agentRes] = await Promise.all([
    fetch("/api/state"), fetch("/api/agents-full").catch(() => null),
  ]);
  const data = await stateRes.json();
  S.agents = {};
  for (const a of data.agents) S.agents[a.id] = a;
  if (agentRes && agentRes.ok) {
    const full = await agentRes.json();
    for (const a of full.agents || []) {
      if (S.agents[a.id]) Object.assign(S.agents[a.id], a);
    }
  }
  S.threads = data.threads;
  const groupT = S.threads.find((t) => t.id === "group:team");
  if (groupT) groupT.members = data.agents.map((a) => a.id);
  const settings = await (await fetch("/api/settings")).json();
  S.imessageAgent = settings.imessage_default_agent;
  S.imessageEnabled = settings.imessage_enabled;
  try {
    const st = await (await fetch("/api/agents-status")).json();
    S.agentState = st.agents || {};
  } catch {}
  renderSidebar();
}

let streamRenderQueued = false;
function scheduleStreamRender() {
  // deltas arrive several times a second; one repaint per frame is plenty
  if (streamRenderQueued) return;
  streamRenderQueued = true;
  requestAnimationFrame(() => { streamRenderQueued = false; renderMessages(); });
}

function onEvent(ev) {
  window.dispatchEvent(new CustomEvent("mc:event", {detail: ev}));
  if (ev.type === "improvements_updated") {
    renderImprovements().then(renderMessages).catch(() => {});
    return;
  }
  if (ev.type === "agent_added") {
    if (!S.agents[ev.agent.id]) refreshState();
    return;
  }
  if (ev.type === "agent_updated" || ev.type === "agent_removed" || ev.type === "settings") {
    refreshState().then(() => {
      if (ev.type === "agent_removed" && S.current === `dm:${ev.agent_id}`) {
        const first = [...S.threads].sort((a, b) => (b.last?.ts || 0) - (a.last?.ts || 0))[0];
        if (first) openThread(first.id);
      } else if (S.current) {
        const t = currentThread();
        if (t) {
          $("#header-avatar").innerHTML = threadAvatar(t, 30);
          $("#header-name").textContent = t.name;
          updateHeaderStatus();
          $("#header-badge").classList.toggle(
            "hidden", !(t.kind === "dm" && t.agent === S.imessageAgent && S.imessageEnabled));
        }
      }
    });
    return;
  }
  if (ev.type === "stream") {
    // the model's reply as it is written; the real message replaces it
    const key = `${ev.agent}|${ev.thread}`;
    if (ev.reset) delete S.stream[key];
    else {
      const d = S.stream[key] || (S.stream[key] = { text: "", thinking: "" });
      if (ev.thinking) d.thinking += ev.thinking;
      if (ev.text) d.text += ev.text;
    }
    if (S.atLatest) scheduleStreamRender();
    return;
  }
  if (ev.type === "message") {
    const m = ev.msg;
    if (m.kind !== "tool") {
      for (const key of Object.keys(S.stream)) if (key.startsWith(`${m.sender}|`)) delete S.stream[key];
    }
    const st = threadForSidebar(m);
    if (st) {
      st.last = m;
      if (m.thread !== S.current && m.sender !== "user") S.unread[m.thread] = true;
    } else if (m.kind === "a2a") {
      for (const aid of [m.sender, m.meta.to]) {
        const dt = S.threads.find((t) => t.id === `dm:${aid}`);
        if (dt) dt.last = m;
      }
      const t = currentThread();
      if (!(t && t.kind === "dm" && (t.agent === m.sender || t.agent === m.meta.to)))
        S.unread[`dm:${m.sender}`] = true;
    }
    if (belongsToCurrent(m)) {
      // a thread opened at a search hit is a view of the past; a new message
      // must not silently graft itself onto the end of it
      if (S.atLatest) {
        S.msgs.push(m);
        renderMessages();
      } else {
        $("#to-latest").classList.add("nudge");
      }
      voiceOnMessage(m);
    }
    renderSidebar();
  } else if (ev.type === "typing") {
    S.typing[`${ev.agent}|${ev.thread}`] = ev.on;
    if (!ev.on) delete S.stream[`${ev.agent}|${ev.thread}`];
    if (S.atLatest) renderMessages();
  } else if (ev.type === "execution") {
    // what each agent is doing right now, for the sidebar and the header
    const e = ev.execution;
    if (e && e.kind === "tool") {
      if (e.status === "running") S.agentTool[e.agent] = e.name;
      else if (S.agentTool[e.agent] === e.name) delete S.agentTool[e.agent];
      renderSidebar();
      updateHeaderStatus();
    }
  } else if (ev.type === "agent_state") {
    if (ev.state === "idle") delete S.agentTool[ev.agent];
    // finishing a task earns a moment of joy
    const was = S.agentState[ev.agent] || "idle";
    S.agentState[ev.agent] = ev.state;
    if (was !== "idle" && ev.state === "idle") {
      S.flash = { ...(S.flash || {}), [ev.agent]: true };
      setTimeout(() => { delete S.flash[ev.agent]; renderSidebar(); updateHeaderStatus(); }, 4000);
    }
    renderSidebar();
    updateHeaderStatus();
  } else if (ev.type === "user") {
    S.user = ev.user; updateProfile();
  } else if (ev.type === "approvals_updated") {
    renderJobsPane();
  } else if (ev.type === "cron_updated") {
    pollPendingJobs().then(renderMessages);
    if (!$("#settings-backdrop").classList.contains("hidden")) renderJobsPane();
  } else if (ev.type === "watch_updated") {
    if (!$("#settings-backdrop").classList.contains("hidden")) renderJobsPane();
  }
}

function updateProfile() {
  if (!S.user) return;
  $("#profile-badge").textContent = S.user.initials;
  $("#profile-name").textContent = S.user.name;
}

function setLink(on) {
  S.link = on;
  const el = $("#link-dot");
  el.textContent = on ? "Connected" : "Reconnecting…";
  el.classList.toggle("on", on);
  renderSidebar();
  updateHeaderStatus();
  window.dispatchEvent(new CustomEvent("mc:connection", {detail: on}));
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  S.ws = new WebSocket(`${proto}://${location.host}/ws`);
  S.ws.onopen = async () => {
    setLink(true);
    try {
      await refreshState();
      if (S.current && S.atLatest) {
        const id = S.current;
        const page = await fetchThreadPage(id);
        if (id === S.current) { S.msgs = page.messages; renderMessages("keep"); }
      }
      window.dispatchEvent(new Event("mc:refresh"));
    } catch (error) { window.MissionDeck?.notify(error.message, true); }
  };
  S.ws.onmessage = (e) => onEvent(JSON.parse(e.data));
  S.ws.onclose = () => { setLink(false); setTimeout(connectWS, 1500); };
}

/* ---------- composer ---------- */
function renderPendingFiles() {
  const wrap = $("#pending-files");
  wrap.classList.toggle("hidden", !S.pendingFiles.length);
  wrap.innerHTML = S.pendingFiles.map((f, i) =>
    `<span class="pf-chip ${f.uploading ? "uploading" : ""}">${esc(f.name)}
       ${f.uploading ? "…" : `<button class="pf-x" data-i="${i}">&times;</button>`}</span>`).join("");
  wrap.querySelectorAll(".pf-x").forEach((b) => b.addEventListener("click", () => {
    S.pendingFiles.splice(+b.dataset.i, 1);
    renderPendingFiles();
  }));
}

async function attachFiles(fileList) {
  for (const file of fileList) {
    const entry = { name: file.name, uploading: true };
    S.pendingFiles.push(entry);
    renderPendingFiles();
    const form = new FormData();
    form.append("file", file);
    try {
      const r = await fetch("/api/upload", { method: "POST", body: form });
      const d = await r.json();
      if (d.ok) Object.assign(entry, d.attachment, { uploading: false });
      else { S.pendingFiles = S.pendingFiles.filter((f) => f !== entry); }
    } catch {
      S.pendingFiles = S.pendingFiles.filter((f) => f !== entry);
    }
    renderPendingFiles();
    $("#send-btn").classList.toggle("hidden",
      !$("#input").value.trim() && !S.pendingFiles.length);
  }
}

async function send() {
  const input = $("#input");
  const text = input.value.trim();
  const atts = S.pendingFiles.filter((f) => !f.uploading);
  if ((!text && !atts.length) || !S.current) return;
  if (S.pendingFiles.some((f) => f.uploading)) return;
  if (S.sending) return;
  S.sending = true;
  $("#send-btn").disabled = true;
  try {
    const res = await fetch("/api/send", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread: S.current, text, attachments: atts }),
    });
    const result = await res.json();
    if (!res.ok || !result.ok) throw new Error(result.error || "Message was not accepted.");
    if (input.value.trim() === text) { input.value = ""; input.style.height = "auto"; }
    S.pendingFiles = S.pendingFiles.filter((f) => !atts.includes(f));
    renderPendingFiles();
    $("#send-btn").classList.toggle("hidden", !input.value.trim());
  } catch (err) {
    window.MissionDeck?.notify(err.message, true);
  } finally {
    S.sending = false;
    $("#send-btn").disabled = false;
  }
}

/* ---------- init ---------- */
async function init() {
  const res = await fetch("/api/state");
  const data = await res.json();
  S.user = data.user;
  await refreshState();
  updateProfile();
  $("#search").addEventListener("input", onSearchInput);
  // reaching the top of the transcript pulls in the page before it
  $("#scroll").addEventListener("scroll", () => {
    if ($("#scroll").scrollTop < 160) loadOlder();
  }, { passive: true });
  $("#messages").addEventListener("click", (e) => {
    if (e.target.closest(".load-older")) loadOlder();
  });
  $("#to-latest").addEventListener("click", () => {
    $("#to-latest").classList.remove("nudge");
    if (S.current) openThread(S.current);
  });
  document.querySelector(".new-chat").addEventListener("click", openModal);
  $("#na-cancel").addEventListener("click", closeModal);
  $("#na-name").addEventListener("input", () => { if (naMode === "new") naPreview(); });
  $("#na-provider").addEventListener("change", async () => {
    const provider = $("#na-provider").value;
    syncModelPicker();
    if (!provider) { $("#na-model").value = ""; return; }
    const models = await refreshModels("#na-model-options", provider, "");
    // a provider that advertises its models can fill the box in for you
    if (!$("#na-model").value && models.length) $("#na-model").value = models[0];
  });
  $("#na-create").addEventListener("click", submitModal);
  $("#na-remove").addEventListener("click", removeAgent);
  $("#edit-agent-btn").addEventListener("click", () => {
    const t = currentThread();
    if (t && t.kind === "dm") openEditModal(t.agent);
  });
  $("#modal-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "modal-backdrop") closeModal();
  });

  // attachment dropdown: click a file or image, pick View or Download
  const attMenu = document.createElement("div");
  attMenu.id = "att-menu";
  attMenu.className = "hidden";
  attMenu.innerHTML = `<button data-act="view">View</button>
    <button data-act="download">Download</button>`;
  document.body.appendChild(attMenu);
  let attFile = null;
  document.addEventListener("click", (e) => {
    const act = e.target.closest("#att-menu button");
    if (act && attFile) {
      if (act.dataset.act === "view") {
        window.open(attFile.url, "_blank");
      } else {
        const link = document.createElement("a");
        link.href = attFile.url;
        link.download = attFile.name;
        document.body.appendChild(link);
        link.click();
        link.remove();
      }
      attMenu.classList.add("hidden");
      return;
    }
    const t = e.target.closest("[data-att]");
    if (!t) { attMenu.classList.add("hidden"); return; }
    attFile = { url: t.dataset.att, name: t.dataset.name };
    attMenu.classList.remove("hidden");
    const r = t.getBoundingClientRect();
    const below = r.bottom + 4 + attMenu.offsetHeight <= innerHeight - 10;
    attMenu.style.left =
      `${Math.max(10, Math.min(r.left, innerWidth - attMenu.offsetWidth - 10))}px`;
    attMenu.style.top =
      `${below ? r.bottom + 4 : Math.max(10, r.top - attMenu.offsetHeight - 4)}px`;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });
  const input = $("#input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 130) + "px";
    $("#send-btn").classList.toggle("hidden", !input.value.trim());
  });
  $("#send-btn").addEventListener("click", send);
  $("#attach-btn").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", (e) => {
    attachFiles([...e.target.files]);
    e.target.value = "";
  });
  $("#scroll").addEventListener("dragover", (e) => e.preventDefault());
  $("#scroll").addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer?.files?.length) attachFiles([...e.dataTransfer.files]);
  });
  connectWS();
  renderSidebar();
  if (location.hash === "#new") openModal();
  const first = [...S.threads].sort((a, b) => (b.last?.ts || 0) - (a.last?.ts || 0))[0];
  if (first) await openThread(first.id);
  window.MissionDeck?.show("home");
  window.dispatchEvent(new Event("mc:ready"));
}
init().catch((error) => window.MissionDeck?.notify(`Unable to load workspace: ${error.message}`, true));

/* ---------- Settings ---------- */
const SET = { full: null, chOpen: {} };

const CHANNEL_META = {
  imessage: {
    label: "iMessage", icon: "&#128172;",
    hint: "Needs a BlueBubbles server on a Mac. Its password is the secret below.",
    fields: [["server_url", "BlueBubbles server"]], secret: "BlueBubbles password",
    senders: "Your phone or email as iMessage sees it",
  },
  telegram: {
    label: "Telegram", icon: "&#9992;&#65039;",
    hint: "Create a bot with @BotFather and paste its token. Works everywhere.",
    fields: [], secret: "Bot token",
    senders: "Your numeric id or @username",
  },
  whatsapp: {
    label: "WhatsApp", icon: "&#128222;",
    hint: "Meta Business Cloud API. Needs a stable public webhook URL.",
    fields: [["phone_number_id", "Phone number id"], ["verify_token", "Verify token"]],
    secret: "Access token",
    senders: "Phone numbers, digits only",
  },
  email: {
    label: "Email", icon: "&#9993;&#65039;",
    hint: "A dedicated address the agent owns. Use an app password; unseen mail from allowed senders routes to the agent, replies go back on the same thread.",
    fields: [["address", "Email address"], ["imap_host", "IMAP host"], ["smtp_host", "SMTP host"]],
    secret: "App password",
    senders: "Email addresses allowed to write in",
  },
};

async function fetchFull() {
  SET.full = await (await fetch("/api/settings/full")).json();
  return SET.full;
}

async function openSettings(tab) {
  // The learning engine has a page of its own; the native Runtime Manager
  // still asks for it as a settings tab.
  if (tab === "evolution") { window.MissionDeck?.show("evolution"); return; }
  await fetchFull();
  renderLLMPane();
  renderMemoryPane();
  renderChannelsPane();
  renderGithubPane();
  renderFleetPane();
  renderPortsPane();
  renderJobsPane();
  renderHeartbeat();
  renderBrief();
  renderHooks();
  renderMcp();
  renderWhatsApp();
  renderRemotePane();
  renderCloudPane();
  if (tab) switchTab(tab);
  $("#settings-backdrop").classList.remove("hidden");
}

function closeSettings() { $("#settings-backdrop").classList.add("hidden"); }

function switchTab(name) {
  if (name === "browser") window.BrowserPanel?.refresh();
  if (name === "improve") renderImprovements();
  if (name === "apple") renderApple();
  document.querySelectorAll(".set-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".set-pane").forEach((p) =>
    p.classList.toggle("hidden", p.id !== `pane-${name}`));
}

/* ----- Model pane ----- */
function renderLLMPane() {
  const llm = SET.full.llm;
  $("#owner-name").value = (S.user && S.user.name) || "";
  if (!SET.ownerWired) {
    SET.ownerWired = true;
    $("#owner-save").addEventListener("click", async () => {
      const name = $("#owner-name").value.trim();
      if (!name) { setStatus("#owner-status", "Enter a name", "err"); return; }
      const d = await (await fetch("/api/user", { method: "PUT",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) })).json();
      setStatus("#owner-status", d.ok ? "Saved" : "Error", d.ok ? "ok" : "err");
      if (d.ok) { S.user = d.user; updateProfile(); }
    });
  }
  const sel = $("#llm-provider");
  sel.innerHTML = Object.entries(llm.presets)
    .map(([k, p]) => `<option value="${k}">${p.label}</option>`).join("");
  sel.value = llm.provider;
  $("#llm-model").value = llm.model || "";
  $("#llm-effort").value = llm.effort || "low";
  $("#llm-baseurl").value = llm.base_url || "";
  $("#llm-key").value = "";
  $("#llm-fallbacks").value = (llm.fallbacks || [])
    .map((f) => `${f.provider} ${f.model}`).join("\n");
  syncLLMFields();
  refreshModels("#llm-model-options");
  renderUsage();
  renderVoicePane();
  renderBudget();
}

async function renderUsage() {
  try {
    const d = await (await fetch("/api/usage")).json();
    if (!d.per_agent.length) {
      $("#usage-table").textContent = "No model calls recorded yet.";
      return;
    }
    const fmtK = (n) => n > 9999 ? `${Math.round(n / 1000)}k` : `${n}`;
    const rows = d.per_agent.map((r) =>
      `<tr><td>${esc(r.agent)}</td><td>${fmtK(r.in_tokens)}</td>` +
      `<td>${fmtK(r.out_tokens)}</td><td>${Number(r.calls)}</td></tr>`).join("");
    $("#usage-table").innerHTML =
      `<table class="usage"><tr><th>agent</th><th>in</th><th>out</th><th>calls</th></tr>${rows}</table>`;
  } catch { $("#usage-table").textContent = "Usage unavailable."; }
}

function syncLLMFields() {
  const provider = $("#llm-provider").value;
  const p = SET.full.llm.presets[provider] || {};
  const local = provider === "ollama" || provider === "lmstudio";
  const custom = !p.base_url;
  $("#llm-baseurl-row").style.display = (custom || local) ? "" : "none";
  $("#llm-baseurl").placeholder = p.base_url || "https://host/v1";
  $("#llm-key-row").style.display = p.key_env ? "" : "none";
  const saved = SET.full.llm.key_set && provider === SET.full.llm.provider;
  $("#llm-key").placeholder = saved ? "saved" : "paste key";
  $("#llm-hint").textContent = local
    ? "Runs on your machine. Private, free, no key needed."
    : (p.key_env ? "The key is stored in your local .env, never synced anywhere."
                 : "Point this at any compatible endpoint.");
  setStatus("#llm-status", "", "");
}

async function refreshModels(datalistSel, provider, base) {
  provider = provider || $("#llm-provider").value;
  base = base !== undefined ? base : $("#llm-baseurl").value.trim();
  try {
    const r = await fetch(`/api/settings/llm/models?provider=${encodeURIComponent(provider)}&base_url=${encodeURIComponent(base)}`);
    const models = (await r.json()).models || [];
    document.querySelector(datalistSel).innerHTML =
      models.map((m) => `<option value="${m}">`).join("");
    return models;
  } catch { return []; }
}

function setStatus(sel, text, cls) {
  const el = document.querySelector(sel);
  el.textContent = text;
  el.className = el.className.replace(/ (ok|err|busy)/g, "") + (cls ? ` ${cls}` : "");
}

function llmBody() {
  const fallbacks = $("#llm-fallbacks").value.split("\n").map((l) => {
    const [provider, model, base_url] = l.trim().split(/\s+/);
    return provider && model ? { provider, model, base_url: base_url || "" } : null;
  }).filter(Boolean);
  return { provider: $("#llm-provider").value, model: $("#llm-model").value.trim(),
           effort: $("#llm-effort").value,
           base_url: $("#llm-baseurl").value.trim(), max_tokens: SET.full.llm.max_tokens || 3000,
           api_key: $("#llm-key").value.trim() || null, fallbacks };
}

async function testLLM() {
  setStatus("#llm-status", "Testing…", "busy");
  const r = await fetch("/api/settings/llm/test", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(llmBody()) });
  const d = await r.json();
  setStatus("#llm-status", d.ok ? "Connection OK" : `Failed: ${d.error}`, d.ok ? "ok" : "err");
}

async function saveLLM() {
  const r = await fetch("/api/settings/llm", { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(llmBody()) });
  const d = await r.json();
  setStatus("#llm-status", d.ok ? "Saved" : `Error: ${d.error}`, d.ok ? "ok" : "err");
  if (d.ok) await fetchFull();
}

/* ----- Channels pane ----- */
function renderChannelsPane() {
  const wrap = $("#channel-cards");
  const { settings, status } = SET.full.channels;
  const agentOpts = (sel) => SET.full.agents
    .map((a) => `<option value="${a}" ${a === sel ? "selected" : ""}>${S.agents[a]?.name || a}</option>`).join("");
  wrap.innerHTML = Object.entries(CHANNEL_META).map(([name, meta]) => {
    const conf = settings[name] || {}, st = status[name] || {};
    const open = SET.chOpen[name] ?? conf.enabled;
    const dot = st.connected ? "on" : (conf.enabled ? "warn" : "off");
    const pill = st.connected ? "connected" : (conf.enabled ? (st.detail || "not connected") : "off");
    const fields = meta.fields.map(([f, label]) => `
      <div class="set-row">
        <div class="set-row-label">${label}</div>
        <input class="ch-field set-control" data-ch="${name}" data-f="${f}" type="text"
               value="${conf[f] || ""}" spellcheck="false">
      </div>`).join("");
    return `<div class="set-card ch-card ${open ? "" : "collapsed"}" data-ch="${name}">
      <div class="ch-head" data-ch="${name}">
        <span class="ch-dot ${dot}"></span>
        <strong>${meta.label}</strong>
        <span class="ch-pill ${dot}">${pill}</span>
        <span class="ch-chevron">&#8250;</span>
        <label class="switch" title="Enable ${meta.label}">
          <input type="checkbox" class="ch-enabled" data-ch="${name}" ${conf.enabled ? "checked" : ""}><span></span>
        </label>
      </div>
      <div class="ch-body">
        <div class="set-hint top">${meta.hint}</div>
        ${fields}
        <div class="set-row">
          <div class="set-row-label">${meta.secret}</div>
          <input class="ch-secret set-control" data-ch="${name}" type="password"
                 placeholder="${st.secret_set ? "saved" : "paste"}" autocomplete="off">
        </div>
        <div class="set-row">
          <div class="set-row-label" title="${meta.senders}">Allowed senders</div>
          <input class="ch-senders set-control" data-ch="${name}" type="text"
                 value="${(conf.allowed_senders || []).join(", ")}"
                 placeholder="${meta.senders}" spellcheck="false">
        </div>
        <div class="set-row">
          <div class="set-row-label">Answered by</div>
          <select class="ch-agent set-control" data-ch="${name}">${agentOpts(conf.default_agent)}</select>
        </div>
        <div class="set-footer">
          <span class="set-status" id="ch-status-${name}"></span>
          <button class="btn primary ch-save" data-ch="${name}">Save</button>
        </div>
      </div>
    </div>`;
  }).join("");
  $("#channels-public-url").textContent = SET.full.public_url
    ? `Public webhook base: ${SET.full.public_url}` : "";
  renderDevices();
  wrap.querySelectorAll(".ch-save").forEach((b) =>
    b.addEventListener("click", () => saveChannel(b.dataset.ch)));
  wrap.querySelectorAll(".ch-head").forEach((h) => h.addEventListener("click", (e) => {
    if (e.target.closest(".switch")) return;
    const name = h.dataset.ch;
    SET.chOpen[name] = h.parentElement.classList.contains("collapsed");
    h.parentElement.classList.toggle("collapsed");
  }));
  wrap.querySelectorAll(".ch-enabled").forEach((t) => t.addEventListener("change", () => {
    const card = t.closest(".ch-card");
    if (t.checked) { card.classList.remove("collapsed"); SET.chOpen[t.dataset.ch] = true; }
  }));
}

async function saveChannel(name) {
  const q = (sel) => document.querySelector(`${sel}[data-ch="${name}"]`);
  const body = {
    enabled: q(".ch-enabled").checked,
    default_agent: q(".ch-agent").value,
    allowed_senders: q(".ch-senders").value.split(",").map((s) => s.trim()).filter(Boolean),
  };
  document.querySelectorAll(`.ch-field[data-ch="${name}"]`).forEach((el) => { body[el.dataset.f] = el.value.trim(); });
  const secret = q(".ch-secret").value.trim();
  if (secret) body.secret = secret;
  setStatus(`#ch-status-${name}`, "Saving…", "busy");
  const r = await fetch(`/api/settings/channels/${name}`, { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  if (d.ok) {
    SET.full.channels.status = d.status;
    SET.full.channels.settings[name] = { ...SET.full.channels.settings[name], ...body };
    const chSt = d.status[name] || {};
    const head = document.querySelector(`.ch-head[data-ch="${name}"]`);
    const dot = chSt.connected ? "on" : (body.enabled ? "warn" : "off");
    head.querySelector(".ch-dot").className = `ch-dot ${dot}`;
    const pill = head.querySelector(".ch-pill");
    pill.className = `ch-pill ${dot}`;
    pill.textContent = chSt.connected ? "connected" : (body.enabled ? (chSt.detail || "not connected") : "off");
    setStatus(`#ch-status-${name}`, chSt.connected ? "Saved, connected" :
      (body.enabled ? `Saved. ${chSt.detail || "Not connected yet"}` : "Saved"),
      chSt.connected || !body.enabled ? "ok" : "err");
    refreshState();
  } else {
    setStatus(`#ch-status-${name}`, `Error: ${d.error}`, "err");
  }
}

/* ----- GitHub pane ----- */
function renderFleetPane() {
  const f = SET.full.fleet || {};
  $("#fleet-node").value = f.node_name || "";
  $("#fleet-key").value = "";
  $("#fleet-key").placeholder = f.key_set ? "saved" : "same key on every machine";
  $("#fleet-peers").value = (f.peers || []).map((p) => `${p.name} ${p.url}`).join("\n");
  $("#fleet-status").innerHTML = (f.peers || []).length
    ? (f.peers || []).map((p) => {
        const ok = p.ok === true, dot = ok ? "on" : (p.ok === false ? "warn" : "off");
        const ago = p.last_sync ? `synced ${Math.max(1, Math.round((Date.now()/1000 - p.last_sync)/60))}m ago` : (p.detail || "not seen yet");
        return `<span class="ch-dot ${dot}" style="display:inline-block;margin-right:5px"></span>${p.name}: ${ok ? ago : (p.detail || "unreachable")}`;
      }).join("<br>")
    : "";
  renderFleetPairing();
}

async function renderTsStatus() {
  try {
    const s = await (await fetch("/api/fleet/tailscale", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "status" }) })).json();
    $("#ts-status").textContent = !s.installed ? "not installed"
      : s.running ? `up: ${s.host}` : "installed, not signed in";
    $("#ts-setup").textContent = !s.installed ? "Install" : s.running ? "Signed in" : "Sign in";
    $("#ts-setup").disabled = s.installed && s.running;
  } catch { $("#ts-status").textContent = "unavailable"; }
}

function renderFleetPairing() {
  renderTsStatus();
  if (SET.fleetPairWired) return;
  SET.fleetPairWired = true;

  $("#ts-setup").addEventListener("click", async () => {
    setStatus("#ts-note", "Setting up Tailscale… this can take a minute.", "busy");
    let s = await (await fetch("/api/fleet/tailscale", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "install" }) })).json();
    if (!s.ok) { setStatus("#ts-note", s.note || "install failed", "err"); return; }
    const up = await (await fetch("/api/fleet/tailscale", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "up" }) })).json();
    if (up.url) $("#ts-note").innerHTML = `Open this to add the machine: <a href="${up.url}" target="_blank">${up.url}</a>`;
    else setStatus("#ts-note", up.note || "", up.ok ? "ok" : "err");
    renderTsStatus();
  });

  $("#join-create").addEventListener("click", async () => {
    const d = await (await fetch("/api/fleet/joincode")).json();
    const out = $("#join-code-out");
    out.value = d.code; out.classList.remove("hidden");
    out.focus(); out.select();
    setStatus("#join-status", d.over_tailscale ? "Code ready (works from anywhere via Tailscale). Paste it on the other machine."
      : "Code ready. Both machines must be on the same network, or set up Tailscale first.", "ok");
  });

  $("#join-go").addEventListener("click", async () => {
    const code = $("#join-code-in").value.trim();
    if (!code) { setStatus("#join-status", "Paste a code first", "err"); return; }
    setStatus("#join-status", "Joining…", "busy");
    const d = await (await fetch("/api/fleet/join", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) })).json();
    if (d.ok) {
      setStatus("#join-status", `Joined ${d.joined} (${d.peer_agents} agents there). Their agents appear as name@${d.joined}.`, "ok");
      $("#join-code-in").value = "";
      await fetchFull(); renderFleetPane();
    } else setStatus("#join-status", d.error || "join failed", "err");
  });
}

async function saveFleet() {
  const peers = $("#fleet-peers").value.split("\n").map((l) => {
    const [name, url] = l.trim().split(/\s+/);
    return name && url ? { name, url } : null;
  }).filter(Boolean);
  const body = { node_name: $("#fleet-node").value.trim(), peers };
  const key = $("#fleet-key").value.trim();
  if (key) body.sync_key = key;
  const r = await fetch("/api/settings/fleet", { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  setStatus("#fleet-save-status", d.ok ? "Saved. Restart the app to apply." : `Error: ${d.error}`,
    d.ok ? "ok" : "err");
  if (d.ok) { SET.full.fleet = d.fleet; renderFleetPane(); }
}

async function renderCloudPane() {
  try {
    const d = await (await fetch("/api/cloud")).json();
    $("#cloud-connected").classList.toggle("hidden", !d.connected);
    $("#cloud-form").classList.toggle("hidden", d.connected);
    if (d.connected) {
      const u = d.user;
      $("#cloud-info").textContent = u
        ? `${u.email} · ${u.plan} plan · ${Math.round(u.remaining_tokens / 1000)}k tokens left this month`
        : `${d.url} (cloud unreachable right now)`;
    } else {
      $("#cloud-url").value = d.url || "";
    }
  } catch {}
}

async function cloudConnect() {
  setStatus("#cloud-status", "Signing in…", "busy");
  const r = await fetch("/api/cloud/connect", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: $("#cloud-url").value.trim(),
      email: $("#cloud-email").value.trim(), password: $("#cloud-pass").value }) });
  const d = await r.json();
  if (d.ok) { setStatus("#cloud-status", "", ""); renderCloudPane(); }
  else setStatus("#cloud-status", `Failed: ${d.error}`, "err");
}

async function renderRemotePane() {
  try {
    const d = await (await fetch("/api/remote")).json();
    $("#remote-expose").checked = !!d.expose;
    $("#remote-token").value = d.token || "";
    $("#remote-token").placeholder = d.token ? "" : "generated when you enable";
  } catch {}
}

async function saveRemote(regen) {
  const r = await fetch("/api/remote", { method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expose: $("#remote-expose").checked, regenerate: !!regen }) });
  const d = await r.json();
  $("#remote-token").value = d.token || "";
  setStatus("#remote-status", d.ok ? "Saved. Restart to apply." : "Error", d.ok ? "ok" : "err");
}

function renderPortsPane() {
  const ports = SET.full.ports || {};
  $("#port-app").value = ports.app || 8899;
  $("#port-public").value = ports.public || 8901;
  $("#port-fleet").value = ports.fleet || 8902;
  setStatus("#ports-status", "", "");
}

async function savePorts() {
  const body = { app: +$("#port-app").value, public: +$("#port-public").value,
                 fleet: +$("#port-fleet").value };
  const r = await fetch("/api/settings/ports", { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  setStatus("#ports-status", d.ok ? "Saved. Restart Mission Control to apply." : `Error: ${d.error}`,
    d.ok ? "ok" : "err");
}

async function detectAddresses() {
  const el = $("#fleet-addresses");
  el.textContent = "Detecting...";
  try {
    const d = await (await fetch("/api/fleet/addresses")).json();
    let html = `Same network: <code>${d.lan}</code>`;
    if (d.tailscale) {
      html += `<br>Anywhere (Tailscale): <code>${d.tailscale}</code>`;
    } else {
      html += `<br>Tailscale: ${d.tailscale_error || "not available"} `
        + `(install from tailscale.com for a stable address that works from anywhere)`;
    }
    html += `<br>Paste the address into the Machines list on your other machines.`;
    el.innerHTML = html;
  } catch { el.textContent = "Detection failed"; }
}

function renderGithubPane() {
  const gh = SET.full.github;
  $("#gh-enabled").checked = !!gh.enabled;
  $("#gh-repos").value = (gh.repos || []).join(", ");
  const opts = SET.full.agents.map((a) =>
    `<option value="${a}">${S.agents[a]?.name || a}</option>`).join("");
  $("#gh-route-ci").innerHTML = opts;
  $("#gh-route-code").innerHTML = opts;
  if (gh.route_ci) $("#gh-route-ci").value = gh.route_ci;
  if (gh.route_code) $("#gh-route-code").value = gh.route_code;
  setStatus("#gh-status", "", "");
}

async function saveGithub() {
  const body = {
    enabled: $("#gh-enabled").checked,
    repos: $("#gh-repos").value.split(",").map((s) => s.trim()).filter(Boolean),
    route_ci: $("#gh-route-ci").value, route_code: $("#gh-route-code").value,
  };
  const r = await fetch("/api/settings/github", { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  setStatus("#gh-status", d.ok ? "Saved. Takes effect on next restart." : `Error: ${d.error}`,
    d.ok ? "ok" : "err");
}

/* ----- Welcome (first run): plug in a model and go ----- */
const KEY_HINT = {
  anthropic: ["console.anthropic.com", "https://console.anthropic.com/settings/keys"],
  openai: ["platform.openai.com", "https://platform.openai.com/api-keys"],
  minimax: ["platform.minimax.io", "https://platform.minimax.io/user-center/basic-information/interface-key"],
};
const WEL_ORDER = ["anthropic", "openai", "minimax", "ollama", "lmstudio", "custom-openai"];
const WEL_LABEL = { ollama: "Ollama", lmstudio: "LM Studio", "custom-openai": "Other endpoint" };

async function maybeWelcome() {
  if (new URLSearchParams(location.search).get('demo') === '1') return;
  const full = SET.full || await fetchFull();
  if (full.llm.ready && !location.hash.includes("welcome")) return;

  const presets = full.llm.presets;
  const order = WEL_ORDER.filter((k) => presets[k]);
  $("#wel-providers").innerHTML = order.map((k) =>
    `<button class="wel-chip" data-provider="${k}">${esc(WEL_LABEL[k] || presets[k].label)}</button>`).join("");
  $("#wel-providers").querySelectorAll(".wel-chip").forEach((b) =>
    b.addEventListener("click", () => welPick(b.dataset.provider)));
  welPick(presets[full.llm.provider]?.key_env ? full.llm.provider : "anthropic");
  const cur = (S.user && S.user.name) || "";
  $("#wel-name").value = (cur === "You" || cur === "Alex") ? "" : cur;
  $("#welcome-backdrop").classList.remove("hidden");

  // a local model already running is the zero-step path
  for (const [prov, name] of [["ollama", "Ollama"], ["lmstudio", "LM Studio"]]) {
    const local = await refreshModels("#wel-model-options", prov, "");
    if (!local.length) continue;
    $("#wel-local-kind").textContent = name;
    $("#wel-local-model").textContent = local[0];
    $("#wel-local").classList.remove("hidden");
    $("#wel-or").classList.remove("hidden");
    $("#wel-use-local").onclick = async () => {
      setStatus("#wel-status", "Setting up…", "busy");
      const r = await fetch("/api/settings/llm", { method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: prov, model: local[0], base_url: "", max_tokens: 3000 }) });
      if ((await r.json()).ok) welDone();
      else setStatus("#wel-status", "Could not save", "err");
    };
    break;
  }
}

function welPick(provider) {
  const full = SET.full;
  const p = full.llm.presets[provider] || {};
  $("#wel-provider").value = provider;
  $("#wel-providers").querySelectorAll(".wel-chip").forEach((b) =>
    b.classList.toggle("on", b.dataset.provider === provider));
  const local = provider === "ollama" || provider === "lmstudio";
  const custom = !p.base_url;
  $("#wel-key").classList.toggle("hidden", !p.key_env);
  $("#wel-key").placeholder = p.key_env ? `${p.label} API key` : "";
  const hint = KEY_HINT[provider];
  $("#wel-keyhint").innerHTML = hint
    ? `Get a key at <a href="${hint[1]}" target="_blank" rel="noopener">${hint[0]}</a>. It stays in a local .env on this machine.`
    : local ? "Runs on this machine. Private, free, no key." : "Any OpenAI-compatible endpoint.";
  $("#wel-base").classList.toggle("hidden", !(custom || local));
  $("#wel-base").placeholder = p.base_url || (local ? "http://localhost:11434/v1" : "https://host/v1");
  welSyncModel();
  refreshModels("#wel-model-options", provider, "");
  setStatus("#wel-status", "", "");
}

function welSyncModel() {
  const p = SET.full.llm.presets[$("#wel-provider").value] || {};
  $("#wel-model").value = (p.models || [])[0] || "";
}

async function welConnect() {
  const body = { provider: $("#wel-provider").value, model: $("#wel-model").value.trim(),
                 base_url: $("#wel-base").value.trim(), max_tokens: 3000,
                 api_key: $("#wel-key").value.trim() || null };
  if (!body.model) { setStatus("#wel-status", "Pick a model", "err"); return; }
  setStatus("#wel-status", "Testing connection…", "busy");
  const t = await (await fetch("/api/settings/llm/test", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
  if (!t.ok) { setStatus("#wel-status", `Failed: ${t.error}`, "err"); return; }
  const r = await fetch("/api/settings/llm", { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if ((await r.json()).ok) welDone();
  else setStatus("#wel-status", "Could not save", "err");
}

async function welSaveName() {
  const name = ($("#wel-name") && $("#wel-name").value.trim()) || "";
  if (!name) return;
  try {
    const d = await (await fetch("/api/user", { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) })).json();
    if (d.ok) { S.user = d.user; updateProfile(); }
  } catch {}
}

function welDone() {
  welSaveName();
  setStatus("#wel-status", "Connected. Your agent is saying hello.", "ok");
  setTimeout(() => $("#welcome-backdrop").classList.add("hidden"), 900);
  fetchFull();
  const lead = [...S.threads].find((t) => t.kind === "dm");
  if (lead) openThread(lead.id);
}

/* ----- Heartbeat ----- */
async function renderBrief() {
  try {
    const b = await (await fetch("/api/settings/brief")).json();
    $("#brief-enabled").checked = !!b.enabled;
    $("#brief-at").value = b.at || "08:00";
    $("#brief-agent").innerHTML = SET.full.agents.map((a) =>
      `<option value="${a}">${S.agents[a]?.name || a}</option>`).join("");
    if (b.agent) $("#brief-agent").value = b.agent;
  } catch {}
  if (!SET.briefWired) {
    SET.briefWired = true;
    $("#brief-save").addEventListener("click", async () => {
      const body = { enabled: $("#brief-enabled").checked, agent: $("#brief-agent").value,
                     at: $("#brief-at").value.trim() || "08:00" };
      const d = await (await fetch("/api/settings/brief", { method: "PUT",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
      setStatus("#brief-status", d.ok ? "Saved" : `Error: ${d.error}`, d.ok ? "ok" : "err");
    });
  }
}

async function renderHeartbeat() {
  try {
    const h = await (await fetch("/api/heartbeat")).json();
    $("#hb-enabled").checked = !!h.enabled;
    $("#hb-minutes").value = h.every_minutes;
    $("#hb-agent").innerHTML = SET.full.agents.map((a) =>
      `<option value="${a}">${S.agents[a]?.name || a}</option>`).join("");
    if (h.agent) $("#hb-agent").value = h.agent;
    $("#hb-checklist").value = h.checklist || "";
  } catch {}
}

async function saveHeartbeat() {
  const body = { enabled: $("#hb-enabled").checked, agent: $("#hb-agent").value,
                 every_minutes: +$("#hb-minutes").value || 30,
                 checklist: $("#hb-checklist").value };
  const r = await fetch("/api/heartbeat", { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json();
  setStatus("#hb-status", d.ok ? "Saved" : "Error", d.ok ? "ok" : "err");
}

/* ----- Webhooks + import ----- */
async function renderHooks() {
  try {
    const d = await (await fetch("/api/hooks")).json();
    $("#hook-agent").innerHTML = SET.full.agents.map((a) =>
      `<option value="${a}">${S.agents[a]?.name || a}</option>`).join("");
    if (!d.hooks.length) {
      $("#hook-list").textContent = "No routes yet.";
      return;
    }
    const base = d.public_url || "(tunnel url appears after restart)";
    $("#hook-list").innerHTML = d.hooks.map((h) =>
      `<div style="margin-bottom:7px"><strong>${esc(h.name)}</strong> &rarr; ${esc(h.agent || "lead")} (${esc(h.mode)})<br>` +
      `<code>${esc(base)}/hook/${esc(h.name)}?token=${esc(h.secret)}</code> ` +
      `<button class="btn ghost danger hook-del" data-name="${esc(h.name)}" style="padding:2px 10px;font-size:11px">remove</button></div>`
    ).join("");
    $("#hook-list").querySelectorAll(".hook-del").forEach((b) =>
      b.addEventListener("click", async () => {
        await fetch(`/api/hooks/${b.dataset.name}`, { method: "DELETE" });
        renderHooks();
      }));
  } catch {}
}

// MCP servers: one row per server, with the gate's choices next to it.
const MCP_APPROVAL = {
  destructive: "Approve destructive calls",
  writes: "Approve every change",
  none: "Never ask me",
};

async function renderMcp() {
  let d;
  try { d = await (await fetch("/api/mcp")).json(); } catch { return; }
  if (!d.servers.length) {
    $("#mcp-list").innerHTML = `<div class="set-hint">No servers yet. Paste a block below to add one.</div>`;
    return;
  }
  $("#mcp-list").innerHTML = d.servers.map((m) => {
    const tone = !m.enabled ? "off" : m.error ? "warn" : m.connected ? "on" : "warn";
    const where = m.transport === "http" ? m.url : [m.command, ...(m.args || [])].join(" ");
    const what = m.error ? `<span class="apple-warn">${esc(m.error)}</span>`
      : m.connected ? `${esc(m.title || m.name)} ${esc(m.version || "")} · ${m.tools.length} tools` : (m.enabled ? "connecting…" : "off");
    const who = m.agents ? ` · only ${esc(m.agents.join(", "))}` : "";
    return `<div class="mcp-row">
      <div class="set-row">
        <div class="set-row-label"><span class="ch-dot ${tone}"></span> ${esc(m.name)}</div>
        <div class="set-row-note">${what}${who}${m.token_set ? " · token saved" : ""}<br><code>${esc(where)}</code></div>
        <label class="switch"><input type="checkbox" class="mcp-tog" data-name="${esc(m.name)}" ${m.enabled ? "checked" : ""}><span></span></label>
      </div>
      <div class="set-row mcp-controls">
        <input class="set-control mcp-agents" data-name="${esc(m.name)}" placeholder="All agents" spellcheck="false"
               title="Agent ids that may use this server, comma-separated. Blank for every agent with the mcp tool."
               value="${esc((m.agents || []).join(", "))}">
        <select class="set-control mcp-approval" data-name="${esc(m.name)}">${Object.entries(MCP_APPROVAL).map(([k, v]) =>
          `<option value="${k}" ${m.approval === k ? "selected" : ""}>${v}</option>`).join("")}</select>
        <button class="btn ghost danger mcp-del" data-name="${esc(m.name)}" style="padding:2px 10px;font-size:11px">remove</button>
      </div>
    </div>`;
  }).join("");
  const byName = Object.fromEntries(d.servers.map((m) => [m.name, m]));
  const save = async (name, change) => {
    const m = byName[name];
    const body = { transport: m.transport, url: m.url, command: m.command, args: m.args, cwd: m.cwd,
                   enabled: m.enabled, agents: m.agents, approval: m.approval, ...change };
    setStatus("#mcp-status", "Saving…", "busy");
    const r = await (await fetch(`/api/mcp/${encodeURIComponent(name)}`, { method: "PUT",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
    setStatus("#mcp-status", r.ok ? (r.error ? `Saved, but ${r.error}` : `Saved. ${r.tools} tools.`) : `Error: ${r.error}`,
              r.ok && !r.error ? "ok" : "err");
    renderMcp();
  };
  $("#mcp-list").querySelectorAll(".mcp-tog").forEach((b) =>
    b.addEventListener("change", () => save(b.dataset.name, { enabled: b.checked })));
  $("#mcp-list").querySelectorAll(".mcp-approval").forEach((b) =>
    b.addEventListener("change", () => save(b.dataset.name, { approval: b.value })));
  $("#mcp-list").querySelectorAll(".mcp-agents").forEach((b) =>
    b.addEventListener("change", () => {
      const ids = b.value.split(",").map((x) => x.trim()).filter(Boolean);
      save(b.dataset.name, { agents: ids.length ? ids : null });
    }));
  $("#mcp-list").querySelectorAll(".mcp-del").forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm(`Remove ${b.dataset.name}? Agents lose its tools and its saved token is deleted.`)) return;
      await fetch(`/api/mcp/${encodeURIComponent(b.dataset.name)}`, { method: "DELETE" });
      renderMcp();
    }));
}

// WhatsApp: the owner's own account as a linked device. Fields fill once when
// settings open; only the link state polls, so typing is never overwritten.
const WA = { timer: null, settings: null };

async function renderWhatsApp() {
  let d;
  try { d = await (await fetch("/api/whatsapp")).json(); } catch { return; }
  const s = d.settings;
  WA.settings = s;
  $("#wa-enabled").checked = s.enabled;
  $("#wa-groups").checked = s.groups;
  $("#wa-include").value = s.include.join(", ");
  $("#wa-ignore").value = s.ignore.join(", ");
  $("#wa-agents").value = (s.agents || []).join(", ");
  paintWhatsApp(d);
}

function paintWhatsApp(d) {
  const s = d.settings, b = d.bridge, c = d.counts;
  WA.settings = s;
  let tone = "off", state = "Off", detail = "Turn it on, save, then link your phone.", canLink = false, canUnlink = false;
  if (s.enabled && !b) {
    tone = "warn"; state = "Starting…"; detail = d.problem || "";
  } else if (b) {
    const st = b.status;
    if (st === "connected") {
      tone = "on"; state = `Linked as +${b.number}`; detail = "Listening. Remove it any time here or from the phone's Linked devices."; canUnlink = true;
    } else if (st === "pairing") {
      tone = "warn"; state = "Waiting for your phone"; detail = "";
    } else if (st === "connecting") {
      tone = "warn"; state = "Reconnecting…"; detail = b.problem || ""; canUnlink = b.linked;
    } else {
      tone = st === "error" ? "warn" : "off"; state = b.linked ? "Not connected" : "Not linked";
      detail = b.problem || "Link your phone to start."; canLink = true; canUnlink = b.linked;
    }
  }
  $("#wa-dot").className = `ch-dot ${tone}`;
  $("#wa-state").textContent = state;
  $("#wa-detail").textContent = detail;
  $("#wa-link").classList.toggle("hidden", !(canLink || (!s.enabled)));
  $("#wa-link").textContent = b?.linked ? "Reconnect" : "Link";
  $("#wa-unlink").classList.toggle("hidden", !canUnlink);
  const pairing = b?.status === "pairing";
  $("#wa-pair").classList.toggle("hidden", !pairing);
  if (pairing && d.qr) $("#wa-qr").innerHTML = d.qr;
  if (!pairing) { $("#wa-qr").innerHTML = ""; $("#wa-code").textContent = ""; }
  const parts = [`${c.messages} messages from ${c.chats} chats, ${c.files} files kept for ${s.keep_days} days`];
  $("#wa-counts").textContent = s.enabled || c.messages ? parts.join(" · ") : "";
  // Keep watching while something is about to change: a QR rotates every 20 seconds.
  clearTimeout(WA.timer);
  const settled = !s.enabled || b?.status === "connected" || (b && b.status === "unlinked");
  if (!settled && !$("#settings-backdrop").classList.contains("hidden")) {
    WA.timer = setTimeout(refreshWhatsApp, 2500);
  }
}

async function refreshWhatsApp() {
  try { paintWhatsApp(await (await fetch("/api/whatsapp")).json()); } catch {}
}

function whatsAppBody() {
  const list = (sel) => $(sel).value.split(",").map((x) => x.trim()).filter(Boolean);
  return {
    enabled: $("#wa-enabled").checked, groups: $("#wa-groups").checked,
    include: list("#wa-include"), ignore: list("#wa-ignore"), agents: list("#wa-agents"),
  };
}

async function saveWhatsApp(extra = {}) {
  setStatus("#wa-status", "Saving…", "busy");
  try {
    const d = await (await fetch("/api/whatsapp", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...whatsAppBody(), ...extra }) })).json();
    setStatus("#wa-status", d.ok ? "Saved" : `Error: ${d.error || d.detail}`, d.ok ? "ok" : "err");
    if (d.ok) paintWhatsApp(d);
    return d;
  } catch (e) {
    setStatus("#wa-status", `Error: ${e.message}`, "err");
    return null;
  }
}

async function linkWhatsApp(phone = "") {
  if (!WA.settings?.enabled) {
    $("#wa-enabled").checked = true;
    await saveWhatsApp({ enabled: true });
  }
  setStatus("#wa-status", phone ? "Asking WhatsApp for a code…" : "Starting…", "busy");
  // The bridge needs a moment after the service starts.
  let r = null;
  for (let i = 0; i < 12; i++) {
    r = await (await fetch("/api/whatsapp/link", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone }) })).json();
    if (r.ok || !/not running|did not answer/.test(r.error || "")) break;
    await new Promise((res) => setTimeout(res, 1000));
  }
  if (!r.ok) { setStatus("#wa-status", `Error: ${r.error}`, "err"); return; }
  setStatus("#wa-status", "", "");
  if (r.code) $("#wa-code").textContent = r.code;
  refreshWhatsApp();
}

async function importMcp() {
  const text = $("#mcp-json").value.trim();
  if (!text) { setStatus("#mcp-status", "Paste a JSON block first", "err"); return; }
  setStatus("#mcp-status", "Connecting…", "busy");
  const d = await (await fetch("/api/mcp/import", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ config: text }) })).json();
  if (d.ok) {
    $("#mcp-json").value = "";
    const extra = d.problems?.length ? ` Skipped: ${d.problems.join("; ")}` : "";
    setStatus("#mcp-status", `Added ${d.added.join(", ")}.${extra}`, d.problems?.length ? "err" : "ok");
  } else {
    setStatus("#mcp-status", `Error: ${d.error}`, "err");
  }
  renderMcp();
}

async function addHook() {
  const name = $("#hook-name").value.trim();
  if (!name) { setStatus("#hook-status", "Name required", "err"); return; }
  const r = await fetch("/api/hooks", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, agent: $("#hook-agent").value,
                           mode: $("#hook-mode").value }) });
  const d = await r.json();
  setStatus("#hook-status", d.ok ? "Added. Restart if this is the first route." : `Error: ${d.error}`,
    d.ok ? "ok" : "err");
  if (d.ok) { $("#hook-name").value = ""; renderHooks(); }
}

async function runImport() {
  const url = $("#import-url").value.trim();
  if (!url) { setStatus("#import-status", "URL required", "err"); return; }
  setStatus("#import-status", "Importing…", "busy");
  const r = await fetch("/api/import", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, kind: $("#import-kind").value }) });
  const d = await r.json();
  setStatus("#import-status",
    d.ok ? `Imported ${d.imported} ${d.kind}` : `Error: ${d.error}`,
    d.ok ? "ok" : "err");
  if (d.ok && d.kind === "agents") refreshState();
}

/* ----- Jobs pane ----- */
// Three near-identical held commands are impossible to tell apart once
// truncated, so say which one was already answered and what moved since.
function priorNote(prior, esc) {
  if (!prior) return "";
  const when = new Date(prior.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (prior.identical) {
    return `<div class="job-note">Identical to the command you ${esc(prior.status === "denied" ? "declined" : "approved")} at ${when}. Approving runs it again.</div>`;
  }
  const lines = (prior.diff || []).map((l) =>
    `<div class="approval-diff-line ${l[0] === "+" ? "add" : "del"}">${esc(l)}</div>`).join("");
  return `<details class="job-note"><summary>Differs from the command you ${esc(prior.status === "denied" ? "declined" : "approved")} at ${when}</summary>${lines || "<div>only whitespace changed</div>"}</details>`;
}

// A command the guard held. It sits above the watches because it is the only
// thing in this panel that is waiting on the owner to do something.
async function renderApprovalsInto(wrap, agents) {
  let d;
  try { d = await (await fetch("/api/approvals")).json(); } catch { return 0; }
  const held = (d.approvals || []).filter((a) => a.status === "pending");
  if (!held.length) return 0;
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const html = held.map((a) => `
    <div class="set-card job-card">
      <div class="job-head">
        <strong>&#9888; Command held for you</strong>
        <span class="job-status pending">needs approval</span>
      </div>
      <div class="job-meta">${esc((agents && agents[a.agent]) || a.agent)} &middot; in ${esc(a.cwd)}</div>
      <div class="job-task"><code>${esc(a.command)}</code></div>
      ${priorNote(a.prior, esc)}
      <div class="job-actions">
        <button class="btn primary approval-act" data-id="${esc(a.id)}" data-act="approve">Run it</button>
        <button class="btn ghost danger approval-act" data-id="${esc(a.id)}" data-act="deny">Decline</button>
      </div>
    </div>`).join("");
  wrap.insertAdjacentHTML("afterbegin",
    `<div class="set-hint">Held for approval &mdash; approving runs the command exactly as written</div>${html}`);
  wrap.querySelectorAll(".approval-act").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    await fetch(`/api/approvals/${b.dataset.id}/${b.dataset.act}`, { method: "POST" });
    renderJobsPane();
  }));
  return held.length;
}

async function renderWatchesInto(wrap, agents) {
  let w;
  try { w = await (await fetch("/api/watches")).json(); } catch { return; }
  if (!w.watches || !w.watches.length) return;
  // agents name and write their own watches: everything here is their text
  const html = w.watches.map((x) => `
    <div class="set-card job-card">
      <div class="job-head">
        <strong>${esc(x.name)}</strong>
        <span class="job-status active">${Number(x.checks)}/${Number(x.max_checks)} checks</span>
      </div>
      <div class="job-meta">${esc((agents && agents[x.agent]) || x.agent)} &middot; every ${Number(x.interval)}s &middot; no approval, auto-stops</div>
      <div class="job-task"><code>${esc(x.poll || "")}</code></div>
      <div class="job-actions">
        <button class="btn ghost danger watch-cancel" data-id="${Number(x.id)}">Stop watching</button>
      </div>
    </div>`).join("");
  wrap.insertAdjacentHTML("afterbegin",
    `<div class="set-hint">Active watches (polling automatically, no approval)</div>${html}`);
  wrap.querySelectorAll(".watch-cancel").forEach((b) => b.addEventListener("click", async () => {
    await fetch(`/api/watches/${b.dataset.id}/cancel`, { method: "POST" });
    renderJobsPane();
  }));
}

async function renderJobsPane() {
  const d = await (await fetch("/api/cron")).json();
  const wrap = $("#job-cards");
  const pending = d.jobs.filter((j) => j.status === "pending").length;
  updateJobsBadge(pending);
  if (!d.jobs.length) {
    wrap.innerHTML = `<div class="set-hint">No scheduled jobs yet. Ask it to schedule something, e.g. "check the error logs every morning at 9".</div>`;
    await renderWatchesInto(wrap, d.agents);
    updateJobsBadge(pending + await renderApprovalsInto(wrap, d.agents));
    return;
  }
  const fmtNext = (j) => j.next_run
    ? new Date(j.next_run * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : "-";
  // An agent writes every field of a job it schedules, so none of it is markup;
  // and the check it runs before each fire is shown whole, since approving runs it.
  wrap.innerHTML = d.jobs.map((j) => `
    <div class="set-card job-card">
      <div class="job-head">
        <strong>${esc(j.name)}</strong>
        <span class="job-status ${esc(j.status)}">${esc(j.status)}</span>
      </div>
      <div class="job-meta">${esc(d.agents[j.agent] || j.agent)} &middot; ${esc(j.schedule)} &middot; next: ${fmtNext(j)}${j.max_runs ? ` &middot; ${Number(j.runs) || 0}/${Number(j.max_runs)} checks` : ""}</div>
      <div class="job-task">${esc(j.task)}${j.monitor_cmd ? `<div class="job-note">check before each run: <code>${esc(j.monitor_cmd)}</code></div>` : ""}${j.note ? `<div class="job-note">notepad: ${esc(j.note)}</div>` : ""}</div>
      <div class="job-actions">
        ${j.status === "pending" ? `<button class="btn primary job-act" data-id="${Number(j.id)}" data-act="approve">Approve</button>` : ""}
        ${j.status === "active" ? `<button class="btn ghost job-act" data-id="${Number(j.id)}" data-act="pause">Pause</button>` : ""}
        ${j.status === "paused" ? `<button class="btn primary job-act" data-id="${Number(j.id)}" data-act="resume">Resume</button>` : ""}
        <button class="btn ghost danger job-act" data-id="${Number(j.id)}" data-act="delete">Delete</button>
      </div>
    </div>`).join("");
  wrap.querySelectorAll(".job-act").forEach((b) => b.addEventListener("click", async () => {
    await fetch(`/api/cron/${b.dataset.id}/${b.dataset.act}`, { method: "POST" });
    renderJobsPane();
  }));
  await renderWatchesInto(wrap, d.agents);
  updateJobsBadge(pending + await renderApprovalsInto(wrap, d.agents));
}

function updateJobsBadge(pending) {
  const badge = $("#jobs-badge"), dot = $("#settings-dot");
  if (badge) { badge.textContent = pending || ""; badge.classList.toggle("hidden", !pending); }
  if (dot) dot.classList.toggle("hidden", !pending);
}

async function pollPendingJobs() {
  try {
    const d = await (await fetch("/api/cron")).json();
    S.jobs = d.jobs;
    updateJobsBadge(d.jobs.filter((j) => j.status === "pending").length);
  } catch {}
}

/* ----- wiring ----- */
function initSettings() {
  $("#settings-btn").addEventListener("click", () => openSettings());
  $("#settings-close").addEventListener("click", closeSettings);
  $("#settings-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "settings-backdrop") closeSettings();
  });
  document.querySelectorAll(".set-tab").forEach((tab) =>
    tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
  $("#llm-provider").addEventListener("change", () => {
    const p = SET.full.llm.presets[$("#llm-provider").value] || {};
    $("#llm-baseurl").value = "";
    $("#llm-model").value = (p.models || [])[0] || "";
    syncLLMFields();
    refreshModels("#llm-model-options");
  });
  $("#llm-baseurl").addEventListener("change", () => refreshModels("#llm-model-options"));
  $("#llm-test").addEventListener("click", testLLM);
  $("#llm-save").addEventListener("click", saveLLM);
  $("#gh-save").addEventListener("click", saveGithub);
  $("#fleet-save").addEventListener("click", saveFleet);
  $("#fleet-detect").addEventListener("click", detectAddresses);
  $("#ports-save").addEventListener("click", savePorts);
  $("#hb-save").addEventListener("click", saveHeartbeat);
  $("#hook-add").addEventListener("click", addHook);
  $("#mcp-import").addEventListener("click", importMcp);
  $("#wa-save").addEventListener("click", () => saveWhatsApp());
  $("#wa-link").addEventListener("click", () => linkWhatsApp());
  $("#wa-code-btn").addEventListener("click", () => {
    const phone = $("#wa-phone").value.replace(/\D/g, "");
    if (phone.length < 8) { setStatus("#wa-status", "Enter your number with its country code", "err"); return; }
    linkWhatsApp(phone);
  });
  $("#wa-unlink").addEventListener("click", async () => {
    if (!confirm("Unlink WhatsApp? This Mac is removed from your phone's linked devices. Messages already stored stay until they age out.")) return;
    await fetch("/api/whatsapp/unlink", { method: "POST" });
    refreshWhatsApp();
  });
  $("#import-run").addEventListener("click", runImport);
  $("#usage-refresh").addEventListener("click", renderUsage);
  $("#remote-expose").addEventListener("change", () => saveRemote(false));
  $("#remote-regen").addEventListener("click", () => saveRemote(true));
  $("#cloud-connect").addEventListener("click", cloudConnect);
  $("#cloud-disconnect").addEventListener("click", async () => {
    await fetch("/api/cloud/disconnect", { method: "POST" });
    renderCloudPane();
  });
  $("#wel-provider").addEventListener("change", () => { welSyncModel();
    refreshModels("#wel-model-options", $("#wel-provider").value, ""); });
  $("#wel-connect").addEventListener("click", welConnect);
  $("#wel-skip").addEventListener("click", () =>
    $("#welcome-backdrop").classList.add("hidden"));
  maybeWelcome();
  pollPendingJobs();
}
initSettings();

/* ---------- Voice mode ----------
   Talk to the agent you have open: the microphone records until you pause,
   the server transcribes it (Whisper on this Mac), the text is sent as a
   spoken message, and the reply bubbles are read aloud in the team voice.
   Hands-free listens again as soon as the agent has finished. */
const V = { on: false, phase: "idle", err: "", stream: null, ctx: null, analyser: null,
            rec: null, chunks: [], mime: "", speaking: Promise.resolve(), gen: 0,
            audio: null, lastId: 0, listenTimer: null, awaitTimer: null, awaitStart: 0 };

function voiceHandsFree() { return $("#voice-handsfree").checked; }

function voiceUI() {
  $("#voice-panel").classList.toggle("hidden", !V.on);
  $("#voice-btn").classList.toggle("active", V.on);
  $("#mic-btn").classList.toggle("active", V.on);
  const t = currentThread();
  $("#voice-title").textContent = t ? `Talking with ${t.name}` : "Voice mode";
  const label = { idle: "Paused. Press Talk, or turn on hands-free.",
                  starting: "Starting microphone…", listening: "Listening…",
                  thinking: "Thinking…", speaking: "Speaking…" }[V.phase] || "";
  $("#voice-status").textContent = V.err || label;
  $("#voice-orb").className = `voice-orb ${V.phase}`;
  const mood = V.err ? "sad" : ({ listening: "listening", thinking: "thinking", speaking: "speaking" }[V.phase] || "idle");
  $("#voice-orb").innerHTML = t?.agent
    ? `<span class="avatar-wrap st-${mood}">${faceSVG(S.agents[t.agent], 34, mood)}</span>` : "";
  $("#voice-talk").classList.toggle("hidden", V.phase !== "idle");
}

async function voiceOpen() {
  if (V.on) return;
  V.on = true; V.err = ""; V.phase = "starting";
  V.lastId = S.msgs.length ? S.msgs[S.msgs.length - 1].id : 0;
  voiceUI();
  try {
    V.stream = await navigator.mediaDevices.getUserMedia(
      { audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    V.err = "Microphone unavailable: " + (e.message || e.name || e);
    V.phase = "idle"; voiceUI(); return;
  }
  V.ctx = new (window.AudioContext || window.webkitAudioContext)();
  V.analyser = V.ctx.createAnalyser();
  V.analyser.fftSize = 1024;
  V.ctx.createMediaStreamSource(V.stream).connect(V.analyser);
  V.mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]
    .find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
  voiceListen();
}

function voiceStopAll() {
  V.gen += 1;
  if (V.audio) { try { V.audio.pause(); } catch {} V.audio = null; }
  if (V.rec && V.rec.state !== "inactive") { V.rec.onstop = null; try { V.rec.stop(); } catch {} }
  V.rec = null;
  clearInterval(V.listenTimer); V.listenTimer = null;
  clearInterval(V.awaitTimer); V.awaitTimer = null;
}

function voiceClose() {
  V.on = false;
  voiceStopAll();
  if (V.stream) { V.stream.getTracks().forEach((t) => t.stop()); V.stream = null; }
  if (V.ctx) { V.ctx.close().catch(() => {}); V.ctx = null; }
  V.phase = "idle"; V.err = "";
  voiceUI();
}

function voiceLevel() {
  const buf = new Float32Array(V.analyser.fftSize);
  V.analyser.getFloatTimeDomainData(buf);
  let s = 0;
  for (const x of buf) s += x * x;
  return Math.sqrt(s / buf.length);
}

function voiceListen() {
  if (!V.on || !V.stream) return;
  voiceStopAll();
  V.phase = "listening"; V.err = ""; voiceUI();
  const rec = new MediaRecorder(V.stream, V.mime ? { mimeType: V.mime } : undefined);
  V.rec = rec; V.chunks = [];
  rec.ondataavailable = (e) => { if (e.data && e.data.size) V.chunks.push(e.data); };
  let spoke = 0, quiet = 0;
  const started = Date.now();
  rec.onstop = () => {
    const blob = new Blob(V.chunks, { type: rec.mimeType || V.mime || "audio/webm" });
    if (spoke >= 4) voiceUtterance(blob, rec.mimeType || V.mime);
    else if (V.on && voiceHandsFree()) voiceListen();
    else { V.phase = "idle"; voiceUI(); }
  };
  rec.start(250);
  // speech starts above the threshold; ~0.9s of quiet after it ends the take
  V.listenTimer = setInterval(() => {
    if (V.rec !== rec) return;
    const level = voiceLevel();
    $("#voice-orb").style.setProperty("--level", Math.min(1, level * 12).toFixed(2));
    if (level > 0.015) { spoke += 1; quiet = 0; } else if (spoke) { quiet += 1; }
    const age = Date.now() - started;
    if ((spoke && quiet >= 9) || age > 25000 || (!spoke && age > 15000)) {
      clearInterval(V.listenTimer); V.listenTimer = null;
      try { rec.stop(); } catch {}
    }
  }, 100);
}

async function voiceUtterance(blob, mime) {
  V.phase = "thinking"; voiceUI();
  const fd = new FormData();
  fd.append("file", blob, /mp4/.test(mime) ? "voice.m4a" : "voice.webm");
  let text = "";
  try {
    const d = await (await fetch("/api/transcribe", { method: "POST", body: fd })).json();
    if (!d.ok) throw new Error(d.error || "transcription failed");
    text = (d.text || "").trim();
  } catch (e) {
    V.err = String(e.message || e); V.phase = "idle"; voiceUI(); return;
  }
  if (!text) {
    if (V.on && voiceHandsFree()) voiceListen(); else { V.phase = "idle"; voiceUI(); }
    return;
  }
  V.lastId = S.msgs.length ? S.msgs[S.msgs.length - 1].id : 0;
  await fetch("/api/send", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ thread: S.current, text, voice: true }) });
  voiceAwait();
}

// wait for the reply to be spoken and the agent to go quiet, then listen again
function voiceAwait() {
  clearInterval(V.awaitTimer);
  V.awaitStart = Date.now();
  let idleTicks = 0;
  V.awaitTimer = setInterval(() => {
    if (!V.on) { clearInterval(V.awaitTimer); V.awaitTimer = null; return; }
    if (V.phase === "speaking") { idleTicks = 0; return; }
    const t = currentThread();
    const idle = !t?.agent || (S.agentState[t.agent] || "idle") === "idle";
    idleTicks = idle ? idleTicks + 1 : 0;
    const waited = Date.now() - V.awaitStart;
    if ((idle && idleTicks >= 4 && waited > 2500) || waited > 180000) {
      clearInterval(V.awaitTimer); V.awaitTimer = null;
      if (voiceHandsFree()) voiceListen(); else { V.phase = "idle"; voiceUI(); }
    }
  }, 500);
}

function voiceOnMessage(m) {
  if (!V.on || m.sender === "user" || (m.kind && m.kind !== "chat")) return;
  if (m.meta?.silent || m.id <= V.lastId) return;
  V.lastId = m.id;
  voiceSpeak(m.text.replace(/`+/g, ""));
}

function voiceSpeak(text) {
  const g = V.gen;
  V.speaking = V.speaking.then(async () => {
    if (g !== V.gen || !V.on) return;
    V.phase = "speaking"; voiceUI();
    try {
      const d = await (await fetch("/api/tts", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) })).json();
      if (d.ok && g === V.gen) {
        await new Promise((done) => {
          const a = new Audio(d.url);
          V.audio = a;
          a.onended = done; a.onerror = done;
          a.play().catch(done);
        });
      }
    } catch {}
    V.audio = null;
    if (V.on && g === V.gen) { V.phase = "thinking"; voiceUI(); }
  });
}

function initVoice() {
  $("#stop-btn").addEventListener("click", async () => {
    const t = currentThread();
    if (t && t.agent) await fetch(`/api/agents/${t.agent}/stop`, { method: "POST" });
  });
  document.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
    if (e.key === "1") { e.preventDefault(); window.MissionDeck?.show("home"); }
    if (e.key === "2") { e.preventDefault(); window.MissionDeck?.show("schedule"); }
    if (e.key === "3") { e.preventDefault(); openSettings(); }
  });
  const toggle = () => (V.on ? voiceClose() : voiceOpen());
  $("#voice-btn").addEventListener("click", toggle);
  $("#mic-btn").addEventListener("click", toggle);
  $("#voice-close").addEventListener("click", voiceClose);
  $("#voice-stop").addEventListener("click", () => { voiceStopAll(); V.phase = "idle"; voiceUI(); });
  $("#voice-talk").addEventListener("click", voiceListen);
  $("#voice-handsfree").addEventListener("change", () => {
    if (voiceHandsFree() && V.on && V.phase === "idle") voiceListen();
  });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initVoice);
else initVoice();

/* ----- Team voice (settings) ----- */
async function renderVoicePane() {
  try {
    const s = await (await fetch("/api/settings/voice")).json();
    const vs = (await (await fetch("/api/settings/voice/voices")).json()).voices || [];
    const sel = $("#voice-id");
    sel.innerHTML = vs.map((v) => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("")
      || `<option value="">System voice</option>`;
    if (s.voice) sel.value = s.voice;
    $("#voice-speed").value = String(s.speed);
    const p = s.providers || {};
    $("#voice-stt").textContent = p.whisper ? "Whisper on this Mac, nothing leaves the machine"
      : p.openai ? "OpenAI Whisper" : "Not available: install faster-whisper in the server's venv";
    setStatus("#voice-status-set", "", "");
  } catch {}
  if (!SET.voiceWired) {
    SET.voiceWired = true;
    $("#voice-save").addEventListener("click", saveVoice);
    $("#voice-preview").addEventListener("click", previewVoice);
  }
}

async function saveVoice() {
  const body = { voice: $("#voice-id").value, speed: +$("#voice-speed").value };
  const d = await (await fetch("/api/settings/voice", { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
  setStatus("#voice-status-set", d.ok ? "Saved" : `Error: ${d.error}`, d.ok ? "ok" : "err");
  return d.ok;
}

async function previewVoice() {
  setStatus("#voice-status-set", "Synthesising…", "busy");
  if (!(await saveVoice())) return;
  const d = await (await fetch("/api/tts", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: "Mission Control online. Standing by." }) })).json();
  if (!d.ok) { setStatus("#voice-status-set", `Error: ${d.error}`, "err"); return; }
  new Audio(d.url).play().catch(() => {});
  setStatus("#voice-status-set", "", "");
}


/* ----- Budget, phones ----- */
function renderBudget() {
  const b = SET.full.budget || {};
  $("#budget-tokens").value = b.daily_tokens || "";
  const used = (b.used_today || 0).toLocaleString();
  $("#budget-status").textContent = b.daily_tokens
    ? `${used} of ${b.daily_tokens.toLocaleString()} tokens used today (${Math.round((b.pct || 0) * 100)}%)`
    : `${used} tokens used today, no limit set`;
  if (!SET.budgetWired) {
    SET.budgetWired = true;
    $("#budget-save").addEventListener("click", async () => {
      const d = await (await fetch("/api/settings/budget", { method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ daily_tokens: +$("#budget-tokens").value || 0 }) })).json();
      if (d.ok) { SET.full.budget = d; renderBudget(); }
      else $("#budget-status").textContent = `Error: ${d.error}`;
    });
  }
}

function renderDevices() {
  const list = SET.full.devices || [];
  $("#devices-list").textContent = list.length
    ? list.map((d) => d.name).join(", ") : "No phone registered yet.";
  if (!SET.devicesWired) {
    SET.devicesWired = true;
    $("#push-test").addEventListener("click", async () => {
      setStatus("#push-status", "Sending…", "busy");
      const d = await (await fetch("/api/devices/test", { method: "POST" })).json();
      setStatus("#push-status", d.ok ? "Sent. Check the phone." : `Failed: ${d.error}`, d.ok ? "ok" : "err");
    });
  }
}

/* ----- Memory pane ----- */
function ago(ts) {
  const m = Math.max(1, Math.round((Date.now() / 1000 - ts) / 60));
  if (m < 60) return `${m}m`;
  if (m < 1440) return `${Math.round(m / 60)}h`;
  return `${Math.round(m / 1440)}d`;
}

async function renderMemoryPane() {
  const q = ($("#mem-q").value || "").trim();
  try {
    const d = await (await fetch(`/api/memory?q=${encodeURIComponent(q)}`)).json();
    const rows = q ? d.memories : [...d.memories].reverse();
    $("#mem-list").innerHTML = rows.length ? rows.map((m) => `
      <div class="mem-row" data-id="${m.id}">
        <div class="mem-head"><span class="mem-kind">${esc(m.kind || "fact")}</span>
          <span class="mem-meta">${esc(m.author)} · ${ago(m.ts)} ago</span>
          <span class="mem-actions"><button class="mem-edit">edit</button><button class="mem-del">delete</button></span></div>
        <div class="mem-text">${esc(m.text)}</div>
      </div>`).join("") : `<div class="set-hint">${q ? "Nothing matches." : "No memories yet."}</div>`;
  } catch { $("#mem-list").textContent = "Memory unavailable."; }
  $("#mem-list").querySelectorAll(".mem-del").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".mem-row");
    await fetch(`/api/memory/${row.dataset.id}`, { method: "DELETE" });
    row.remove();
  }));
  $("#mem-list").querySelectorAll(".mem-edit").forEach((b) => b.addEventListener("click", () => {
    const row = b.closest(".mem-row"), textEl = row.querySelector(".mem-text");
    if (row.querySelector("textarea")) return;
    const ta = document.createElement("textarea");
    ta.className = "set-control wide"; ta.rows = 3; ta.value = textEl.textContent;
    const save = document.createElement("button"); save.className = "btn primary"; save.textContent = "Save";
    save.style.marginTop = "6px";
    textEl.replaceWith(ta); row.appendChild(save);
    save.addEventListener("click", async () => {
      const d = await (await fetch(`/api/memory/${row.dataset.id}`, { method: "PUT",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: ta.value }) })).json();
      if (d.ok) renderMemoryPane();
    });
  }));
  renderSkills();
  renderPlaybooks();
  if (!SET.memWired) {
    SET.memWired = true;
    let t;
    $("#mem-q").addEventListener("input", () => { clearTimeout(t); t = setTimeout(renderMemoryPane, 250); });
  }
}

const APPLE_BLURB = {
  notes: "search, read and create notes",
  calendar: "see what's coming up, and add events",
  reminders: "list, add and tick off reminders",
  mail: "search and read your inbox, and open drafts — never send",
  messages: "read recent iMessage and SMS history — read-only",
};

async function renderApple() {
  const d = await (await fetch("/api/apple")).json();
  const reach = d.reachable || {};
  $("#apple-list").innerHTML = (d.apps || []).map((a) => {
    const ok = reach[a];
    const why = reach[`${a}_error`] || "";
    return `<div class="set-row">
      <div class="set-row-label">${esc(a)}</div>
      <div class="set-row-note">${esc(APPLE_BLURB[a] || "")}${
        ok ? "" : `<span class="apple-warn">macOS is refusing access${why ? ": " + esc(why.slice(0, 120)) : ""}</span>`}</div>
      <label class="switch"><input type="checkbox" class="apple-tog" data-app="${esc(a)}"
        ${d.connected[a] ? "checked" : ""}><span></span></label>
    </div>`;
  }).join("");
  $("#apple-list").querySelectorAll(".apple-tog").forEach((b) =>
    b.addEventListener("change", async () => {
      await fetch("/api/apple", { method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app: b.dataset.app, enabled: b.checked }) });
      renderApple();
    }));
  $("#apple-note").textContent = d.remindctl
    ? "Reminders go through remindctl (EventKit): about 0.2s where AppleScript takes 44s."
    : "Reminders will work but slowly. `brew install steipete/tap/remindctl` makes it ~200x faster.";
}

// What each state means to the owner, in the words the pane shows.
const IMP_STATUS = {
  fixing: "fixing", pending: "waiting on you", applied: "landed", pushed: "landed and pushed",
  discarded: "discarded", rolled_back: "rolled back", failed: "failed", rejected: "rejected",
  approved: "approved, lands when nothing is running",
};
const ISSUE_STATE = {
  open: "not fixed yet", fixing: "fixing now", pending: "fix ready", gave_up: "needs you",
};

async function renderImprovements() {
  const d = await (await fetch("/api/improvements")).json();
  S.improvements = d.improvements || [];
  const set = d.settings || { enabled: !!d.enabled };
  const autoApply = !!set.auto_apply;
  document.querySelectorAll(".improve-switch").forEach((box) => {
    const key = box.dataset.key;
    box.checked = !!set[key];
    // each switch only means something once the one above it is on
    box.disabled = ((key === "autofix" || key === "peer_approve") && !set.enabled) ||
      (key === "auto_apply" && !(set.enabled && set.autofix));
    if (box.dataset.wired) return;
    box.dataset.wired = "1";
    box.addEventListener("change", async () => {
      await fetch("/api/improvements-enabled", { method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: box.checked }) });
      renderImprovements();
    });
  });
  const check = $("#improve-check");
  check.disabled = !(set.enabled && set.autofix);
  if (!check.dataset.wired) {
    check.dataset.wired = "1";
    check.addEventListener("click", async () => {
      check.disabled = true;
      await fetch("/api/improvements/check", { method: "POST" });
      setTimeout(renderImprovements, 1500);
    });
  }
  const last = d.last_check || {};
  $("#improve-last").textContent = !(set.enabled && set.autofix) ? "Self-repair is off."
    : last.ts ? `Last check ${new Date(last.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}: ${last.result}`
    : "Checks every 10 minutes.";

  const blocked = $("#improve-blocked");
  blocked.classList.toggle("hidden", !d.blocked);
  blocked.textContent = d.blocked ? `Cannot change code right now: ${d.blocked}` : "";
  $("#improve-protected").textContent =
    "Off limits to agents and to self-repair, always: " + (d.protected || []).join(", ") +
    ". A change touching any of those is refused before you ever see it.";

  const issues = d.issues || [];
  $("#improve-issues").innerHTML = issues.length ? issues.map((i) => `
    <div class="mem-row">
      <div class="mem-head"><span class="mem-kind">${esc(ISSUE_STATE[i.state] || i.state)}</span>
        <span class="mem-text-inline"><strong>${esc(i.title)}</strong>
          <span class="mem-meta">${i.count} times since ${esc(new Date(i.first * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }))}
            &middot; ${esc(Object.entries(i.agents || {}).map(([a, n]) => `${S.agents[a]?.name || a} ${n}`).join(", "))}
            ${i.attempts ? `&middot; ${i.attempts} fix${i.attempts > 1 ? "es" : ""} tried` : ""}</span>
        </span></div>
      <details><summary class="mem-meta">Evidence</summary><pre class="imp-pre">${esc(i.evidence)}</pre></details>
    </div>`).join("") : (set.enabled && set.autofix ? `<div class="set-hint">No problem has repeated on the code running now.</div>` : "");

  // with agents approving each other's, their proposals are not waiting on you
  const peers = !!set.peer_approve;
  const pending = S.improvements.filter((i) => i.status === "pending" && !(peers && i.agent !== "selfimprove")).length;
  const badge = $("#improve-badge");
  badge.classList.toggle("hidden", !pending);
  badge.textContent = pending || "";

  $("#improve-list").innerHTML = S.improvements.length ? S.improvements.map((i) => {
    const auto = i.agent === "selfimprove";
    const by = i.stat.approved_by ? ` by ${S.agents[i.stat.approved_by]?.name || i.stat.approved_by}` : "";
    const status = auto && i.status === "pending" && autoApply ? "lands when nothing is running"
      : !auto && i.status === "pending" && peers ? "waiting for a teammate"
      : i.status === "approved" ? `approved${by}, lands when nothing is running`
      : (IMP_STATUS[i.status] || i.status);
    return `
    <div class="mem-row imp-row" data-id="${i.id}">
      <div class="mem-head"><span class="mem-kind">${esc(status)}</span>
        <span class="mem-text-inline"><strong>${esc(i.title)}</strong>
          <span class="mem-meta">${esc(auto ? "self-repair" : (S.agents[i.agent]?.name || i.agent))} &middot; ${(i.stat.files || []).length} files &middot; +${i.stat.added || 0}/-${i.stat.removed || 0}</span>
        </span>
        <span class="mem-actions">
          ${["fixing", "discarded"].includes(i.status) ? "" : `<button class="imp-btn" data-id="${i.id}" data-act="diff">diff</button>`}
          ${["pending", "approved"].includes(i.status) ? `<button class="imp-btn" data-id="${i.id}" data-act="approve">${(auto && autoApply) || i.status === "approved" ? "land now" : "approve"}</button><button class="imp-btn mem-del" data-id="${i.id}" data-act="reject">reject</button>` : ""}
        </span></div>
      ${i.note ? `<div class="mem-text">${esc(i.note)}</div>` : ""}
      <div class="imp-diff hidden"></div>
    </div>`; }).join("") : `<div class="set-hint">No changes yet.</div>`;
  $("#improve-list").querySelectorAll(".imp-btn").forEach((b) =>
    b.addEventListener("click", () => onImprovement(b.dataset.id, b.dataset.act, b)));
}

async function onImprovement(id, act, btn) {
  if (act === "diff") {
    const d = await (await fetch(`/api/improvements/${id}/diff`)).json();
    const row = btn.closest(".imp-row");
    if (row) {
      const box = row.querySelector(".imp-diff");
      box.classList.toggle("hidden");
      box.innerHTML = `<pre class="imp-pre">${esc(d.diff || "(empty)")}</pre>`;
    } else {
      alertDiff(d);
    }
    return;
  }
  if (act === "approve" && !confirmOnce(btn, "Approve & restart?")) return;
  btn.disabled = true;
  const r = await (await fetch(`/api/improvements/${id}/${act}`, { method: "POST" })).json();
  if (!r.ok) {
    btn.disabled = false;
    setStatus("#improve-blocked", r.error || "failed", "err");
    $("#improve-blocked").classList.remove("hidden");
    $("#improve-blocked").textContent = r.error || "failed";
    return;
  }
  if (act === "approve") setLink(false);   // the server is restarting under us
  await renderImprovements();
  renderMessages();
}

// a second click confirms, so "approve and restart the app" is never one stray tap
function confirmOnce(btn, text) {
  if (btn.dataset.confirm) return true;
  btn.dataset.confirm = "1";
  btn.dataset.was = btn.textContent;
  btn.textContent = text;
  setTimeout(() => {
    if (btn.dataset.confirm) {
      delete btn.dataset.confirm;
      btn.textContent = btn.dataset.was;
    }
  }, 4000);
  return false;
}

function alertDiff(d) {
  const box = document.createElement("div");
  box.className = "imp-modal";
  box.innerHTML = `<div class="imp-modal-inner"><div class="imp-modal-head">${esc(d.title || "diff")}<button class="set-x">&times;</button></div><pre class="imp-pre">${esc(d.diff || "(empty)")}</pre></div>`;
  box.addEventListener("click", (e) => {
    if (e.target === box || e.target.classList.contains("set-x")) box.remove();
  });
  document.body.appendChild(box);
}

async function renderSkills() {
  const d = await (await fetch("/api/skills")).json();
  const box = $("#autolearn");
  box.checked = d.autolearn !== false;
  if (!box.dataset.wired) {
    box.dataset.wired = "1";
    box.addEventListener("change", () => fetch("/api/skills-autolearn", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ autolearn: box.checked }) }));
  }
  $("#skill-list").innerHTML = d.skills.length ? d.skills.map((s) => `
    <div class="mem-row skill-row" data-slug="${esc(s.slug)}">
      <div class="mem-head"><span class="mem-kind">skill</span>
        <span class="mem-text-inline"><strong>${esc(s.name)}</strong> ${esc(s.description || "")}${
          s.avoid ? `<span class="skill-avoid">not for: ${esc(s.avoid)}</span>` : ""}</span>
        <span class="mem-meta skill-uses" title="times an agent has read this skill">${
          s.uses ? `${s.uses}&times;` : "unused"}</span>
        <span class="mem-actions"><button class="skill-open">open</button><button class="skill-del">delete</button></span></div>
      <div class="skill-body hidden"></div>
    </div>`).join("") : `<div class="set-hint">No skills saved yet.</div>`;
  $("#skill-list").querySelectorAll(".skill-del").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".skill-row");
    await fetch(`/api/skills/${encodeURIComponent(row.dataset.slug)}`, { method: "DELETE" });
    row.remove();
  }));
  $("#skill-list").querySelectorAll(".skill-open").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".skill-row"), body = row.querySelector(".skill-body");
    if (!body.classList.contains("hidden")) { body.classList.add("hidden"); return; }
    const d = await (await fetch(`/api/skills/${encodeURIComponent(row.dataset.slug)}`)).json();
    body.innerHTML = `<textarea class="set-control wide" rows="12" spellcheck="false"></textarea>
      <div class="set-footer"><span class="set-status"></span><button class="btn primary skill-save">Save</button></div>`;
    body.querySelector("textarea").value = d.content || "";
    body.classList.remove("hidden");
    body.querySelector(".skill-save").addEventListener("click", async () => {
      const r = await (await fetch(`/api/skills/${encodeURIComponent(row.dataset.slug)}`, { method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: body.querySelector("textarea").value }) })).json();
      body.querySelector(".set-status").textContent = r.ok ? "Saved" : `Error: ${r.error}`;
    });
  }));
}

async function renderPlaybooks() {
  const d = await (await fetch("/api/playbooks")).json();
  $("#pb-list").innerHTML = d.playbooks.length ? d.playbooks.map((p) => `
    <div class="mem-row" data-site="${esc(p.site)}" data-title="${esc(p.title)}">
      <div class="mem-head"><span class="mem-kind">${esc(p.site)}</span>
        <span class="mem-text-inline">${esc(p.title)}</span>
        <span class="mem-meta">${esc(p.author)} · ${ago(p.ts)} ago</span>
        <span class="mem-actions"><button class="pb-del">delete</button></span></div>
    </div>`).join("") : `<div class="set-hint">No playbooks yet.</div>`;
  $("#pb-list").querySelectorAll(".pb-del").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".mem-row");
    await fetch("/api/playbooks/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ site: row.dataset.site, title: row.dataset.title }) });
    row.remove();
  }));
}
