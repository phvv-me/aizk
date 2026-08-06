---
title: "How settled a claim is"
description: "Reading the hedged, disputed and refuted marks on a derived memory, and correcting one."
---

A derived memory arrives as a clean assertion whatever the sentence behind it said. This page covers
the mark that says how much weight it carries and how you correct one that is wrong. It assumes you
have read [Sources and derived knowledge](/docs/user/concepts/sources/).

## The mark on a fact line

A fact line carries one bracket naming who said it and how settled the source was.

```text
  - [hedged] (improves_over) V7 improves over the baseline on noise
  - [Maya, opinion, disputed] (uses) the batch lane is the wrong default
  - (uses) the engine stores every claim in PostgreSQL
```

The third line carries no bracket at all, which is the ordinary case.

| Mark | What the source did |
|---|---|
| `settled` | stated it outright, so nothing is printed |
| `reported` | credited the claim to somebody else |
| `hedged` | qualified or doubted it |
| `disputed` | two of your sources disagree about it |
| `refuted` | said the claim is wrong |

## Why a word is not enough

A label beside a claim is easy to read past, so anything other than settled changes the answer in
two further ways.

The answer opens with a line telling you not to repeat such a claim as fact. And the source excerpt
the claim was drawn from stops counting as a repetition of it, so it stays in the answer beside it
rather than being dropped. That second one matters most. The sentence behind an unsettled claim is
the thing worth reading, and now it arrives on its own instead of you having to go looking.

## Where the mark comes from

Two places, and the careful one wins. Extraction reads the source and proposes a mark. Then a
deterministic check re-reads the sentence behind the claim and compares it against what the claim
says, and that check can only ever make a claim less settled, never more.

The same check refuses a fact outright when it states more certainly than its own sentence did. So a
note reading "V7 improves over the baseline, though the margin is inside run-to-run variance" cannot
become a flat memory that V7 improves over the baseline. Either the qualification survives into the
fact or there is no fact.

## Correcting a claim that is wrong

There is no tool that edits or deletes a derived claim. That is deliberate, since derived claims are
read out of your sources and any hand edit would be overwritten the next time they are read.

Write the correction as an ordinary note instead.

```text
  keep("Correction. The earlier claim that V7 improves over RIConv++ is
        refuted. The audit found the comparison used mismatched training
        budgets.")
```

Name the claim in the words a `find` returned it in, and say plainly that it is refuted, corrected,
or no longer holds. The engine reads your note against what memory already holds and closes the
claim it disproves, keeping your note as the evidence that closed it. The closed claim is not
deleted, so the record of what aizk believed stays intact.

A note that only casts doubt does something gentler. Both claims stay live and both are marked
disputed, because withdrawing something you committed to memory on a maybe is the worse of the two
mistakes.

## Next

<div class="not-content">

- [Evidence and provenance](/docs/user/concepts/evidence/) covers the rest of what a line carries.
- [Time and history](/docs/user/concepts/time/) explains what closing a claim means.
- [Writing memory well](/docs/user/using/remember/) is the guide to notes worth deriving from.

</div>
