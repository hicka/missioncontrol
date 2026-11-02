---
name: Mail cleanup
description: Clean up the owner's Mail inbox, explain what the hourly Laya mail sweep archived or kept, find a message it archived, and apply the owner's keep or always-archive rules for a sender. Use when the owner mentions inbox clutter, a missing email, or a sender they do or do not want archived.
avoid: Sending, deleting or drafting mail (use apple mail draft for drafts), reading mail content for its own sake, or changing the sweep's settings (dry run, thresholds, schedule), which are the owner's.
author: Mission Control
updated: 2026-10-20
---

How the sweep works (so you can explain it)
- Every hour it reads the Mail inbox's senders and subjects and asks the
  self-hosted Laya model two things: what kind of mail each sender sends (once
  per sender), and, for bulk senders only, what kind each message is. Mail
  from a bulk sender (promotions, social notifications, automated notices,
  receipts) is moved to Archive unless that message reads as a security alert,
  something to act on, or a personal note. No LLM is involved and it costs
  nothing.
- Never touched: flagged mail, mail the owner replied to, senders the owner
  corresponds with (they replied to a real share of that sender's mail), and
  senders on the keep list.
- Archive is not deletion: everything stays searchable in Mail's Archive.
- Until the owner turns dry run off, sweeps only report what they would move.

Procedure
1. Start with mail_cleanup action=status. It shows the settings, the last
   sweep's counts, its biggest archived senders, the unsure senders it left
   alone, and examples it held back as important.
2. The owner wants it cleaned now: mail_cleanup action=run. It runs in the
   background and wakes you with its report when it finishes, so tell the
   owner it has started and end your turn: do not sleep, wait or poll. When
   the report arrives, relay it in a few lines. Do not pass dry_run=false to
   get around a dry-run setting: going live is the owner's decision in
   settings.
3. "Stop archiving X" or "where is my email from X": mail_cleanup action=keep
   with sender (an address, or a domain such as example.com). Add restore=true
   to move what the sweep archived from them back to the inbox, and say how
   many came back.
4. "Always archive X" (often an unsure sender from the report):
   mail_cleanup action=always with sender.
5. When explaining a decision, quote the report: which senders it treated as
   bulk, what it held back, and that anything archived can be restored.
6. If status shows a problem (Laya not running, Mail not connected), say so
   plainly. Laya runs as the "Laya decisions" service, and Mail access is
   switched on in Settings > Apple apps.
