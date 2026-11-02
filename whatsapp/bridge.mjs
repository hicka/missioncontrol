// Read-only WhatsApp bridge: the owner's own account, as a linked device.
//
// The approach Hermes Agent (Nous Research) uses: Baileys speaks the WhatsApp
// Web protocol, so the owner links this Mac from WhatsApp > Linked devices the
// way they would link a browser, by QR code or by an 8-character code, and no
// Meta business account or public webhook is involved. Mission Control runs it
// as a child process and talks to it over loopback HTTP.
//
// Unlike Hermes, nothing here can send. There is no endpoint that writes to a
// chat, sends a read receipt or shows the account online, so agents can read
// what arrives but can never speak as the owner, and the phone keeps its
// notifications. That is also what keeps the account's risk of being flagged
// low: an unofficial client that only listens looks like WhatsApp Web left open.
//
// Endpoints (127.0.0.1 only, and only for a loopback Host header):
//   GET  /health            state, linked account, queue length
//   GET  /qr                the current pairing QR as text and SVG, while pairing
//   GET  /messages?after=N  events after sequence N; everything up to N is dropped
//   POST /link {phone?}     start pairing: a QR, or a code for that phone number
//   POST /unlink            remove this device from the account and forget the session
//   POST /config            {groups, include, ignore}: which chats are recorded
//   POST /download {raw}    fetch the file of a message from history (stored as `raw`)
//   GET  /groups            the groups this account is in, by name
import http from "node:http";
import { mkdirSync, rmSync, writeFileSync, readFileSync } from "node:fs";
import path from "node:path";
import { randomBytes } from "node:crypto";
import pino from "pino";
import QRCode from "qrcode";
import {
  makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion,
  fetchLatestWaWebVersion, downloadMediaMessage, Browsers, proto,
} from "@whiskeysockets/baileys";
import {
  toEvent, chatAllowed, fileVerdict, storedName, bareJid, number,
  withAdvSecret, notificationAck, isRegRefresh,
} from "./events.mjs";

const HOST = process.env.WA_HOST || "127.0.0.1";
const PORT = Number(process.env.WA_PORT || 8936);
const SESSION_DIR = process.env.WA_SESSION_DIR || path.resolve("session");
const MEDIA_DIR = process.env.WA_MEDIA_DIR || path.resolve("media");
// A full history sync can bring tens of thousands of messages at once.
const MAX_QUEUE = 100_000;
const MAX_BODY = 64 * 1024;
const BOOT = randomBytes(6).toString("hex");
const STARTED = Math.floor(Date.now() / 1000);
const LOOPBACK = new Set(["127.0.0.1", "localhost", "::1"]);

let config = { groups: false, include: [], ignore: [] };
try { config = { ...config, ...JSON.parse(process.env.WA_CONFIG || "{}") }; } catch {}

const logger = pino({ level: process.env.WA_LOG_LEVEL || "error" });
let sock = null;
let state = "unlinked";      // unlinked | pairing | connecting | connected | error
let problem = "";
let qr = "";
let me = null;
let seq = 0;
let queue = [];
let retry = 0;
let reconnectTimer = null;
const names = {};             // jid -> the best name we know
const groupNames = new Map();  // jid -> subject, fetched once

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

function registered() {
  try {
    const creds = JSON.parse(readFileSync(path.join(SESSION_DIR, "creds.json"), "utf8"));
    return Boolean(creds.registered || (creds.me?.id && creds.account));
  } catch {
    return false;
  }
}

function learnName(jid, name) {
  if (jid && name && name !== "~") names[bareJid(jid)] = name;
}

async function groupName(socket, jid) {
  if (groupNames.has(jid)) return groupNames.get(jid);
  let subject = "";
  try {
    subject = (await socket.groupMetadata(jid))?.subject || "";
  } catch {
    return "";               // not cached: the next message tries again
  }
  groupNames.set(jid, subject);
  if (subject) learnName(jid, subject);
  return subject;
}

function enqueue(event) {
  seq += 1;
  queue.push({ seq, ...event });
  if (queue.length > MAX_QUEUE) {
    queue.splice(0, queue.length - MAX_QUEUE);
    log(`queue over ${MAX_QUEUE}: oldest events dropped (is Mission Control polling?)`);
  }
}

