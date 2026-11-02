// What the bridge makes of one WhatsApp message, with no socket involved, so
// the rules can be tested on plain objects.
//
// A message becomes an event Mission Control stores: who, which chat, when,
// its text, and whether it carries a file worth keeping. Stickers, reactions,
// edits, deletions, polls and status updates are not conversation and are
// dropped here. Which chats are recorded at all is the owner's choice (the
// config Mission Control pushes): direct chats always, groups only when asked,
// and never a chat on the ignore list.

export const FILE_KINDS = new Set(["image", "document", "audio"]);
export const MAX_FILE_BYTES = { image: 25e6, document: 25e6, audio: 8e6 };

const EXTENSIONS = {
  "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic",
  "application/pdf": "pdf", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a",
  "text/csv": "csv", "text/plain": "txt",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
  "application/vnd.ms-excel": "xls",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
};

// The wrappers WhatsApp puts around real content: disappearing messages, view
// once, documents with captions, edits and forwarded templates.
const WRAPPERS = ["ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
  "viewOnceMessageV2Extension", "documentWithCaptionMessage", "editedMessage"];

export function unwrap(message) {
  let content = message || null;
  for (let depth = 0; content && depth < 6; depth += 1) {
    const key = WRAPPERS.find((w) => content[w]?.message);
    if (!key) break;
    content = content[key].message;
  }
  return content;
}

/** user@server with the device suffix gone: 9607771234:12@s.whatsapp.net -> 9607771234@s.whatsapp.net */
export function bareJid(jid) {
  const raw = String(jid || "").trim();
  if (!raw.includes("@")) return raw;
  const [user, server] = raw.split("@", 2);
  return `${user.split(":", 1)[0]}@${server}`;
}

export function isGroup(jid) {
  return String(jid || "").endsWith("@g.us");
}

/** Chats that are never conversation: status updates, broadcasts, channels. */
export function isNoise(jid) {
  const j = String(jid || "");
  return !j || j === "status@broadcast" || j.endsWith("@broadcast") || j.endsWith("@newsletter");
}

/** The phone-number form when WhatsApp gave us one, else what we have. */
export function preferPhone(jid, alt) {
  const a = bareJid(alt);
  return a.endsWith("@s.whatsapp.net") ? a : bareJid(jid);
}

export function number(jid) {
  return String(jid || "").split("@", 1)[0].replace(/\D/g, "");
}

function norm(text) {
  return String(text || "").trim().toLowerCase();
}

/**
 * Should this chat be recorded? config: {groups: bool, include: [], ignore: []}.
 * include and ignore hold chat names, phone numbers or JIDs.
 */
export function chatAllowed(config, { chat, chatName }) {
  const cfg = config || {};
  const name = norm(chatName);
  const digits = number(chat);
  const hit = (rules) => (rules || []).some((rule) => {
    const r = norm(rule);
    if (!r) return false;
    if (r === norm(chat) || (name && r === name)) return true;
    const ruleDigits = r.replace(/\D/g, "");
    return !isGroup(chat) && ruleDigits.length >= 6 && digits && digits.endsWith(ruleDigits);
  });
  if (hit(cfg.ignore)) return false;
  if (!isGroup(chat)) return true;
  return Boolean(cfg.groups) || hit(cfg.include);
}

function contextOf(content) {
  for (const value of Object.values(content || {})) {
    if (value && typeof value === "object" && value.contextInfo) return value.contextInfo;
  }
  return null;
}

/** The part of a message worth recording, or null for everything else. */
export function describe(content) {
  if (!content) return null;
  if (typeof content.conversation === "string") {
    return { kind: "text", text: content.conversation };
  }
  if (content.extendedTextMessage) {
    return { kind: "text", text: content.extendedTextMessage.text || "" };
  }
  if (content.imageMessage) {
    const m = content.imageMessage;
    return { kind: "image", text: m.caption || "", mime: m.mimetype || "image/jpeg",
             size: Number(m.fileLength || 0), media: "imageMessage" };
  }
  if (content.documentMessage) {
    const m = content.documentMessage;
    return { kind: "document", text: m.caption || "", mime: m.mimetype || "application/octet-stream",
             size: Number(m.fileLength || 0), fileName: m.fileName || m.title || "", media: "documentMessage" };
  }
  if (content.audioMessage) {
    const m = content.audioMessage;
    return { kind: "audio", text: "", mime: (m.mimetype || "audio/ogg").split(";")[0],
             size: Number(m.fileLength || 0), voice: Boolean(m.ptt), media: "audioMessage" };
  }
  if (content.videoMessage) {
    const m = content.videoMessage;
    return { kind: "video", text: m.caption || "", mime: m.mimetype || "video/mp4",
             size: Number(m.fileLength || 0) };
  }
  if (content.locationMessage) {
    const m = content.locationMessage;
    const where = [m.name, m.address].filter(Boolean).join(", ");
    return { kind: "location", text: `${where ? where + " " : ""}(${m.degreesLatitude}, ${m.degreesLongitude})` };
  }
  if (content.contactMessage) {
    return { kind: "contact", text: content.contactMessage.displayName || "a contact card" };
  }
  return null;
}

