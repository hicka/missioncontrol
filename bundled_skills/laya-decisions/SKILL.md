---
name: Laya decisions
description: Sort, triage or gate many items cheaply with the decide tool (self-hosted Laya, Jev-compatible, free and local) instead of reading each one yourself. Use for classifying emails, alerts, issues, log lines or tickets into kinds, scoring urgency, or deciding whether something deserves a closer look.
avoid: Writing or summarising text, answering factual questions, anything that needs more than about 512 tokens of context per item, or a single irreversible action taken on a probability alone.
author: Mission Control
updated: 2026-10-20
---

Laya is a decision model, not a language model. It reads a short state and
returns calibrated probabilities for typed questions, in a fraction of a second,
on this Mac, at no cost. Nothing leaves the machine. It never writes text.

When to reach for it
- Many similar items need the same judgement: which of 300 alerts are noise,
  which issues are bugs and which are feature requests, which emails are
  promotions. Ask Laya about every item, then read only what it is unsure of.
- A cheap first pass in front of your own reasoning: let Laya rank, then spend
  your attention on the top and on the uncertain middle.

How to ask
1. Keep the state short and factual: the fields that carry the signal (an
   email's from and subject, an alert's title and service), as a small object.
   Long bodies get cut at about 512 tokens.
2. Prefer a choice of concrete kinds over an abstract yes/no. Measured on the
   owner's mail: "can this email be archived?" as a noul scored everything
   between 0.42 and 0.65, useless; the same emails as a choice between seven
   kinds (promotion, social, automated notice, receipt, security alert, needs
   action, personal) separated cleanly. Give each option a few words saying
   what it covers, keep the options distinct, and stay under about 20.
3. Combine kinds after the answer: the probability that an item is clutter is
   the sum of the clutter kinds, not the top choice alone.
4. A noul works for a concrete, checkable statement about the state ("the
   subject mentions a failed payment"), not for a judgement call.
5. score is for an ordered scale (not urgent, somewhat, urgent, critical); the
   answer is the expected level.
6. For many items pass items (up to 200 states) with one set of questions;
   each is answered on its own. Expect about 0.3 s per item, plus a few
   seconds when the model has been idle and has to load.

Before acting on many items
- Calibrate first: run the questions over 10 to 20 items whose right answer you
  already know, look at the spread, and choose a threshold that separates
  them. If the probabilities bunch together, reframe the question.
- Act only above a margin (for example 0.8 or more on the kinds you act on)
  and leave the middle alone or look at it yourself.
- Combine levels when one is not enough: judge the source once (a sender, with
  several of its subjects) and each item from it (one subject). The source
  tells you what usually arrives; the item catches the exception, such as a
  login alert from a bank that mostly sends offers.
- Prefer reversible actions (archive, label, queue for review) and keep a
  record of what you did, so a wrong call can be undone.

If decide says Laya is off or unreachable, decide another way. An unanswered
question is never a "no".