async function saveFile(socket, msg, event) {
  const verdict = fileVerdict(event);
  if (!verdict.keep) return verdict.reason ? { fileError: verdict.reason } : {};
  try {
    // An old file has usually left WhatsApp's servers; reuploadRequest asks the
    // phone to send it again, which works while the phone still has it.
    const data = await downloadMediaMessage(msg, "buffer", {},
      { logger, reuploadRequest: socket.updateMediaMessage });
    const month = new Date(event.ts * 1000).toISOString().slice(0, 7);
    const rel = path.join(month, storedName(event.chat, event.id, event.mime, event.fileName));
    mkdirSync(path.join(MEDIA_DIR, month), { recursive: true });
    writeFileSync(path.join(MEDIA_DIR, rel), data, { mode: 0o600 });
    return { file: rel, size: data.length };
  } catch (err) {
    return { fileError: `download failed: ${err?.message || err}` };
  }
}

async function onMessages(socket, { messages, type }) {
  for (const msg of messages || []) {
    if (msg.pushName && !msg.key?.fromMe) {
      learnName(msg.key.participantAlt || msg.key.remoteJidAlt || msg.key.participant || msg.key.remoteJid, msg.pushName);
    }
    const event = toEvent(msg, { names, me });
    if (!event) continue;
    // "append" also carries older messages WhatsApp replays on reconnect; only
    // what was sent while the bridge was up counts. "notify" includes what
    // queued at WhatsApp while the Mac was off, which is exactly what we want.
    if (type === "append" && event.ts < STARTED - 300) continue;
    if (event.isGroup) {
      // Groups are off and none is named: no need to ask WhatsApp what it is called.
      if (!config.groups && !(config.include || []).length) continue;
      event.chatName = (await groupName(socket, event.chat)) || event.chatName;
    }
    if (!chatAllowed(config, event)) continue;
    Object.assign(event, await saveFile(socket, msg, event));
    enqueue(event);
  }
}

/** A history message, kept small enough to fetch its file later: no thumbnails. */
function rawMessage(msg) {
  try {
    const plain = proto.WebMessageInfo.fromObject(msg).toJSON();
    const strip = (o) => {
      if (!o || typeof o !== "object") return;
      delete o.jpegThumbnail;
      Object.values(o).forEach(strip);
    };
    strip(plain.message);
    return JSON.stringify({ key: plain.key, message: plain.message, messageTimestamp: plain.messageTimestamp });
  } catch {
    return "";
  }
}

// History arrives once, right after linking (the full history, as WhatsApp
// Desktop gets it), then in chunks over several minutes. Its files are not
// downloaded now: most are old photos nobody will ask for, and an old file
// has to be re-sent by the phone. Each keeps what POST /download needs.
async function onHistory(socket, { chats, contacts, messages, syncType, progress }) {
  for (const c of chats || []) {
    if (c.id && c.name) {
      learnName(c.id, c.name);
      if (c.id.endsWith("@g.us")) groupNames.set(bareJid(c.id), c.name);
    }
  }
  (contacts || []).forEach((c) => learnName(c.phoneNumber || c.id, c.name || c.notify));
  let kept = 0;
  for (const msg of messages || []) {
    const event = toEvent(msg, { names, me });
    if (!event) continue;
    if (event.isGroup) {
      if (!config.groups && !(config.include || []).length) continue;
      event.chatName = groupNames.get(event.chat) || event.chatName;
    }
    if (!chatAllowed(config, event)) continue;
    event.history = true;
    if (fileVerdict(event).keep) event.raw = rawMessage(msg);
    enqueue(event);
    kept += 1;
  }
  log(`history (type ${syncType ?? "?"}${progress != null ? `, ${progress}%` : ""}): kept ${kept} of ${(messages || []).length} messages`);
}

function scheduleReconnect(ms) {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => startSocket().catch((err) => {
    problem = String(err?.message || err);
    log("start failed:", problem);
    scheduleReconnect(Math.min(300_000, 5000 * 2 ** retry++));
  }), ms);
}