export function extension(mime, fileName) {
  const fromName = String(fileName || "").match(/\.([A-Za-z0-9]{1,8})$/);
  if (fromName) return fromName[1].toLowerCase();
  return EXTENSIONS[String(mime || "").split(";")[0].trim()] || "bin";
}

/** A file name safe on any disk, unique per chat and message. */
export function storedName(chat, id, mime, fileName) {
  const safe = (s) => String(s || "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40) || "x";
  return `${safe(number(chat) || chat)}-${safe(id)}.${extension(mime, fileName)}`;
}

/** Does this file get downloaded, and if not, why not. */
export function fileVerdict(described) {
  if (!described || !FILE_KINDS.has(described.kind)) return { keep: false, reason: "" };
  const cap = MAX_FILE_BYTES[described.kind];
  if (described.size && described.size > cap) {
    return { keep: false, reason: `larger than ${Math.round(cap / 1e6)} MB, not downloaded` };
  }
  return { keep: true, reason: "" };
}

/**
 * The event for one upserted message, before any file is downloaded, or null
 * when it is not something to record. names: {jid: name} learned so far.
 */
export function toEvent(msg, { names = {}, me = null } = {}) {
  const key = msg?.key || {};
  const chatRaw = key.remoteJid || "";
  if (!msg?.message || !key.id || isNoise(chatRaw)) return null;
  const content = unwrap(msg.message);
  const described = describe(content);
  if (!described) return null;
  const group = isGroup(chatRaw);
  const chat = group ? bareJid(chatRaw) : preferPhone(chatRaw, key.remoteJidAlt);
  const fromMe = Boolean(key.fromMe);
  const sender = fromMe ? bareJid(me?.id || "me") : group ? preferPhone(key.participant, key.participantAlt) : chat;
  const senderName = fromMe ? "you" : (msg.pushName || names[sender] || names[bareJid(key.participant)] || "");
  const chatName = group ? (names[chat] || "") : (fromMe ? names[chat] || "" : senderName || names[chat] || "");
  const ctx = contextOf(content);
  const quoted = ctx?.quotedMessage ? describe(unwrap(ctx.quotedMessage)) : null;
  const text = String(described.text || "").trim();
  if (described.kind === "text" && !text) return null;
  const ts = Number(msg.messageTimestamp?.low ?? msg.messageTimestamp ?? 0) || Math.floor(Date.now() / 1000);
  return {
    id: key.id, chat, chatName, isGroup: group, sender, senderName, fromMe, ts,
    kind: described.kind, text, mime: described.mime || "", size: described.size || 0,
    fileName: described.fileName || "", voice: Boolean(described.voice),
    quotedId: ctx?.stanzaId || "", quotedText: quoted ? (quoted.text || `[${quoted.kind}]`).slice(0, 300) : "",
    forwarded: Boolean(ctx?.isForwarded),
  };
}

// ---------- pairing ----------
//
// Linking a new device broke on WhatsApp's side in mid-2026 in ways Baileys
// 7.0.0-rc14 does not handle yet (Baileys issues #2679 and #2737, PRs #2749 and
// #2765). These two helpers are the bridge's stand-ins until a release does.

/**
 * A pairing QR with a different adv secret. The QR payload is
 * `...#ref,noiseKey,identityKey,advSecret,platform`; Baileys captures the secret
 * once per pairing, so after WhatsApp asks for a new one every QR it emits
 * still carries the retired value until this rewrites it.
 */
export function withAdvSecret(qr, adv) {
  const parts = String(qr || "").split(",");
  if (parts.length < 5 || !adv) return qr;
  parts[parts.length - 2] = adv;
  return parts.join(",");
}

/** The ack WhatsApp expects for a notification, which Baileys cannot build before
 * the device has an account id (it reads creds.me.id, which does not exist yet). */
export function notificationAck(node) {
  const attrs = node?.attrs || {};
  if (node?.tag !== "notification" || !attrs.id || !attrs.from) return null;
  const ack = { tag: "ack", attrs: { id: attrs.id, to: attrs.from, class: "notification" } };
  for (const key of ["participant", "recipient", "type"]) {
    if (attrs[key]) ack.attrs[key] = attrs[key];
  }
  return ack;
}

/** True for WhatsApp retiring an unpaired device's registration secret. */
export function isRegRefresh(node) {
  const children = Array.isArray(node?.content) ? node.content.map((c) => c?.tag) : [];
  return node?.attrs?.type === "companion_reg_refresh"
    && children.some((tag) => tag === "companion_reg_refresh" || tag === "pair-device-rotate-qr");
}
