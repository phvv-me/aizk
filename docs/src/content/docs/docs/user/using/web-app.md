---
title: "The web app"
description: "A tour of the signed-in interface and what each screen shows you."
---

This page assumes you have an account and have stored something, which
[Quickstart](/docs/user/quickstart/) covers. The web app is optional. Everything it shows comes from
the same memory your assistant reads, so nothing here is required to use aizk.

Sign in at your deployment's address and you land under `/app/`. Nine screens sit in four groups.

```text
  Knowledge        Dashboard      Recall
  Explore          Sources        Findings      Subjects     Themes
  Operations       Usage          Processing
  Collaboration    Organizations
```

:::note[The words change in the app]
The interface uses friendlier names than the rest of these docs. Findings are facts, Subjects are
entities, and Themes are communities. Sources keep their name.
[Entities, facts, ontology](/docs/user/concepts/graph/) uses the engine words throughout.
:::

## Dashboard

Four counters run across the top for sources, findings, subjects, and themes, each linking to its own
screen. The counts cover everything visible to you, which is your private memory plus every
organization you belong to plus any public ones.

Below that sits a processing card with two bars. Source conversion is preserved originals being
turned into text, and graph enrichment is findings, subjects, and themes being built from that text.
Each shows an estimated range rather than a single number, and the card updates live while the tab is
in front.

At the bottom you get the newest sources, each with a badge for who can see it, and a thirty day usage
summary with requests, evidence items, recalls, and remembers.

## Recall

A box, a question, and the same evidence Markdown your assistant receives. This is the fastest way to
see what recall really returns without reading a transcript, and it is handy for checking whether a
note you just wrote is findable yet.

It behaves exactly like the tool, so [Asking memory well](/docs/user/using/recall/) applies here
unchanged. One focused question, no scope selector, evidence rather than an answer.

## Sources

The catalog of everything you or a teammate stored. Search titles and source links, then read a table
of source, type, observed date, updated date, and scope, newest first, with paging.

Observed is when the source says it was true or published, and updated is when aizk last stored it. A
chart above the table groups the current page by type, a quick way to see whether your memory is
mostly notes, mostly papers, or mostly web pages.

## Findings

The current claims aizk pulled from source text. Each card shows the relation, the subjects on either
side of it, the statement itself, when it was recorded, the scope, and a link back to its source.

Two things are worth knowing. These are projections rather than records, so if one disagrees with a
source excerpt the source wins. And only current findings appear, so superseded history is kept but
not shown here. [Sources and derived knowledge](/docs/user/concepts/sources/) explains why.

## Subjects

The named things in memory, which is people, projects, places, concepts, and whatever else your notes
talk about. Search by name or type. The table shows the subject, its type, how many current findings
touch it, when it last changed, and its scope, busiest first.

The finding count is a link, so clicking it filters findings down to that subject. A small graph view
above the table shows how the visible subjects connect.

## Themes

Clusters aizk found on its own by watching how subjects and findings group together. Each card gives a
generated summary, the number of subjects in the cluster, when it was last rebuilt, and a preview of
member names that link back to Subjects.

This screen stays empty for a while on a new memory, which is correct rather than broken. Themes are
rebuilt after enough findings accumulate, so sources become recallable well before the next theme
pass runs.

## Usage

Successful operations over the last 7, 30, 90 or 365 days. Four tiles cover successful requests, items
handled, bytes uploaded, and bytes downloaded, then a chart over time, then a breakdown by operation
into recalls, remembers, files, shares, and artifact reads.

Only successful operations count. Failed calls and ordinary page views are excluded, and the numbers
survive restarts because they are written down rather than held in memory.

## Processing

The detailed version of the dashboard's progress card. It reports whether processing is idle, active,
or delayed, gives separate estimates for when content becomes recallable and when full enrichment
finishes, and lists recent preserved originals with a state badge.

The four states are queued while it waits for secure processing, processing while it converts, ready
when it can be recalled, and failed when it needs attention. A failed original is still findable by
name, which [Files, PDFs and web sources](/docs/user/using/files/) explains.

Delayed does not mean broken. It means recent completions are too sparse to make an honest estimate,
and the app says so rather than inventing a number.

## Organizations

Create an organization with a name and a description, and see every organization you belong to with
your roles on it.

Where your role allows it, you can add a member by email, move somebody between viewer, editor, and
admin, and remove a member. Only existing accounts can be added, since aizk does not send invitations
or expose the user directory.

There is no upload screen anywhere in the app. Files come in through a connected client or the command
line tool, which [Files, PDFs and web sources](/docs/user/using/files/) covers.

## Next

<div class="not-content">

- [Sharing and organizations](/docs/user/using/sharing/) is the practical side of the last screen.
- [Evidence and provenance](/docs/user/concepts/evidence/) explains the scope badges you see.
- [Entities, facts, ontology](/docs/user/concepts/graph/) is what Findings and Subjects show.

</div>