// The WhatsApp Web version to claim. Baileys' own helper reads a file in its
// repository that went stale in March 2026, and WhatsApp refuses to finish
// linking a device that claims it (Baileys #2679); web.whatsapp.com itself says
// what is current.
async function waVersion() {
  for (const [source, fetcher] of [["web.whatsapp.com", fetchLatestWaWebVersion], ["Baileys", fetchLatestBaileysVersion]]) {
    try {
      const got = await Promise.race([fetcher({}),
        new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 8000))]);
      if (got?.version && !got.error) {
        log(`claiming WhatsApp Web ${got.version.join(".")} (from ${source})`);
        return got.version;
      }
    } catch {}
  }
  log("could not learn the current WhatsApp Web version; using Baileys' default");
  return undefined;
}

// A linked device must keep presenting itself the way it was linked: WhatsApp
// ends the connection (428) of a session that comes back as something else.
// New links present as a desktop, which is what gets the full history; a
// session linked before that was a Chrome browser and stays one.
const LEGACY_BROWSER = ["Mission Control", "Chrome", "120.0"];
const BROWSER_FILE = () => path.join(SESSION_DIR, "browser.json");

function sessionBrowser() {
  if (!registered()) {
    const desktop = Browsers.macOS("Desktop");
    try { writeFileSync(BROWSER_FILE(), JSON.stringify(desktop), { mode: 0o600 }); } catch {}
    return desktop;
  }
  try {
    const saved = JSON.parse(readFileSync(BROWSER_FILE(), "utf8"));
    if (Array.isArray(saved) && saved.length === 3) return saved;
  } catch {}
  return LEGACY_BROWSER;
}

function forgetSession() {
  try { rmSync(SESSION_DIR, { recursive: true, force: true }); } catch {}
  mkdirSync(SESSION_DIR, { recursive: true, mode: 0o700 });
  me = null;
}

