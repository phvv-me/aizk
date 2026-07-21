---
title: "Notes that stay useful"
description: "The writing habits that keep a memory worth reading a year from now."
---

This page assumes you already know the mechanics from
[Writing memory well](/docs/user/using/remember/). Those rules are about one note. These are about
what a year of notes turns into, which is a different problem and the one that decides whether
anybody still trusts the memory later.

Six habits do most of the work.

```text
  ┌─────────────────────────────────────────────────────────┐
  │  one coherent source     over   many fragments          │
  │  text                    over   preserved files         │
  │  correct                 over   duplicate               │
  │  why                     over   what                    │
  │  generic tags            over   status tags             │
  │  a pointer to the repo   over   a copy of the repo      │
  └─────────────────────────────────────────────────────────┘
```

## One coherent source beats many fragments

Recall returns sources as units, so the unit you wrote is the unit somebody gets. A single note
covering a decision, its reason and its consequences comes back whole and reads like something a
colleague wrote. The same content chopped into eight one-line notes comes back as whichever three
lines scored highest, and those three rarely explain each other.

Fragments also multiply. Eight small notes on one subject become sixteen after the subject changes
once, and nobody can tell which eight are current. One note that gets rewritten stays one note.

The failure mode in the other direction is real too. A single note covering four unrelated projects
matches everything weakly and helps with nothing. One topic, written whole, is the target.

## Text beats a preserved file

Store what you understood, not the thing you read. Your own paragraph is shorter, it is searchable
immediately, and it says why the document mattered, which the document itself never does.

Preserve the original only when the exact bytes might be needed later. A contract, a signed record,
a form, a paper you might need to quote by page, and a presentation somebody will want to reopen
all qualify. [Files, PDFs and web sources](/docs/user/using/files/) covers how, and its most useful
advice is that a link plus your own summary usually beats a stored copy.

## Correct, do not duplicate

When something changes, rewrite the note. Do not add a second note saying the first one is out of
date, and do not leave both standing so a reader has to work out which is newer.

aizk keeps history for you. The old version keeps its dates and stays available, ordinary recall
favors the current one, and asking what the team believed six months ago still works.
[Time and history](/docs/user/concepts/time/) explains the two clocks that make this safe.

Adding a contradicting note instead is the single fastest way to make a memory untrustworthy,
because after it happens twice nobody can tell which of any two notes to believe.

## Write the why, not only the what

A note that records a decision is worth something. A note that records the reason is worth
something a year later, because the reason tells the next reader when the decision stops applying.

```markdown
# Extraction runs on the local model

The hosted lane cost 4.1 s per section and the local one costs 0.3 s at the same
grounding rate, measured on the internal set in June. If the hosted price per
section drops below the operations cost of running our own, this is worth redoing.
```

That last sentence is what makes it a good note. Somebody reading it in a year knows exactly what
would change the answer, and does not have to reconstruct the trade from scratch.

Be equally direct about what you do not know. A measurement taken once on one machine should say
so, and a comparison with no head-to-head number behind it should say that too. An honest gap is
useful and a confident guess is a trap.

## Tags organize, they do not track status

Use tags to say what a note is about. A project name, an area, a paper, a tool. That is what makes
related notes find each other.

Do not use tags as a workflow board. A tag saying urgent, in progress, or blocked is true for about
a week and then quietly lies forever, because nothing goes back to remove it. When state genuinely
matters, write it in the prose where a reader can see the date around it, or use an explicit typed
relation. [Entities, facts, ontology](/docs/user/concepts/graph/) covers both.

Tags also say nothing about who can read a note. That is entirely
[Scopes](/docs/user/concepts/scopes/), and treating a tag as a boundary is a mistake worth avoiding
early.

## Keep the repository in the repository

Large code, generated logs, benchmark output and datasets belong where they are produced. They have
version control there, they are already searchable there, and a copy in memory starts drifting the
day after it is made.

What belongs in memory is the note explaining what that output showed and where to find it. One
paragraph and a path beats a thousand lines of log, and it is the paragraph somebody will actually
read.

## The test

Before writing, ask whether somebody would otherwise have to ask a person. Before finishing, read it
as if you had never seen the project and check that it still makes sense. If both pass, it is worth
keeping, and in a year it will still be worth keeping.

## Next

<div class="not-content">

- [Writing memory well](/docs/user/using/remember/) is the per-note version of this page.
- [Who maintains memory](/docs/user/concepts/lifecycle/) covers whose job the upkeep is.
- [Asking memory well](/docs/user/using/recall/) closes the loop.

</div>
