# Native macOS application

Mission Control is a Swift/AppKit application with a SwiftUI runtime manager,
an `NSStatusItem` menu bar control, and a WKWebView containing the complete
workspace: home, inbox, tasks, schedule, knowledge, evolution, settings and chat. Python and
the backend dependencies and Chromium are bundled; running the built app does not require
an activated virtual environment or a separate web browser.

## Build and launch

Requires macOS 13+, Xcode command line tools, and Python 3.11+ for building:

```sh
./venv/bin/pip install -r requirements-desktop.txt
./venv/bin/python scripts/build_macos.py
open "dist/native/Mission Control.app"
```

The build targets the Mac's architecture. Build on Intel for an Intel app, or
on Apple Silicon for an arm64 app. `--skip-runtime` reuses the frozen backend
when only Swift files changed. Chromium is copied from the matching Playwright
installation (downloaded on build if missing); `--without-browser` skips it for
development builds. The build stages and verifies the app before
replacing the previous output. Only example configs, static assets and starter
skills are packaged; local `.env`, databases, conversations and settings are not.

The default signature is ad hoc for local development. For distribution use
`--signing-identity 'Developer ID Application: …'`, then notarize and staple the
result using your Apple developer account. This build script does not submit
anything to Apple. Copy the app to Applications before enabling launch at login.

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
requires a source checkout; updating bundled application code requires a rebuild.

## Development checks

```sh
./venv/bin/python -m pytest tests/ -q
./venv/bin/python scripts/check_native_runtime.py
./venv/bin/python scripts/check_ui.py --url http://127.0.0.1:8919
```

For an isolated native run, launch the built executable with `--data-home PATH
--port 8919`. These overrides do not replace the saved workspace preference.
`--show-manager` opens Runtime Manager first; `--no-start` permits inventory and
permission inspection without starting a backend. The bundled runtime supports
`--inventory` for diagnostics. `/api/runtime` and the associated mutation routes
are limited to local clients and reject cross-origin control requests.