async function startSocket() {
  clearTimeout(reconnectTimer);
  if (sock) {
    try { sock.end(undefined); } catch {}
    sock = null;
  }
  mkdirSync(SESSION_DIR, { recursive: true, mode: 0o700 });
  const { state: auth, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const version = await waVersion();
  const browser = sessionBrowser();
  state = registered() ? "connecting" : "pairing";
  const socket = makeWASocket({
    ...(version ? { version } : {}),
    auth, logger,
    // A desktop that asks for the full history gets it, as WhatsApp Desktop does;
    // this only matters when a device is linked.
    browser,
    syncFullHistory: browser[1] === "Desktop",
    shouldSyncHistoryMessage: () => true,
    markOnlineOnConnect: false,
    // Incoming messages that need the encryption session re-established are
    // dropped without this; we keep no store, and a placeholder is enough.
    getMessage: async () => ({ conversation: "" }),
  });
  sock = socket;
  socket.ev.on("creds.update", saveCreds);
  // Before the device is paired Baileys cannot acknowledge a notification
  // (Baileys #2749), and WhatsApp's pairing notifications go unanswered.
  socket.ws.on("CB:notification", (node) => {
    if (sock !== socket || auth.creds.me?.id) return;
    const ack = notificationAck(node);
    if (ack) socket.sendNode(ack).catch((err) => log("pre-login ack failed:", err?.message || err));
  });
  // WhatsApp now retires an unpaired device's adv secret during pairing, and a
  // QR that still shows the old one fails on the phone with "couldn't link
  // device" (Baileys #2737). Rotate it the way WhatsApp Web does and show a QR
  // that carries the new one. A pending pairing code is verified against the
  // secret it was issued with, so that flow keeps it.
  socket.ws.on("CB:notification,type:companion_reg_refresh", (node) => {
    if (sock !== socket || auth.creds.me || !isRegRefresh(node)) return;
    auth.creds.advSecretKey = randomBytes(32).toString("base64");
    saveCreds();
    if (qr) qr = withAdvSecret(qr, auth.creds.advSecretKey);
    log("WhatsApp retired the pairing secret; a new one is in the QR");
  });
  socket.ev.on("contacts.upsert", (rows) => rows.forEach((c) => learnName(c.phoneNumber || c.id, c.name || c.notify || c.verifiedName)));
  socket.ev.on("contacts.update", (rows) => rows.forEach((c) => learnName(c.phoneNumber || c.id, c.name || c.notify)));
  socket.ev.on("messaging-history.set", (batch) => {
    if (sock !== socket) return;
    onHistory(socket, batch).catch((err) => log("history handling failed:", err?.message || err));
  });
  socket.ev.on("groups.update", (rows) => rows.forEach((g) => { if (g.id && g.subject) { groupNames.set(g.id, g.subject); learnName(g.id, g.subject); } }));
  socket.ev.on("messages.upsert", (batch) => {
    if (sock !== socket) return;
    onMessages(socket, batch).catch((err) => log("message handling failed:", err?.message || err));
  });
  socket.ev.on("connection.update", (update) => {
    if (sock !== socket) return;
    const { connection, lastDisconnect } = update;
    if (update.qr) {
      // Baileys builds each QR with the secret it saw when pairing began.
      qr = withAdvSecret(update.qr, auth.creds.advSecretKey);
      state = "pairing";
    }
    if (update.isNewLogin) log("paired; WhatsApp restarts the connection next");
    if (connection === "open") {
      state = "connected";
      problem = "";
      qr = "";
      retry = 0;
      me = { id: bareJid(socket.user?.id), lid: bareJid(socket.user?.lid), name: socket.user?.name || "" };
      log(`connected as ${number(me.id) || me.id}`);
    }
    if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode;
      log(`connection closed: ${code ?? "no code"} ${lastDisconnect?.error?.message || ""}`.trim());
      qr = "";
      if (code === DisconnectReason.loggedOut) {
        // Removed from the phone's Linked devices: the session is worthless now.
        forgetSession();
        state = "unlinked";
        problem = "This Mac was removed from the account's linked devices.";
        log("logged out; session forgotten");
      } else if (code === DisconnectReason.connectionReplaced) {
        state = "error";
        problem = "Another copy of this bridge took over the session.";
        log(problem);
      } else if (!registered() && code !== DisconnectReason.restartRequired) {
        // A QR that nobody scanned in time: stop until the owner asks again.
        state = "unlinked";
        problem = code === DisconnectReason.timedOut ? "The pairing code expired before it was used." : `Pairing stopped (${code ?? "closed"}).`;
        log(problem);
      } else {
        // 515 right after pairing is WhatsApp asking for exactly this.
        state = "connecting";
        const wait = code === DisconnectReason.restartRequired ? 500 : Math.min(300_000, 3000 * 2 ** retry++);
        log(`reconnecting in ${Math.round(wait / 1000)}s`);
        scheduleReconnect(wait);
      }
    }
  });
  return socket;
}

async function link(phone) {
  if (state === "connected") return { ok: true, state, me };
  const digits = String(phone || "").replace(/\D/g, "");
  if (phone && digits.length < 8) return { ok: false, error: "the phone number needs its country code, digits only" };
  problem = "";
  if (registered()) {
    // Already linked, just not connected: reconnect rather than pair again.
    if (!sock || state === "error") await startSocket();
    return { ok: true, state };
  }
  if (!sock || state !== "pairing") {
    forgetSession();
    await startSocket();
  }
  if (!digits) return { ok: true, state };
  // The socket has to be talking to WhatsApp before it can ask for a code.
  for (let i = 0; i < 40 && !qr; i += 1) await new Promise((r) => setTimeout(r, 250));
  try {
    const code = await sock.requestPairingCode(digits);
    return { ok: true, state, code: code.match(/.{1,4}/g).join("-") };
  } catch (err) {
    return { ok: false, error: `could not get a pairing code: ${err?.message || err}` };
  }
}

async function unlink() {
  clearTimeout(reconnectTimer);
  const socket = sock;
  sock = null;
  if (socket) {
    try { if (state === "connected") await socket.logout(); } catch (err) { log("logout:", err?.message || err); }
    try { socket.end(undefined); } catch {}
  }
  forgetSession();
  state = "unlinked";
  problem = "";
  qr = "";
  return { ok: true, state };
}

