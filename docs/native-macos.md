# Native macOS application

Mission Control on the Mac is a native Swift application: SwiftUI for the whole workspace
(home, inbox, tasks, activity, schedule, knowledge, evolution, settings and chat), AppKit
for the windows and menu bar, and Metal for the floating bar. There is no web view. The
agent engine is the same Python server as every other install, frozen into the app with
its dependencies and Chromium and talked to over loopback, so running the built app needs
no virtual environment and no separate browser.

Everything in the window is drawn from a local copy of the server's state. Live events keep
it current, parsing happens off the main thread, and the last state is kept on disk, so the
window has something to show the moment it opens. What came from that disk snapshot can be
looked at but not decided on: decision buttons wait for the first live read.

## Build and launch

Requires macOS 14+, Xcode command line tools, and Python 3.11+ for building:

```sh
./venv/bin/pip install -r requirements-desktop.txt
./venv/bin/python scripts/build_macos.py
open "dist/native/Mission Control.app"
```

The build targets the Mac's architecture. Build on Intel for an Intel app, or
on Apple Silicon for an arm64 app. `--skip-runtime` reuses the frozen backend
when only Swift files changed. Chromium is copied from the matching Playwright
installation (downloaded on build if missing); `--without-browser` skips it for
development builds. `--no-runtime --debug` builds just the app in under a minute, for
working on the interface against a running server. The build stages and verifies the app
before replacing the previous output. Only example configs, static assets and starter
skills are packaged; local `.env`, databases, conversations and settings are not.

The default signature is ad hoc for local development. Releases are signed and notarized by
the [build workflow](../.github/workflows/build.yml). Copy the app to Applications before
enabling launch at login.

## Using a runtime it did not start

If a Mission Control server already answers on the app's port, or a
`com.missioncontrol` LaunchAgent exists, the app attaches to that server instead of starting
its own. It uses it fully but never starts or stops it, and Runtime Manager says so. This
is how a source checkout gets the native app: the server keeps running from source under
launchd and the app is only its interface. To use a server elsewhere:

```sh
"Mission Control.app/Contents/MacOS/MissionControl" --attach http://100.x.y.z:8899 --token YOUR_TOKEN
```

Process and service controls stay with the Mac the server runs on.

## The floating bar

An always-on bar sits at the bottom of the screen, on every Space and over full-screen
apps. It says what the agent is doing, and it is the quickest way to talk to it.

| | |
| --- | --- |
| `⌥Space` | Open or close the bar, without leaving the app you are in |
| Hold `⌥Space` | Talk. Let go to send; the reply is read back |
| `Esc` | Back out of what is open |
| Click, or drag | Open it, or move it; near the centre it snaps there |
| View > Show Floating Bar, `⌘B` | Hide or show it |

It shows at a glance:

- **The orb and the edge light** move with the agent. Each word of a reply lands as a kick
  of light, a voice bends the orb, and colour says what kind of thing is happening: blue and
  violet thinking, teal running a tool, amber waiting on you.
- **One small light in orbit for each running task,** in that profile's colour. A task that
  ends falls into the orb and sends a ripple along the bar.
- **The reply as it is written** runs through the bar, and a finished one buds off as a card
  you can read without opening anything.
- **Idle, it folds down to just the orb,** and opens up as the pointer comes near.

A held command can be approved from the bar only after it is shown in full, with what
changed since a near-identical one was answered, exactly as in the inbox.

The body of the bar is one Metal fragment shader (`native/macos/BarAura.metal`) driven by
spring physics on its own display link (`BarAura.swift`). None of that motion goes through
SwiftUI, which only lays out the text on top. It draws at 60 frames a second while something
is moving, 30 while something is going on, and about 12 when quiet, and stops when it cannot
be seen. The window only takes the mouse over the shape itself, so the light around the bar
never blocks a click on what is underneath. Reduce Motion is respected.

Speech is recognized on this Mac where the system supports it. Where it does not, macOS
sends the audio to Apple, as dictation does. The shortcut uses Carbon hot keys, so none of
this needs Accessibility permission; the microphone and speech recognition ask when first
used.

## Updates

