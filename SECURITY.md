# Security

Mission Control runs AI agents with real tools on your own computer. Read this
before you connect it to anything you do not control.

## What it is, and is not

Agents run as your user. They have a real terminal, a real browser, and your
files. The rules that hold them back are defence in depth, **not a sandbox**: a
determined script can get past a command pattern, and work that started from an
outside event still runs on your machine. If you want a hard boundary, run it in
a container or on a machine you can afford to lose.

[The authority section of the kernel design](docs/super-agent.md#authority)
says exactly what each kind of work may do, and what those rules do not cover.

## Keeping it safe

- **Leave it on loopback.** By default the server listens on `127.0.0.1` only.
  Turning on remote access mints an API token, and every remote request must
  carry it. Requests from this machine need no token, so anything local can use it.
- **Never port-forward it.** Keep the app port and the fleet port (8902) on a LAN
  or a VPN such as Tailscale.
- **Think before you let strangers suggest work.** Setting a source anyone can
  write to, such as public GitHub comments, to "read and judge", with a rule that
  starts work, lets outsiders suggest tasks to something that has your files.
- **Read what you approve.** A held command runs exactly as shown, so read the
  whole thing.
- **Give it narrow credentials.** Agents use the keys you put in `.env`. Prefer a
  separate bot account with narrow scopes to your own login.
- **Treat what it reads as untrusted.** Web pages, emails, issues and tool output
  can all carry instructions written to mislead a model.

Your data stays on your machine, in `missioncontrol.db`, `.env`, `settings.yaml`
and `browser-profile/`. All are gitignored, and none of them should ever be
committed or shared. There is no telemetry. It connects to your model provider,
the channels and integrations you turn on, and whatever its tools reach while
working, such as a web search or a page.

## Reporting a vulnerability

Please do not open a public issue. Use **Report a vulnerability** on this
repository's Security tab, which reaches the maintainer privately.

Include what you did, what happened, and the commit you tested. Reports are most
useful when they show one of these:

- work from an outside event doing something only the owner should be able to do
- text from outside changing a rule, a memory or a policy
- a held command running without approval, or running differently from what was shown
- a secret reaching a model, a log or a chat without being masked
- the server answering a remote request that has no valid token
- an agent changing the files that hold its own limits (`selfimprove.PROTECTED`)

A shell script getting past a command pattern is a known limit, described in the
design document. Reports that widen it are still welcome.

This is a small project and there is no bounty.
