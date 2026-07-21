---
title: "Sources and derived knowledge"
description: "The two kinds of thing aizk stores and why only one of them is authoritative."
---

aizk stores two kinds of thing and treats them very differently. Getting that difference straight
early makes everything else easier to reason about. This page assumes you have read
[What aizk is](/docs/user/what-is-aizk/) and nothing more.

## A source is what somebody actually wrote

A source is the record. It is the note you typed, the brief your assistant wrote on your behalf,
the page you pointed at by URL, or the contract you uploaded. aizk stores it as given and never
rewrites a word of it. Nothing in the engine edits a source, and nothing quietly merges two of
them into a tidier third one.

A source comes in one of two shapes.

Most are **text**, plain or Markdown, and text is the shape to prefer. Everything downstream
reads text, so a note written as text is the cheapest thing to store and the most useful thing to
get back.

The other shape is a **preserved original file**. aizk keeps the bytes exactly as they arrived and
converts a readable text version alongside them, so the same document can be found by meaning and
still handed back in full later. Preserve an original when the exact document may genuinely be
needed again, a signed contract, a form, a paper, or a presentation.
[Files, PDFs and web sources](/docs/user/using/files/) walks through that path and its size
limits.

Either shape carries the scope set deciding who may read it, covered on
[Scopes](/docs/user/concepts/scopes/), and the times deciding when it applies, covered on
[Time and history](/docs/user/concepts/time/).

## Derived knowledge is what aizk works out afterward

Once a source lands, background work reads it and builds a second layer on top. None of that layer
is something you wrote. All of it is a projection of what you wrote.

| Derived thing | What it is | Name in the web app |
|---|---|---|
| Entity | A named thing the sources talk about, a person, a project, a paper | Subject |
| Fact | One statement linking named things, carrying the quote it came from | Finding |
| Community | A cluster of related entities the engine noticed | Theme |
| Profile | A rolling summary of one entity across everything said about it | Profile |
| Summary | A summary written over a group of sources rather than one | Summary |

[Entities, facts, ontology](/docs/user/concepts/graph/) explains how prose turns into the first
two, which is where most of the value sits.

:::note[Where this comes from]
The derived layer, the entities and facts aizk pulls from your text, follows the temporal entity
and fact graph of [Zep and Graphiti](https://arxiv.org/abs/2501.13956). Keeping the raw source
authoritative echoes [Does Memory Need Graphs](https://arxiv.org/abs/2601.01280). The rule that a
source always outranks a derived reading is aizk's own. The full
[map of prior art](/docs/dev/prior-art/references/) traces every mechanism.
:::

## The two layers are not equal

```text
  ┌──────────────────────────────────────────────────┐
  │  SOURCES     what you wrote or uploaded          │
  │              authoritative, never edited         │
  └───────────────────────┬──────────────────────────┘
                          │ read by the engine
                          ▼
  ┌──────────────────────────────────────────────────┐
  │  DERIVED     entities and facts                  │
  │              communities, profiles, summaries    │
  │              rebuildable, never the record       │
  └──────────────────────────────────────────────────┘

    delete the lower box and aizk rebuilds it from the upper one
    delete the upper box and the memory is actually gone
```

That asymmetry is the whole point. Derived knowledge is disposable by design. When the extraction
models improve, or a bad batch of facts turns up, the fix is to rebuild the layer rather than to
repair it by hand. Nothing of yours is at risk in that operation because nothing of yours lives
there.

## Why the split matters when evidence disagrees

Recall returns both layers mixed into one ranked list, and every item says which layer it came
from. Sooner or later two items will disagree.

When they do, **the source wins**. A source is a human statement that somebody committed to memory
on purpose. A derived fact is a machine reading of some source, and it can be a stale reading, a
partial one, or simply a wrong one. So a source excerpt saying the team moved off the LLM
extractor outranks a derived memory still claiming the team uses it.

This is exactly why aizk labels each item instead of blending everything into one confident
paragraph. Your assistant can see which layer a claim came from and weigh it accordingly, and so
can you. [Evidence and provenance](/docs/user/concepts/evidence/) covers the labels in full.

The same rule settles a subtler case. A derived fact often reads as more authoritative than it is,
because it arrives as a clean assertion while the sentence behind it was hedged or conditional.
When a derived claim really matters, the honest move is to go read the source excerpt under it.

## What happens when a source changes

Correcting a note does not leave the old derived claims standing. When a source is replaced, the
facts built from its previous text stop counting as current and the facts built from the new text
take over. The old ones are not deleted, they are closed, so the record of what aizk believed
stays intact.

That is the same machinery that handles a fact simply going out of date, and
[Time and history](/docs/user/concepts/time/) owns the explanation.

## What this means in practice

Three habits follow directly from the split.

**Write the source you would want to read.** Derived knowledge is only as good as the prose under
it. A note with a real title and real sentences produces useful entities and facts. A note that is
three keywords produces almost nothing.

**Do not try to write facts by hand.** There is no tool for adding an entity or a fact directly,
and that is deliberate. You write sources and the engine derives the rest, which keeps every
derived claim traceable to a real quote in a real document.

**Do not treat the derived layer as a backup.** It is an index, not an archive. If the text
matters, the text belongs in a source.

## Next

<div class="not-content">

- [Entities, facts, ontology](/docs/user/concepts/graph/) shows how prose becomes the derived layer.
- [Writing memory well](/docs/user/using/remember/) is the practical guide to authoring sources.
- [Time and history](/docs/user/concepts/time/) explains the clocks every source and fact carries.

</div>
