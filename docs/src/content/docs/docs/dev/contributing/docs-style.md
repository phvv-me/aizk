---
title: "Writing these docs"
description: "The rules every page here follows, including the ten minute ceiling and the diagram requirement."
---

These documents are code. They live in `docs/src/content/docs/docs/`, they build with the site,
and a check in the build fails the whole thing when a page breaks one of the three mechanical
rules below. This page is the contract, and it applies to every page in both halves.

## The four rules the build enforces

`docs/scripts/check-pages.mjs` runs after every build and refuses four things.

```text
  page.md ──▶ strip frontmatter, code fences, tables, html
                       │
                       ├──▶ words > 1600 ?           ──▶ fail, split the page
                       ├──▶ no mermaid, d2, text
                       │    or component ?           ──▶ fail, add a diagram
                       ├──▶ em dash, colon or
                       │    semicolon in prose ?     ──▶ fail, rewrite the sentence
                       └──▶ (from dist/) href that
                            no built page serves ?   ──▶ fail, fix the link
```

**Ten minutes, no exceptions.** The budget is 1,600 words of prose, which at 200 words a minute
is eight minutes and leaves room for the code and diagrams that also cost a reader time. Going
over is a signal that the page holds two subjects. Split it and link the halves rather than
trimming sentences until it squeaks under.

**Every page carries a diagram.** A page with no picture is usually a page that is listing
rather than explaining. Mermaid, D2, hand-drawn ASCII art in a fenced block, or an interactive
component all count.

**Every internal link resolves.** The check reads the built HTML, so a link to a page that was
renamed fails the build rather than reaching a reader.

## Self-contained means something specific

A reader arrives from search, not from the sidebar. So every page opens by saying what it
assumes and links the page that supplies it. After that it must not need anything the reader
has not been given.

The rule that follows is that an idea is explained in exactly one place. Scopes are explained on
[Scopes](/docs/user/concepts/scopes/). Expiry is explained on
[Time and history](/docs/user/concepts/time/). When another page needs either, it spends one
sentence on the shape and links out for the rest. The previous version of these docs restated
the expiry rules in full on three pages and the source tag syntax on four, and they drifted
apart, which is exactly the failure this rule exists to prevent.

## Voice

Write friendly, simple, concise American English, the way you would explain the thing to a
colleague who is smart but new.

Never use an em dash, a colon, or a semicolon in prose. Use conjunctions, shorter sentences, and
real cohesion instead. Colons are fine inside code, tables, frontmatter, links and component
imports, where they are syntax rather than punctuation, and the check knows the difference.

Prefer the concrete. Name the file, the table, the setting, the number. A developer page that
says "the extractor is configurable" helps nobody, while one that says extraction switches
between the two backends through `AIZK_EXTRACT_BACKEND` sends the reader straight to the thing.

Say what is not true as readily as what is. If a measurement was taken on one machine on one
day, say so. If a comparison has no head-to-head number behind it, say that too. These docs
carry several dated measurement cells and they are only worth keeping because they are honest
about their conditions.

## Which half a page belongs to

The user half answers "how do I get value from this". It never mentions a table name, a Python
module, or an environment variable, and it never assumes the reader has a deployment.

The developer half answers "how does this work and how do I change it". It may assume the
reader has the repository open and knows SQL.

When a subject spans both, write it twice at two depths rather than once in the middle. Sharing
is a good example. The user page explains that naming two organizations makes a cell only their
overlap can read, and the developer page explains the sorted `uuid[]` column, the GIN index and
the policies that enforce it.

## Diagrams

Pick by what the picture has to do.

| Need | Use |
|---|---|
| flow, sequence, state, decision | mermaid |
| tables with columns, nested containers | D2 through `astro-d2` |
| something that should look like a terminal | ASCII art in a ` ```text ` block |
| a map worth clicking through | a Svelte component mounted `client:only="svelte"` |

Svelte Flow measures nodes with the browser layout engine and has no server-side layout, so an
interactive diagram must use `client:only="svelte"` rather than `client:visible`, and its
container needs an explicit height or the canvas collapses to nothing.

## Keeping pages true

A page that describes code is only as good as its last check against that code. When you change
behavior, change the page in the same commit. When you find a page that has drifted, fix it
rather than working around it, and prefer naming the code path so the next reader can verify it
without trusting you.
