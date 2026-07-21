---
title: "Glossary"
description: "Every term these docs use, defined once."
---

Every word these docs lean on, in one place, each with a link to the page that owns it. Read
[What aizk is](/docs/user/what-is-aizk/) first if none of this is familiar yet.

Most of the terms describe one path, from something you wrote to something an agent gets back.

```text
   remember ──▶ document ──▶ chunk ──▶ entity + fact ──▶ community + profile
                (source)                  (derived memory)
        │                                        │
        └──────────── every row carries a scope ─┘
                              │
                           recall ──▶ evidence
```

## A to E

**Agent** is the assistant calling aizk, such as Claude Code or Codex. aizk stores and retrieves
and the agent does the talking. See [What aizk is](/docs/user/what-is-aizk/).

**Artifact** is one preserved original file with its revisions, kept byte for byte. See
[Files, PDFs and web sources](/docs/user/using/files/).

**Blob** is the stored bytes behind an artifact. Identical bytes are kept once and shared, and the
database holds only the location and the integrity record. See
[Content and artifact tables](/docs/dev/store/content-tables/).

**Chunk** is one ordered span of a document, the unit that gets embedded and searched. See
[Chunking and embedding](/docs/dev/write/chunking/).

**Claim** is one scope's assertion of a shared piece of derived knowledge, carrying the dates and
the usage counts. Two teams can claim the same statement independently. See
[Entities, facts, ontology](/docs/user/concepts/graph/).

**Community** is a cluster of related entities with a label and a summary, used to answer broad
questions. The web app calls these Themes. See
[Entities, facts, ontology](/docs/user/concepts/graph/).

**Content** is the deduplicated wording of an entity or a fact, stored once and pointed at by every
claim on it. See [Graph tables](/docs/dev/store/graph-tables/).

**Decay** is the background pass that archives derived knowledge nobody has touched in a long
time. It leaves recall and stays in history. See
[Who maintains memory](/docs/user/concepts/lifecycle/).

**Derived memory** is everything aizk worked out from your text, including entities, facts,
communities, and profiles. It can be thrown away and rebuilt. See
[Sources and derived knowledge](/docs/user/concepts/sources/).

**Document** is one remembered note or file as a source item, and the parent of its chunks. See
[Sources and derived knowledge](/docs/user/concepts/sources/).

**Entity** is a thing being talked about, such as a person, a project, a tool, or a result. The web
app calls these Subjects. See [Entities, facts, ontology](/docs/user/concepts/graph/).

**Evidence** is what recall returns, a ranked list of relevant items each labeled with where it
came from. It is not an answer. See [Evidence and provenance](/docs/user/concepts/evidence/).

**Expiry** is a known time after which a statement stops being true. Past it, ordinary recall skips
the source and its current derived facts while history keeps them. It is not a reminder. See
[Time and history](/docs/user/concepts/time/).

## F to P

**Fact** is a statement connecting entities, such as one project using one tool. The web app calls
these Findings. See [Entities, facts, ontology](/docs/user/concepts/graph/).

**Gate** is the cheap relevance check that decides whether a chunk is worth running full extraction
on. See [Extraction and the gate](/docs/dev/write/extraction/).

**Intersection** is a memory scoped to two or more organizations, readable only by people who
belong to all of them. Adding an organization narrows a memory rather than widening it. See
[Scopes](/docs/user/concepts/scopes/).

**MCP** is the Model Context Protocol, the standard your agent speaks to reach aizk. It is what
turns aizk into four tools your assistant can call. See
[MCP tools](/docs/user/reference/tools/).

**Ontology** is the vocabulary of entity kinds and relation kinds that extraction is allowed to
use. It keeps the graph from inventing a new word for the same idea. See
[Entities, facts, ontology](/docs/user/concepts/graph/).

**Organization** is a team you belong to, defined in the identity system rather than inside aizk.
Naming one on a memory is how sharing happens. See
[Sharing and organizations](/docs/user/using/sharing/).

**Perspective** separates claims that belong to a speaker from claims about the shared world. An
opinion or an observation stays attached to whoever said it, so two people can hold different
versions without overwriting each other. A statement about the world does not. See
[Entities, facts, ontology](/docs/user/concepts/graph/).

**Profile** is a maintained summary of everything currently known about one entity. See
[Profiles, insights, decay](/docs/dev/passes/profiles-insights/).

**Projection** is the background work that turns a stored source into derived memory. It runs after
the write returns, which is why something you just stored takes a moment to become findable. See
[Sources and derived knowledge](/docs/user/concepts/sources/).

**Provenance** is the label on each evidence item saying where it came from, which is a source
excerpt, a derived memory, or a recent session memory, and which scope holds it. See
[Evidence and provenance](/docs/user/concepts/evidence/).

## R to Z

**RAPTOR** is the pass that builds layered summaries over your material so a broad question can be
answered from a summary rather than from a hundred fragments. See
[Communities and RAPTOR](/docs/dev/passes/communities-raptor/).

**Recall** is the tool that takes one question and returns evidence. It searches everything you can
see at once and takes no scope selector. See [Asking memory well](/docs/user/using/recall/).

**Recorded time** is when aizk was told something, as opposed to when it was true. Correcting a
note opens a new recorded window rather than erasing the old one. See
[Time and history](/docs/user/concepts/time/).

**Remember** is the tool that stores a note, preserves an original, or prepares a file upload. See
[Writing memory well](/docs/user/using/remember/).

**Scope** is the answer to who can read a memory, expressed as a set of organizations. Naming none
keeps it private. See [Scopes](/docs/user/concepts/scopes/).

**Scope set** is the same idea named precisely, the exact sorted set of organizations a row carries
and the thing the database checks on every read. See
[Scope sets in depth](/docs/dev/identity/scope-sets/).

**Session memory** is recent working material that has not been promoted into the long-term graph
yet. Recall labels it separately so an agent can tell fresh chatter from settled knowledge. See
[Evidence and provenance](/docs/user/concepts/evidence/).

**Share** is the tool that copies documents you can see into a team scope. It copies rather than
moves, so your original stays yours and unchanged. See
[Sharing and organizations](/docs/user/using/sharing/).

**Source** is your own words, kept exactly as you wrote them. It is the authoritative half of what
aizk stores, and it wins whenever derived memory disagrees with it. See
[Sources and derived knowledge](/docs/user/concepts/sources/).

**Valid time** is when something was true in the world, as opposed to when aizk heard about it. See
[Time and history](/docs/user/concepts/time/).

## The web app's names

The web app speaks user language rather than engine language, so three terms change on the way to
the screen. Findings are facts, Subjects are entities, and Themes are communities. Nothing else is
different, and [The web app](/docs/user/using/web-app/) uses those names throughout.

## Next

<div class="not-content">

- [MCP tools](/docs/user/reference/tools/) is the exact tool surface.
- [Questions and answers](/docs/user/reference/faq/) covers the first-week questions.
- [The data model](/docs/dev/store/data-model/) is where these terms become tables.

</div>
