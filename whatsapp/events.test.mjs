import test from "node:test";
import assert from "node:assert/strict";
import { toEvent, chatAllowed, fileVerdict, storedName, unwrap, bareJid } from "./events.mjs";

const me = { id: "9607770000@s.whatsapp.net" };

function msg(key, message, extra = {}) {
  return { key: { id: "ABC123", ...key }, message, messageTimestamp: 1760950000, ...extra };
}

test("a direct text from a contact", () => {
  const e = toEvent(msg({ remoteJid: "9607771111@s.whatsapp.net" }, { conversation: "Sent 500 for the rent" },
    { pushName: "Ali" }), { me });
  assert.equal(e.kind, "text");
  assert.equal(e.text, "Sent 500 for the rent");
  assert.equal(e.chat, "9607771111@s.whatsapp.net");
  assert.equal(e.sender, e.chat);
  assert.equal(e.senderName, "Ali");
  assert.equal(e.chatName, "Ali");
  assert.equal(e.fromMe, false);
  assert.equal(e.ts, 1760950000);
});

test("a LID chat is recorded under the phone number WhatsApp also gave", () => {
  const e = toEvent(msg({ remoteJid: "123456789012345@lid", remoteJidAlt: "9607772222:3@s.whatsapp.net" },
    { conversation: "hi" }), { me });
  assert.equal(e.chat, "9607772222@s.whatsapp.net");
});

test("a captioned document inside the caption wrapper, with a quote", () => {
  const e = toEvent(msg({ remoteJid: "9607771111@s.whatsapp.net" }, {
    documentWithCaptionMessage: { message: { documentMessage: {
      mimetype: "application/pdf", fileName: "INV-204.pdf", fileLength: 48213, caption: "October invoice",
      contextInfo: { stanzaId: "Q1", quotedMessage: { conversation: "can you send the invoice?" } },
    } } },
  }), { me });
  assert.equal(e.kind, "document");
  assert.equal(e.mime, "application/pdf");
  assert.equal(e.fileName, "INV-204.pdf");
  assert.equal(e.text, "October invoice");
  assert.equal(e.quotedId, "Q1");
  assert.equal(e.quotedText, "can you send the invoice?");
});

test("an image in a disappearing chat is unwrapped", () => {
  const content = unwrap({ ephemeralMessage: { message: { viewOnceMessageV2: { message: {
    imageMessage: { mimetype: "image/jpeg", caption: "slip" } } } } } });
  assert.ok(content.imageMessage);
  const e = toEvent(msg({ remoteJid: "9607771111@s.whatsapp.net" },
    { ephemeralMessage: { message: { imageMessage: { mimetype: "image/jpeg", caption: "slip", fileLength: 90000 } } } }), { me });
  assert.equal(e.kind, "image");
  assert.equal(e.text, "slip");
});

test("in a group the sender is the participant, and the owner's own messages say so", () => {
  const theirs = toEvent(msg({ remoteJid: "120363000000000000@g.us", participant: "9607773333:2@s.whatsapp.net" },
    { conversation: "paid" }, { pushName: "Sara" }), { me, names: { "120363000000000000@g.us": "Shop staff" } });
  assert.equal(theirs.isGroup, true);
  assert.equal(theirs.sender, "9607773333@s.whatsapp.net");
  assert.equal(theirs.senderName, "Sara");
  assert.equal(theirs.chatName, "Shop staff");
  const mine = toEvent(msg({ remoteJid: "9607771111@s.whatsapp.net", fromMe: true }, { conversation: "thanks" }),
    { me, names: { "9607771111@s.whatsapp.net": "Ali" } });
  assert.equal(mine.fromMe, true);
  assert.equal(mine.sender, me.id);
  assert.equal(mine.senderName, "you");
  assert.equal(mine.chatName, "Ali");
});