**The signed app updates itself,** with [Sparkle](https://sparkle-project.org). It looks for
a new release hourly, downloads it in the background (a delta where it can, so a small
change is a small download), checks both the Apple signature and an EdDSA signature against
the key built into the app, and installs once no agent or task is running. The database is
copied first, as `missioncontrol.db-before-<version>`, keeping the last two. The menu bar
host is separate from the engine, so a release whose engine fails to start can still be
fixed by the next release.

**A release is a tag someone pushed on purpose.** Self-repair pushes to master on its own, so
nothing reaches installed apps from a plain push. To ship:

```sh
git tag -a v1.3.0 -m "v1.3.0" && git push origin v1.3.0
```

The workflow tests, builds, signs, notarizes, writes the feed, and only then publishes, so the
feed never points at a half-finished release. It needs these on a `release` environment in
the repository:

| Kind | Name | What |
| --- | --- | --- |
| Secret | `MACOS_CERTIFICATE`, `MACOS_CERTIFICATE_PASSWORD` | Developer ID Application certificate, as base64 of a `.p12`, and its password |
| Secret | `NOTARY_KEY` | App Store Connect API key (the `.p8` text) for notarization |
| Secret | `SPARKLE_PRIVATE_KEY` | Sparkle's private EdDSA key, from `generate_keys` |
| Variable | `MACOS_SIGNING_IDENTITY` | The certificate's full name |
| Variable | `NOTARY_KEY_ID`, `NOTARY_ISSUER` | The API key's ids |
| Variable | `SPARKLE_PUBLIC_KEY` | The public half, built into the app |

`scripts/setup_release.sh --notary-key AuthKey_<id>.p8 --issuer <issuer id>` sets all of
them from the Mac that holds the certificate. It creates the environment open to `v*` tags
only, exports just the Developer ID identity (macOS asks first), checks the notary key with
`notarytool`, and makes Sparkle's key in the login keychain. Secrets go straight to GitHub
and nothing is written to the checkout. Run it again at any time: what is already set stays.

Losing the private key strands every installed copy on its last version, so back it up. Anyone
holding it and the certificate can ship code to every install.

**A source checkout** can follow GitHub too, under Settings > Updates or `updates:` in
`settings.yaml`. It is off by default. It only ever fast-forwards a clean master, waits until
nothing is running, installs changed Python requirements first, copies the database, and
restarts behind the rollback watchdog. A version that does not come back up is put back and
not tried again. It follows tagged releases unless told to follow master, and it needs the
LaunchAgent, because without it nothing can restart the server onto the new code. A change
to the Node packages is left for you to install.

## One owner for background work

The native host starts one runtime child and validates its PID and random
instance identity on every connection. The workspace opens only after that
check passes. The host and menu bar are the same process. Closing the window
keeps the host alive by default; change that in **Runtime Manager → Background
& permissions**. Quit stops services, tools and the owned runtime.

The backend watches its parent relationship and shuts down if the host exits.
Normal shutdown cancels service tasks, closes listeners and stops tool process
groups. The host gives shutdown eight seconds before force-stopping an
unresponsive runtime, then reaps surviving tools bearing that instance identity.
Unexpected runtime failures have at most three automatic recovery attempts.
Configuration conflicts require an explicit correction and restart.

The native manager stays available when the backend is stopped. A short-lived,
bundled inventory helper scans processes and launch services in that state; its
PID appears in the manager footer while it runs. The snapshot refreshes every
two seconds while connected and every five seconds while offline. The
workspace's live updates continue to use the existing WebSocket event stream.

## What the manager controls

- **Services:** agent engine, memory compaction, skill learning, agent browser,
  watcher, scheduler, heartbeat, daily brief, messaging channels, webhook
  listener/tunnel, GitHub polling and fleet listeners/synchronization. Desired
  states persist in `settings.yaml`. Dependencies stop in reverse order and
  restart in dependency order; failed loops require an explicit restart.
- **Processes:** detected Mission Control roots and descendants, registered
  tool commands, and tagged detached tools belonging to this user. Records show
  PID, parent, service/agent, resident memory and ownership. Stop rechecks the
  process creation time and refuses unknown, replaced or protected PIDs.
- **Harness:** agent state, queues, per-turn tool limits, tool/skill access and
  active skill runs. Configure agents and skill contracts in the workspace.
- **Tasks:** named application tasks, other live event-loop tasks and recent
  completions/cancellations/failures. Transport tasks stop with the runtime.
- **Background & permissions:** keep running on close, bounded crash recovery,
  launch at login, microphone, Accessibility, screen recording, and links to
  Full Disk Access and Automation settings. Runtime data location and local
  port can be changed while stopped.

The process inventory uses OS process information visible to the current user.
Unrelated applications are not included. Existing shared tools such as Ollama,
LM Studio, BlueBubbles and Tailscale remain external applications configured
through the workspace's integrations. Mission Control does not claim ownership
of those applications simply because an agent connects to them. System-owned
launch daemons appear read-only; changing them requires macOS administrator
controls. Tools that deliberately erase their identity and detach from all
observable Mission Control ancestry cannot be attributed reliably.

## Existing installations

**Processes → Existing launch services** lists legacy `com.missioncontrol`
launch agents, their status and data locations. User-owned services can be
disabled/stopped, enabled, or restarted from the native manager. Other runtime
processes are labeled **outside runtime** and have individual stop controls.

To consolidate an existing installation:

1. Stop the native runtime and disable/stop its legacy login service in the
   process inventory. Stop any remaining older source runtimes for that folder.
2. In Background & permissions, choose the existing folder containing
   `agents.yaml`, `settings.yaml`, `.env` and `missioncontrol.db`.
3. Set the desired app port and start the runtime. Your existing chat, memory,
   roster and integrations load from that folder without copying credentials.
4. Enable launch at login for the native app if desired.

Starting a second runtime for the same folder is rejected, including detected
older runtimes that predate the file lock. An occupied app port is rejected
before agents or recovery logic start. The app does not automatically stop
another installation or rewrite its launch service.

Login behavior uses Apple's [SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice)
API. The app shows registration/approval status and opens Login Items settings.
macOS owns approval for privacy permissions; denied access is not reported as
granted. Full Disk Access and per-application Automation approval are managed
in System Settings. Browser-agent automation uses the bundled Chromium app;
source setup uses `./venv/bin/playwright install chromium`.
Open **Settings → Websites → Open browser / sign in** to sign into sites in
the managed profile. Agent tabs reuse those sessions. The panel shows live
tabs and per-agent tool access, with controls to show/close tabs, set window
visibility, or disable browser access. **Services → Agent browser → Configure**
opens the same panel. Browser tool activity appears in the agent's chat; Chromium
processes are tagged to the browser service in process inventory. Private
login/payment fields are masked in snapshots. Saved logins use macOS Keychain
(Windows uses Credential Manager), keyed by workspace and exact HTTPS origin.
The backend fills credentials directly; models receive status, never passwords.
Add/update/forget accounts under **Settings → Websites → Saved logins**.
When a login needs 2FA, **2FA delivery** can nudge the configured private
iMessage, Telegram or WhatsApp chat. Reply with just the numeric code for a
single request, or `2fa REQUEST CODE` when several sign-ins are waiting. Only
the bound channel and sender can fulfill it. Replies are intercepted before
conversation/model/memory processing, are single-use and expire after five
minutes. They are not saved in Mission Control; the messaging provider manages
its own history. Private app prompts also support additional login details and
manual passkey/device-approval steps. Cancelled/stale requests never fall back
into agent chat. Password changes and payment fields still need the owner.
Typed tool arguments are omitted from the audit trail. Browser permissions do
not replace the scope of the task and the owner's standing rules.
Provider credentials, optional CLIs and third-party integration servers remain
configured in the workspace, as with the browser version. Source self-improvement
requires a source checkout.

## Development checks

```sh
./venv/bin/python -m pytest tests/ -q
./venv/bin/python scripts/check_native_runtime.py
./venv/bin/python scripts/check_ui.py --url http://127.0.0.1:8919
```

For an isolated native run, launch the built executable with `--data-home PATH
--port 8919`. These overrides do not replace the saved workspace preference.
`--no-start` permits inventory and
permission inspection without starting a backend, and `--no-bar` leaves the floating bar
out. The bundled runtime supports
`--inventory` for diagnostics. `/api/runtime` and the associated mutation routes
are limited to local clients and reject cross-origin control requests.
