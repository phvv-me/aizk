---
title: "Questions and answers"
description: "The questions people actually ask after their first week."
---

The questions that come up in the first week, answered plainly. This page assumes you have read
[What aizk is](/docs/user/what-is-aizk/). Where the honest answer is a limitation, it says so.

## Does it read my files?

No. aizk never scans a disk, a repository, or a folder. It holds exactly what was handed to it through
`remember` and nothing else.

The boundary is one step further out, though. Your agent can read your files and decides what to send,
so the rule that actually protects you is the one in your own agent instructions, which
[Claude Code](/docs/user/clients/claude-code/) writes out, including the line about never storing
secrets. aizk enforces nothing about what your agent hands it.

## What happens if I delete something?

There is no delete tool, and that is a real limitation. Correction is the mechanism instead. You store
an updated statement, the new version becomes current, and the old one keeps its dates and stops
appearing in ordinary recall. [Time and history](/docs/user/concepts/time/) covers it.

If something genuinely has to go, an operator can retract what a source contributed and remove the
source at the database. That is a real capability, but it is an operator command, not something you or
your agent can do from a chat.

## Can I get my data out?

Yes. Everything you can see exports to a plain JSONL file, sources and chunks and derived knowledge
together, and preserved originals come back byte for byte. Underneath it all is one PostgreSQL database that you own. The one catch is that the export is an
operator command today rather than a button, so a copy means asking whoever runs the deployment.

## Why did recall not find my note?

Four usual causes, most common first.

```text
  recall missed it
    │
    ├─ stored in the last few minutes ─▶ still processing, check status
    │
    └─ older than that
          ├─ other items came back       ─▶ it lost on ranking or the token budget
          │
          └─ nothing came back
                ├─ it lives in someone else's scope ─▶ you are not in that organization
                └─ it is yours, with an expiry       ─▶ past its expiry, history keeps it
                    that has passed
```

Writing returns before the note is searchable, so `status` gives a range for when new material becomes
recallable. Check it before concluding anything is wrong. If other things came back but not the one you
wanted, it was found and beaten, since recall returns the best items that fit a token budget and a
thin note can lose to a rich one. A more specific question usually fixes it, and
[Asking memory well](/docs/user/using/recall/) has the phrasing advice. If nothing relevant came back
at all, check whether the note lives in an organization you belong to, and whether it carried an
expiry that has since passed.

## Why does it return evidence instead of an answer?

Because an answer cannot be checked and evidence can. A single sentence hides which note produced it,
when it was written, and whether it was your words or an inference. Evidence keeps all three, so your
agent can prefer a source excerpt, notice that two items disagree, and tell you which is newer. It also
keeps aizk out of the reasoning business, a job the assistant already does well.
[Evidence and provenance](/docs/user/concepts/evidence/) goes into the labels.

## How is it different from putting notes in a repository?

Four differences that matter, and one honest concession. A repository note is findable by exact string,
while aizk is findable by meaning, which is what you have when you half remember a decision from six
months ago. A repository has no notion of who may read what beyond the whole repository, while aizk
carries a scope on every row and the database enforces it. A repository has one timeline, the commit
log, while aizk tracks when a file changed and when a statement was true separately. A repository note
is trapped in one project, while aizk answers across everything you can see at once.

The concession is that for a small project, files plus grep are simpler and faster. aizk earns its
setup once memory has to cross projects, people, or years. See [Scopes](/docs/user/concepts/scopes/).

## Does the model see my private memory?

The models aizk itself runs, for embedding and extraction, run on the deployment's own hardware. Your
text is not sent to a model vendor by aizk.

Your assistant is a different matter. When your agent calls `recall`, the evidence enters that agent's
context, and if the agent is hosted then that text travels to its provider like everything else in the
conversation. aizk does not change what your assistant does with what it reads, so if that matters for
a note, the decision belongs at the point of asking.

## What happens when two people write the same thing?

Both sources are kept. Nobody's words overwrite anybody's words. The derived knowledge converges
instead, so when two notes produce the same statement, one shared copy exists and each scope holds its
own claim with its own dates.

Statements tied to a speaker behave differently on purpose. An opinion, a preference, an observation,
and a personal experience stay attached to whoever said them, so two people can hold opposing views
without one erasing the other. Statements about the shared world do not get that, and a later one
supersedes an earlier one. [Entities, facts, ontology](/docs/user/concepts/graph/) has the detail.

## Is there a review step?

No, and there is not going to be one. Nothing sits in a queue between `remember` and the moment that
memory can be recalled. There is no approval state and no human gate.

That is deliberate. A review queue only works if somebody drains it, and a memory that fills faster
than a person can approve becomes a backlog. The correctness work moves to the agents instead. They
recall before writing, update a maintained note rather than adding a competing one, and correct what
changed. [Who maintains memory](/docs/user/concepts/lifecycle/) explains the arrangement, and
[Notes that stay useful](/docs/user/using/habits/) is the habit list.

## Next

<div class="not-content">

- [Glossary](/docs/user/reference/glossary/) defines every term used here.
- [Notes that stay useful](/docs/user/using/habits/) is how to keep memory worth reading.
- [Sign-in troubleshooting](/docs/user/clients/troubleshooting/) covers connection problems.

</div>
