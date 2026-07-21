---
title: "Time and history"
description: "When something was true, when aizk learned it, and how corrections keep the past."
---

Every memory in aizk carries time, and it carries two kinds of it at once. This page assumes you
know the difference between a source and derived knowledge, which
[Sources and derived knowledge](/docs/user/concepts/sources/) covers. It is also the page that
owns the expiry rules, so anything you read elsewhere about expiry points back here.

## Two clocks, not one

Most tools record when they learned something. aizk records that **and** when the thing was true
out in the world, and it keeps the two apart.

**Valid time** is the world's clock. Ada led the project from January to June, then Bo took over.
That is true regardless of when anybody wrote it down.

**Recorded time** is aizk's clock. It is when aizk was told, which is often later and sometimes
much later.

```text
  valid time     when it was true in the world
  ──────────────────────────────────────────────────────────▶
      │  Ada leads the project   │  Bo leads the project   │
     Jan                        Jun                       now

  recorded time  when aizk was told
  ──────────────────────────────────────────────────────────▶
      │  we believed Ada         │  we believe Bo          │
     Jan                        Jul 2                     now

  "who led in March"           reads the top line at March
  "what did we believe Jul 1"  reads the bottom line at Jul 1
```

The gap between the two lines in that picture is real. Bo took over in June and nobody told aizk
until July 2, so for a month aizk believed something the world had already moved past. Keeping
both clocks is what lets you see that gap instead of pretending it never happened.

:::note[Where this comes from]
Keeping two clocks, one for when something was true and one for when aizk was told, comes from the
temporal knowledge graph in [Zep and Graphiti](https://arxiv.org/abs/2501.13956). Facts fading as
they go unused follows [Memora](https://arxiv.org/abs/2604.20006). Closing a range instead of
deleting it is aizk's own.
:::

## The two times you can set

When your assistant stores a memory it can attach two optional times. Both are optional, and for
ordinary durable knowledge both should be left off.

### `observed_at`, when it became true

Set this only when the applicable date is known and is meaningfully different from now. Notes
from a meeting three weeks ago, a paper published in 2019, a decision made before the memory was
written down. If the information became true roughly when it was captured, leave it off and let
capture time stand.

### `expires_at`, when it stops being true

Set this only when the world supplies a real cutoff. A conference badge valid through Friday. A
contractor's access granted until the end of the quarter. A policy with an announced replacement
date. The test is whether you can name the moment the statement becomes false without guessing.

If you cannot, leave it off.

## Expiry is a validity boundary, not a reminder

This is the rule people get wrong most often, so it is worth being blunt about.

An expiry says **this stops being true then**. It does not say "check this later", "this might
change someday", or "review this quarterly". aizk has no reminders, no notifications, no review
queue, and no task list. Nothing happens when an expiry passes except that the statement stops
counting as current.

Concretely, once the time passes, ordinary recall stops returning that source and stops returning
the current facts derived from it. The history is untouched. Nothing is deleted and nothing is
overwritten, so a question about what was known back then still finds it.

```text
  expiry passes
        │
        ├─▶ ordinary recall no longer returns the source        ✓
        ├─▶ its derived current facts stop being current        ✓
        ├─▶ history is preserved and still queryable            ✓
        │
        ├─▶ anybody gets a reminder                             ✗
        ├─▶ a replacement is written automatically              ✗
        └─▶ the text is deleted                                 ✗
```

Because expiry silently removes knowledge from recall, a wrong expiry is expensive. It does not
produce an error or a warning. It produces a memory that used to be found and now is not, with
nothing pointing at why.

### Things that should never carry an expiry

Living documentation. Project briefs. Area briefs. Research findings. Design decisions and the
reasoning behind them. Instructions for using a piece of software. None of these have a scheduled
end. They change when the world changes, and when they change somebody writes the correction.

Uncertainty is not an expiry either. Neither is a maintenance interval, nor a hunch that the
document will probably be rewritten eventually. When in doubt, omit it. A durable memory should
stay current until somebody observes a change and says so.

## Corrections close a range, they do not delete

When knowledge changes, aizk does not overwrite the old version. It closes the old one and opens
a new one beside it.

Take the project lead again. When aizk is told Bo took over, the claim about Ada gets an end on
the valid clock, and a new claim about Bo starts from the handover date. Both rows still exist.
Ordinary recall returns Bo, because only the open claim counts as current, and the claim about
Ada is still there for anybody who asks about March.

The same thing happens on the recorded clock when a mistake is fixed rather than a change
recorded. If a note claimed the wrong start date, the correction closes the wrong claim in
recorded time and opens a right one, which means aizk can still say what it believed before the
correction landed.

Two useful consequences fall out of that.

**Nothing is lost by correcting.** There is no reason to hesitate before fixing a memory, because
fixing it costs nothing and the earlier version stays on the record.

**Backdating works.** A correction that says something became true earlier than aizk was told is
handled properly rather than being flattened into the present.

## Facts fade when nobody uses them

There is one more time effect worth knowing about. Derived facts carry a relevance that decays as
they go unused, and a background pass closes the ones that fall below the floor. This never
touches your sources, only the derived layer, and a closed fact can come back the next time the
source it came from is read again.

## Next

<div class="not-content">

- [Writing memory well](/docs/user/using/remember/) shows where these times go when you store something.
- [Who maintains memory](/docs/user/concepts/lifecycle/) explains why corrections are an agent's job.
- [The bi-temporal model](/docs/dev/store/bitemporal/) is the developer version of this page.

</div>