// ---------- HTTP ----------

function send(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY) { reject(Object.assign(new Error("body too large"), { status: 413 })); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      try { resolve(raw ? JSON.parse(raw) : {}); } catch { reject(Object.assign(new Error("invalid JSON"), { status: 400 })); }
    });
    req.on("error", reject);
  });
}

function loopbackHost(req) {
  // A web page cannot reach this through a rebinding DNS name: the Host header gives it away.
  const host = String(req.headers.host || "").replace(/:\d+$/, "").replace(/^\[|\]$/g, "").toLowerCase();
  return LOOPBACK.has(host);
}

const server = http.createServer(async (req, res) => {
  if (!loopbackHost(req)) return send(res, 400, { error: "loopback hosts only" });
  const url = new URL(req.url, "http://127.0.0.1");
  try {
    if (req.method === "GET" && url.pathname === "/health") {
      return send(res, 200, { status: state, problem, boot: BOOT, me, number: me ? number(me.id) : "",
                              pairing: Boolean(qr), queue: queue.length, linked: registered() });
    }
    if (req.method === "GET" && url.pathname === "/qr") {
      if (!qr) return send(res, 404, { error: "not pairing", status: state });
      const svg = await QRCode.toString(qr, { type: "svg", margin: 1 });
      return send(res, 200, { qr, svg });
    }
    if (req.method === "GET" && url.pathname === "/messages") {
      const after = Number(url.searchParams.get("after") || 0);
      // The caller has stored everything up to `after`: drop it, return the rest.
      if (after > 0) queue = queue.filter((e) => e.seq > after);
      return send(res, 200, { boot: BOOT, events: queue.slice(0, 500) });
    }
    if (req.method === "POST" && url.pathname === "/config") {
      const body = await readBody(req);
      config = {
        groups: Boolean(body.groups),
        include: Array.isArray(body.include) ? body.include.map(String) : [],
        ignore: Array.isArray(body.ignore) ? body.ignore.map(String) : [],
      };
      return send(res, 200, { ok: true, config });
    }
    if (req.method === "GET" && url.pathname === "/groups") {
      // The groups this account is in, so the owner can name the ones to record.
      if (!sock || state !== "connected") return send(res, 503, { error: "not connected to WhatsApp" });
      const all = await sock.groupFetchAllParticipating();
      const groups = Object.values(all || {}).map((g) => ({ id: g.id, name: g.subject || "", size: (g.participants || []).length }));
      groups.forEach((g) => { if (g.name) groupNames.set(g.id, g.name); });
      return send(res, 200, { groups: groups.sort((a, b) => a.name.localeCompare(b.name)) });
    }
    if (req.method === "POST" && url.pathname === "/download") {
      const body = await readBody(req);
      if (!sock || state !== "connected") return send(res, 503, { error: "not connected to WhatsApp" });
      let msg;
      try {
        msg = proto.WebMessageInfo.fromObject(JSON.parse(String(body.raw || "")));
      } catch {
        return send(res, 400, { error: "raw is not a stored message" });
      }
      const event = toEvent(msg, { names, me });
      if (!event) return send(res, 400, { error: "that message has no file" });
      const got = await saveFile(sock, msg, event);
      return send(res, got.file ? 200 : 502, got.file ? got : { error: got.fileError || "no file" });
    }
    if (req.method === "POST" && url.pathname === "/link") {
      const body = await readBody(req);
      return send(res, 200, await link(body.phone));
    }
    if (req.method === "POST" && url.pathname === "/unlink") {
      return send(res, 200, await unlink());
    }
    return send(res, 404, { error: "not found" });
  } catch (err) {
    return send(res, err.status || 500, { error: String(err?.message || err) });
  }
});

server.listen(PORT, HOST, () => {
  log(`whatsapp bridge on http://${HOST}:${PORT}; session ${SESSION_DIR}`);
  mkdirSync(MEDIA_DIR, { recursive: true, mode: 0o700 });
  if (registered()) scheduleReconnect(0);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    try { sock?.end(undefined); } catch {}
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 2000).unref();
  });
}