test("status, channels, stickers, reactions and empty texts are not recorded", () => {
  assert.equal(toEvent(msg({ remoteJid: "status@broadcast" }, { conversation: "story" })), null);
  assert.equal(toEvent(msg({ remoteJid: "1203@newsletter" }, { conversation: "news" })), null);
  assert.equal(toEvent(msg({ remoteJid: "9607771111@s.whatsapp.net" }, { stickerMessage: {} })), null);
  assert.equal(toEvent(msg({ remoteJid: "9607771111@s.whatsapp.net" }, { reactionMessage: { text: "👍" } })), null);
  assert.equal(toEvent(msg({ remoteJid: "9607771111@s.whatsapp.net" }, { conversation: "   " })), null);
  assert.equal(toEvent(msg({ remoteJid: "9607771111@s.whatsapp.net" }, null)), null);
});

test("which chats are recorded", () => {
  const dm = { chat: "9607771111@s.whatsapp.net", chatName: "Ali" };
  const group = { chat: "120363000000000000@g.us", chatName: "Payments" };
  assert.equal(chatAllowed({}, dm), true);
  assert.equal(chatAllowed({}, group), false);
  assert.equal(chatAllowed({ groups: true }, group), true);
  assert.equal(chatAllowed({ include: ["payments"] }, group), true);
  assert.equal(chatAllowed({ include: ["120363000000000000@g.us"] }, group), true);
  assert.equal(chatAllowed({ groups: true, ignore: ["Payments"] }, group), false);
  assert.equal(chatAllowed({ ignore: ["+960 777 1111"] }, dm), false);
  assert.equal(chatAllowed({ ignore: ["ali"] }, dm), false);
  assert.equal(chatAllowed({ ignore: ["11"] }, dm), true, "a short number is not a rule");
});

test("files: what is kept, and the name it is kept under", () => {
  assert.deepEqual(fileVerdict({ kind: "image", size: 1e6 }), { keep: true, reason: "" });
  assert.equal(fileVerdict({ kind: "document", size: 40e6 }).keep, false);
  assert.equal(fileVerdict({ kind: "video", size: 1e6 }).keep, false);
  assert.equal(fileVerdict({ kind: "text" }).keep, false);
  assert.equal(storedName("9607771111@s.whatsapp.net", "3EB0/../x", "application/pdf", "../../etc/passwd"),
    "9607771111-3EB0x.pdf", "a path in the name never reaches the disk path");
  assert.equal(storedName("9607771111@s.whatsapp.net", "ID1", "image/jpeg", ""), "9607771111-ID1.jpg");
  assert.equal(storedName("9607771111@s.whatsapp.net", "ID2", "application/pdf", "INV 204.PDF"), "9607771111-ID2.pdf");
  assert.equal(bareJid("9607771111:7@s.whatsapp.net"), "9607771111@s.whatsapp.net");
});

test("pairing: a retired adv secret is replaced in the QR, and only there", async () => {
  const { withAdvSecret } = await import("./events.mjs");
  const qr = "https://wa.me/settings/linked_devices#2@REF,NOISE,IDENT,OLDSECRET,1";
  assert.equal(withAdvSecret(qr, "NEWSECRET"), "https://wa.me/settings/linked_devices#2@REF,NOISE,IDENT,NEWSECRET,1");
  assert.equal(withAdvSecret("not a qr", "NEW"), "not a qr");
  assert.equal(withAdvSecret(qr, ""), qr);
});

test("pairing: notifications are acked without an account id", async () => {
  const { notificationAck, isRegRefresh } = await import("./events.mjs");
  const node = { tag: "notification", attrs: { id: "N1", from: "s.whatsapp.net", type: "companion_reg_refresh" },
                 content: [{ tag: "companion_reg_refresh", attrs: {} }] };
  assert.deepEqual(notificationAck(node),
    { tag: "ack", attrs: { id: "N1", to: "s.whatsapp.net", class: "notification", type: "companion_reg_refresh" } });
  assert.equal(notificationAck({ tag: "message", attrs: { id: "M", from: "x" } }), null);
  assert.equal(isRegRefresh(node), true);
  assert.equal(isRegRefresh({ ...node, content: [{ tag: "something_else" }] }), false);
  assert.equal(isRegRefresh({ tag: "notification", attrs: { type: "account_sync" }, content: [] }), false);
});
