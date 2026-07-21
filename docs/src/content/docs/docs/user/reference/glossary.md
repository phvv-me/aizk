---
title: "Glossary"
description: "Every term these docs use, defined once."
---

Every word these docs lean on, in one place, each linked to the page that owns it. Read
[What aizk is](/docs/user/what-is-aizk/) first if none of this is familiar yet.

Most terms trace one path, from something you wrote to something an agent gets back.

```text
   remember ──▶ document ──▶ chunk ──▶ entity + fact ──▶ community + profile
                (source)                  (derived memory)
        │                                        │
        └──────────── every row carries a scope ─┘
                              │
                           recall ──▶ evidence
```

## A to E

**Agent** is the assistant calling aizk, such as Claude Code or Codex. aizk stores and retrieves, the
agent talks. See [What aizk is](/docs/user/what-is-aizk/).

**Artifact** is one preserved original file with its revisions, kept byte for byte. See
[Files, PDFs and web sources](/docs/user/using/files/).

**Blob** is the stored bytes behind an artifact. Identical bytes are kept once and shared. See
[Content and artifact tables](/docs/dev/store/content-tables/).

**Chunk** is one ordered span of a document, the unit that gets embedded and searched. See
[Chunking and embedding](/docs/dev/write/chunking/).

**Claim** is one scope's assertion of a shared piece of derived knowledge, with its own dates and
counts. Two teams can claim the same statement. See [the graph](/docs/user/concepts/graph/).

**Community** is a cluster of related entities with a label and a summary, used for broad questions.
The web app calls these Themes. See [the graph](/docs/user/concepts/graph/).

**Content** is the deduplicated wording of an entity or a fact, stored once and pointed at by every
claim on it. See [Graph tables](/docs/dev/store/graph-tables/).

**Decay** is the background pass that archives derived knowledge nobody has touched in a long time. It
leaves recall and stays in history. See [Who maintains memory](/docs/user/concepts/lifecycle/).

**Derived memory** is everything aizk worked out from your text, and it can be rebuilt. See
[Sources and derived knowledge](/docs/user/concepts/sources/).

**Document** is one remembered note or file as a source item, and the parent of its chunks. See
[Sources and derived knowledge](/docs/user/concepts/sources/).

**Entity** is a thing being talked about, such as a person, a project, a tool, or a result. The web
app calls these Subjects. See [the graph](/docs/user/concepts/graph/).

**Evidence** is what recall returns, a ranked list of relevant items each labeled with where it came
from. It is not an answer. See [Evidence and provenance](/docs/user/concepts/evidence/).

**Expiry** is a known time after which a statement stops being true. Past it, ordinary recall skips
the source while history keeps it. See [Time and history](/docs/user/concepts/time/).

## F to P

**Fact** is a statement connecting entities, such as one project using one tool. The web app calls
these Findings. See [the graph](/docs/user/concepts/graph/).

**Gate** is the cheap relevance check that decides whether a chunk is worth full extraction. See
[Extraction and the gate](/docs/dev/write/extraction/).

**Intersection** is a memory scoped to two or more organizations, readable only by people in all of
them. Adding an organization narrows a memory. See [Scopes](/docs/user/concepts/scopes/).

**MCP** is the Model Context Protocol, the standard your agent speaks to reach aizk. It turns aizk
into four tools. See [MCP tools](/docs/user/reference/tools/).

**Ontology** is the vocabulary of entity kinds and relation kinds that extraction may use, which
keeps the graph from renaming the same idea. See [the graph](/docs/user/concepts/graph/).

**Organization** is a team you belong to, defined in the identity system rather than inside aizk.
Naming one is how sharing happens. See [Sharing and organizations](/docs/user/using/sharing/).

**Perspective** separates claims that belong to a speaker from claims about the shared world. An
opinion stays attached to whoever said it, so two people can differ without overwriting each other.
See [the graph](/docs/user/concepts/graph/).

**Profile** is a maintained summary of everything currently known about one entity. See
[Profiles, insights, decay](/docs/dev/passes/profiles-insights/).

**Projection** is the background work that turns a stored source into derived memory, which is why a
new note takes a moment to become findable. See
[Sources and derived knowledge](/docs/user/concepts/sources/).

**Provenance** is the label on each evidence item saying where it came from and which scope holds it.
See [Evidence and provenance](/docs/user/concepts/evidence/).

## R to Z

**RAPTOR** is the pass that builds layered summaries so a broad question can be answered from a
summary rather than a hundred fragments. See
[Communities and RAPTOR](/docs/dev/passes/communities-raptor/).

**Recall** is the tool that takes one question and returns evidence. It searches everything you can
see and takes no scope selector. See [Asking memory well](/docs/user/using/recall/).

**Recorded time** is when aizk was told something, as opposed to when it was true. Correcting a note
opens a new recorded window. See [Time and history](/docs/user/concepts/time/).

**Remember** is the tool that stores a note, preserves an original, or prepares a file upload. See
[Writing memory well](/docs/user/using/remember/).

**Scope** is the answer to who can read a memory, expressed as a set of organizations. Naming none
keeps it private. See [Scopes](/docs/user/concepts/scopes/).

**Scope set** is the same idea named precisely, the exact sorted set of organizations a row carries
and the thing the database checks on every read. See
[Scope sets in depth](/docs/dev/identity/scope-sets/).

**Session memory** is recent working material not yet promoted into the long-term graph. Recall labels
it separately. See [Evidence and provenance](/docs/user/concepts/evidence/).

**Share** is the tool that copies documents you can see into a team scope. It copies rather than
moves, so your original stays yours. See [Sharing and organizations](/docs/user/using/sharing/).

**Source** is your own words, kept exactly as you wrote them. It is the authoritative half of what
aizk stores, and it wins whenever derived memory disagrees. See
[Sources and derived knowledge](/docs/user/concepts/sources/).

**Valid time** is when something was true in the world, as opposed to when aizk heard about it. See
[Time and history](/docs/user/concepts/time/).

## Next

<div class="not-content">

- [MCP tools](/docs/user/reference/tools/) is the exact tool surface.
- [Questions and answers](/docs/user/reference/faq/) covers the first-week questions.
- [The data model](/docs/dev/store/data-model/) is where these terms become tables.

</div>
