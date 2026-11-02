# WhatsApp

Mission Control can read your own WhatsApp: the messages and files people send
you, and what you send. Agents use it to answer questions about your chats.

It is read-only. Nothing in Mission Control can send a WhatsApp message, reply,
react, mark a chat as read or show you as online, so your phone keeps its
notifications and your contacts never hear from an agent. To talk to your agents
over WhatsApp, use the Business API channel in Settings > Channels instead.

## How it connects

The same way [Hermes Agent](https://github.com/NousResearch/hermes-agent) does: a
small Node service in `whatsapp/` speaks the WhatsApp Web protocol through
[Baileys](https://github.com/WhiskeySockets/Baileys), and you link it from your
phone like any browser. There is no Meta business account and no public webhook.
The service listens on 127.0.0.1 only and refuses requests whose Host header is
not a loopback name.

WhatsApp does not officially support third-party clients. A client that only
listens behaves like WhatsApp Web left open and is unlikely to be flagged, but
the risk is not zero, and a WhatsApp protocol change can break the bridge until
Baileys catches up (`npm update` in `whatsapp/`).

## Setting it up

1. Install the bridge once (Node.js 20 or newer):

   ```sh
   cd whatsapp && npm install
   ```

2. Settings > Integrations > **WhatsApp**: switch it on, press **Save**, then
   **Link**.
3. On your phone: WhatsApp > Settings > Linked devices > Link a device, and scan
   the code. When the phone is the only screen you have, enter your number
   under the code and choose "Link with phone number instead" on the phone.

The card then shows "Linked as +960…". If you remove the device from the phone's
Linked devices, the session is forgotten and the card offers **Link** again.

### Linking needed a few workarounds (October 2026)

Baileys 7.0.0-rc14 cannot link a new device on its own any more, and the bridge
covers three open upstream issues: it claims the WhatsApp Web version that
web.whatsapp.com reports, not Baileys' stale default ([#2679](https://github.com/WhiskeySockets/Baileys/issues/2679));
it acknowledges WhatsApp's notifications before the device has an account id
([#2749](https://github.com/WhiskeySockets/Baileys/pull/2749)); and when WhatsApp
retires the pairing secret during a scan it rotates it and refreshes the QR
([#2737](https://github.com/WhiskeySockets/Baileys/issues/2737)). The first scan can
still fail on the phone; scan the refreshed code again. Linking with a phone-number
code has its own open bug, so use the QR.

## History

WhatsApp sends a linked device its history once, at the moment of linking. New
links present as a desktop and ask for the full history, as WhatsApp Desktop does
(the phone then lists the device as "Mac OS"); a session keeps the identity it was
linked with, because WhatsApp drops one that comes back as something else. History
is stored for the chats you record. Its photos and files are not downloaded up
front: each keeps what it takes to fetch it, and it is fetched from the phone when
an agent needs it. A file the phone no longer has says so.

## What is recorded

- Every direct chat, including your own "Message yourself" chat, which makes a
  handy inbox.
- Group chats only when **Group chats** is on, or the group is named in
  **Include groups**.
- Nothing from a chat in **Never record** (names or phone numbers), and never
  status updates, channels, stickers or reactions.
- Photos, documents up to 25 MB and voice notes up to 8 MB are kept in
  `whatsapp_media/`. Videos are noted but not downloaded.
- Messages and files are forgotten after `keep_days` (90 by default).

The session keys live in `secrets/whatsapp-session/`. Anyone with that folder can
read your WhatsApp, so it is never committed or synced.

**Agents who can read** limits the `whatsapp` tool to some agents; blank means all.

## The whatsapp tool

`status`, `chats`, `read` (a chat by name or number, or `around` one message),
`search` and `view` (a photo comes back as an image, a PDF as its text, a voice note
transcribed when an OpenAI key is set). Every message
reaches the model labelled as someone else's words, never instructions.

## Files and arguments in the mcp tool

Before a call is validated, its arguments are fitted to the types the tool declares
where that is unambiguous: `1180` becomes `"1180"` for a string amount, `"true"`
becomes `true`, a list sent as JSON text or wrapped as `{"item": [...]}` becomes the
list. Models get these wrong constantly, and each one used to be a failed call.

The `mcp` tool takes `file` for any server tool with a `file_base64` input:
`whatsapp:<id>`, or a file sent into the chat. The call carries the reference
until the moment it is sent, so the Inbox, when approval is needed, and the
action ledger show `mc-file:whatsapp:42`, not megabytes of base64. No other file
on the Mac can be sent this way. A reference an agent puts inside the tool's own
arguments instead (`"file_path": "whatsapp:42"`) is treated the same way.

## Settings

```yaml
whatsapp_link:
  enabled: true
  groups: false
  include: [Family]          # groups recorded even with groups off
  ignore: [Mum, "+960 7xx xxxx"]
  agents: null               # or [cto]
  keep_days: 90
  port: 8936
  node: ""                   # path to Node.js 20+; found automatically when empty
```

The bridge logs to `whatsapp.log` in the data folder.
