---
title: "Writing memory well"
description: "What belongs in a memory, how to shape it, and when to split one note into two."
---

This page assumes you have a client connected and have stored at least one note, which
[Quickstart](/docs/user/quickstart/) covers. It is about the shape of what you write rather than the
mechanics of writing it.

Writing to aizk is one call named `remember` that carries self-describing Markdown. Your harness
makes it look like this.

```text
aizk.remember(text="# Reranker stays on by default\n\n...the reasoning...")
```

Everything else is optional, and the defaults are almost always the right answer.

## What belongs in a memory

The test is whether somebody would have to ask a person to find it out again. A decision and the
reason behind it passes. A result and the conditions it was measured under passes. A meeting
happened does not, unless something was decided in it.

Write the reasoning, not only the outcome. A note saying the team uses one library is worth little
in six months. A note saying the team moved to that library because the previous one held a lock
across an await and stalled the queue is worth a lot, because it tells the next reader when the
decision stops applying.

:::caution[Keep secrets out]
Credentials, private keys, tokens, and anything you would not want in a database backup do not
belong in memory, and there is no clean way to take them back out once they are in.
:::

## One coherent topic per call

Each call becomes one source document, and recall hands back sources as units. So the unit you
choose when writing is the unit somebody gets when asking.

```text
  one project, its decision, and the reason           ──▶  one call   ✓

  four unrelated projects in one dump                 ──▶  one call   ✗
                                                           split it

  one decision split into six one-line notes          ──▶  six calls  ✗
                                                           join them
```

Keep related decisions and findings together. If three choices were made in the same review and
they explain each other, they belong in one note, because splitting them means recall can return one
without the two that give it context.

Split only for a real reason, and there are three.

- The parts belong in **different places**, such as one private and one in a team scope.
- The parts came from **different sources**, so mixing them would blur where each claim came from.
- The parts have **different validity**, such as one durable decision and one thing that genuinely
  stops being true on a date.

Anything else is a preference, and the preference should be to keep it together.

## Give it a title

The first level-one heading is what recall shows as the title, so make it the thing you would search
for rather than a label like Notes or Update.

```markdown
# Reranker stays on by default

Turning the cross-encoder off saved 40 ms per query and cost more answer quality
than the latency was worth, measured on the 200 question internal set in June.
```

A note with no heading still works, but it is harder to recognize in a list of evidence.

## Tags, when you want them

Source tags attach a note to named things so related notes cluster. The general form is a kind and a
name, and the two common ones are project and area.

```markdown
#project: Assay validation
#area: Research
```

Tags are for organization and nothing else. They never mean status, ownership, or who can read the
note. [Entities, facts, ontology](/docs/user/concepts/graph/) owns the full syntax, the typed
relation lines, and the list of kinds your deployment accepts. Plain prose with no tags at all is a
perfectly good note.

## Dates, almost never

Two optional times exist. One says when the statement became true in the world, and the other says
when it stops being true.

Omit both for ordinary durable knowledge, which is nearly everything. Set the first only when the
real date differs materially from today, such as recording a decision made three months ago. Set the
second only when the world supplies a genuine deadline, such as a contract that ends.

Expiry is a hard boundary, not a reminder. Never use it to mean the note might go stale eventually,
because when it passes the note stops showing up in ordinary recall.
[Time and history](/docs/user/concepts/time/) explains both clocks and what happens after expiry.

## Code, sparingly

A short snippet earns its place when it is the durable thing being decided, such as the exact
configuration line a team standardized on or the query shape that fixed a slow page.

```markdown
# Session pooling settings we settled on

Pool size 20 with a 30 second recycle. Anything larger and the database hit its
connection cap during the nightly backfill.
```

Whole files, generated logs, and datasets do not belong. They live in the repository that produces
them, and a note explaining what they are and where they are beats a copy that drifts.

## Where it goes

Naming no organization writes privately, which is the default. Naming one writes into a team scope,
and your assistant can only name organizations you are actually allowed to write to.

```text
aizk.remember(text="# Session pooling settings ...")                  # private, the default
aizk.remember(text="# Assay protocol ...", scopes=["Book Club"])      # into a team scope
```

[Sharing and organizations](/docs/user/using/sharing/) covers checking that before the first shared
write.

:::tip[Good habit]
Write straight into the scope where the note belongs rather than writing privately and sharing
later. Sharing later makes a copy, and copies drift.
:::

## Next

<div class="not-content">

- [Asking memory well](/docs/user/using/recall/) is the other half of the loop.
- [Notes that stay useful](/docs/user/using/habits/) is the longer view on writing habits.
- [Files, PDFs and web sources](/docs/user/using/files/) covers writing something you did not author.

</div>
