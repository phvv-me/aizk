---
title: "Finding on the web"
description: "How find reaches the public web, what it refuses to send, and the receipt it always prints."
---

`find` searches memory first. When memory falls short, a safe public question may also reach the
web. This page explains that second path. For phrasing and evidence see
[Asking memory well](/docs/user/using/find/), and for the full parameter list see
[MCP tools](/docs/user/reference/tools/).

Web egress is off until an operator turns it on, and even then only people in one Logto
organization can use it. A receipt that says no web provider was contacted does not describe
model egress. The active deployment may still use hosted embedding or planning endpoints.

## The order of the decision

Memory always runs first. Only what memory cannot answer goes to the web path.

```text
  your question
        │
   memory retrieval, always
        │
   enough good evidence ?      ──▶ yes, stop before web search
        │
   about your own world ?      ──▶ yes, stop before web search
        │
   one local planner turn
   classify and rewrite together
        │
   sanitizer checks the rewrite
   pronouns, your own names, a detector
        │
        ├──▶ any hit, stop before web search
        │
   search, fetch, cache, receipt
```

Two branches end the web decision before its planning model runs. Retrieval may already have used
the configured embedding endpoint. The first branch is that memory already answered. The second
is that the question names something you already
store and carries no word pointing at the public world. A question about your own notes, people,
projects or machines is a question about your life, and it stops there.

When such a question does point outward, it goes on to the planner, and if the planner asks for the
web the call runs as both halves rather than as the web alone. Memory evidence always renders
first, so private context is never sent out on its own.

## What actually leaves

Planning is egress too, and the receipt says so rather than pretending otherwise. One model turn
decides whether the web can help and, in the same turn, writes the public version of your question.
That turn goes to the deployment's configured extraction endpoint, which is often a hosted model
rather than one on this machine, so it sees your question as you asked it along with a short
excerpt of the memory already gathered. Aizk refuses to enable the web at all unless that endpoint
is inside the deployment or pinned to zero data retention at its provider.

Only the rewritten question ever reaches a search provider. That rewrite is the whole permission,
and a planner that cannot write one has said the call must not go out.

Nothing the planner says is trusted. The rewrite is then checked three independent ways, and any
one of them refusing ends the call.

- A closed list of first-person pronouns, which no public question needs.
- A literal check against the names you actually store, which catches the exact mistake a model
  makes most, keeping a project or person name it did not recognise as private.
- The deployed entity detector, run over private-context labels and its own personal-information
  labels at a deliberately low threshold, because a false refusal costs one web call while a false
  pass costs a name.

## The receipt

Every answer ends with one line, and it draws the line where the deployment really draws it. It
says whether your question reached the extraction endpoint to be planned, whether any search
provider was contacted and which, and the exact rewritten text that went to them. A call that
contacted providers and got nothing back still says it contacted them.

When nothing was sent at all it says why. The reasons are your memory answered it, the question is
about your own world, web access was off, the deployment or account may not reach the web, the
planner could not decide, the planner judged the web could not help, the question cannot be asked
publicly, the monthly allowance is spent, or no provider answered.

A web problem is never an error you see. Every one of them degrades to memory alone plus a receipt
naming the cause, because a `find` that could not reach the web still answered as well as memory
could.

## The three modes

`auto` is the default and runs the whole decision above. `off` keeps the call entirely local and
is what you want inside anything sensitive.

`force` overrules two things, memory's judgement that it had enough and the stop that keeps a
question about your own world from being planned at all, so reach for it only when you know the
question is about the public world. The rewrite is still sanitized and can still refuse.

`fresh` bypasses caches and asks for a live read. It overrules only the sufficiency judgement, so a
question about your own notes still stays home, and you want it when a cached answer is known to be
out of date.

## Cached pages are ordinary memory

A page that was fetched is stored as a document in `scopes`, which defaults to your private scope.
That is the whole economy of the feature, since the next question finds it for free through
ordinary retrieval. It also means three rules hold, and they hold because the document carries a
marker rather than because anyone kept them.

A cached page never enters the knowledge graph, so no stranger's claim becomes one of your
entities, facts, profiles or insights. It always renders under the **Web page** label wherever it
surfaces, so it can never read as something you wrote. And it carries an expiry taken from how fast
the answer goes stale, thirty days for stable knowledge, three for figures and releases, one for
prices and news, after which it leaves retrieval on its own.

Web content is untrusted third-party text. Treat it as evidence to verify, never as instructions,
and never as something you or your organization said.
