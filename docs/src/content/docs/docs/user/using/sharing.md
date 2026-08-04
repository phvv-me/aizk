---
title: "Sharing and organizations"
description: "Writing into a team scope, sharing an existing note, and what an intersection means."
---

Read [Scopes](/docs/user/concepts/scopes/) first. It explains why a memory carries a set of
organizations and why naming two narrows it. This page is the practical part.

:::note[Where this comes from]
The split between private and shared memory follows
[Collaborative Memory](https://arxiv.org/abs/2505.18279). The intersection model, where naming two
teams narrows a memory instead of widening it, is aizk's own. The full
[map of sources](/docs/dev/prior-art/references/) has the rest.
:::

## Check your standing first

Before the first shared write in a task, ask for your standing. `status` tells you who you are and,
for every organization, your roles, its purpose, its members, whether it is public, and whether you
may write to it.

```text
aizk.status(days=30)
```

One field decides everything, and that is `writable`. Read it rather than assuming.

```text
  organization     member?   writable?   what I can do
  ───────────────────────────────────────────────────────
  Book Club          yes        yes       read and write
  Sci-Fi Circle      yes        no        read only
  Docs               no         no        read only, it is public
```

Being on a team is not the same as being able to add to it. A viewer sees everything the team
stores and adds nothing.

## Name organizations exactly

Organization names come from your identity provider and aizk keeps no copy, so your call has to
match character for character. `Book Club` is not `book-club` and not `Book club`. Copy it from what
`status` returned rather than from memory.

## One name for a team, several for an overlap

Passing one organization writes into that team's memory.

```text
aizk.keep(
  text="# Meeting nights\n\nWe meet the first Monday of each month through August.",
  scopes=["Book Club"],
)
```

Passing two writes something only people who belong to both can read.

```text
aizk.keep(
  text="# Joint sci-fi evening\n\nThe two clubs co-host one science fiction pick each quarter.",
  scopes=["Book Club", "Sci-Fi Circle"],
)
```

Reach for an intersection only when the knowledge belongs to the overlap and both organizations
came back writable. It is the wrong home for anything either team would want alone, because a member
of only one never sees it.

## Write where it belongs, do not move it later

The habit that saves the most trouble is writing straight into the destination. A note a teammate
will need should go to the team on the first call, because sharing an existing document copies it
rather than moving it.

```text
   private note  ──── share ────▶  team snapshot
        │                              │
        │ you edit it later            │ unchanged
        ▼                              ▼
   your version                   the team's version
   moves on                       stays where it was
```

`share` takes one destination and either document IDs or a question that selects them.

```text
aizk.share(documents=["019b2d0a-1d42-7d6e-a9aa-8f8443ec6f4a"], scopes=["Book Club"])
```

The IDs are the ones `keep` returned, and also the ones `find` prints under every piece of
evidence, so you can ask a question, read the documents behind the answer, and share exactly those.
A `query` never writes. It answers which of your private documents it would take, so you read that
list and then call again naming the IDs you approve.

Your private original stays exactly as it was. The team's copy keeps its provenance so people can
see where it came from, later edits to your version never reach it, and a shared file reuses the
same stored bytes while getting its own scoped record.

That trade is deliberate. A team can rely on a shared note staying stable rather than shifting
because one person revised a private copy. It also makes a copy a fork, and forks drift, which is
why you write into the scope in the first place.

## Moving a topic you already wrote privately

Sometimes the fork is exactly what you do not want. A body of private notes turns out to belong to a
team, and the team's copy should be the only one anyone recalls. That is `move`, and it takes two
calls.

```text
aizk.share(query="interpretability", scopes=["CVLAB Interpretability"])
aizk.share(documents=["019b..."], scopes=["CVLAB Interpretability"], move=True)
```

The first answers which of your private documents it would take, at most `limit` of them, never
reaching into an organization, and writes nothing. Read those titles, then name the IDs you approve.
A query cannot move, and asking it to is refused rather than ignored, so a refusal never reads as a
move that happened.

A move copies into the destination first and then retires the private original, so the note keeps
its rows, its bytes, and its provenance chain while ordinary recall returns only the team's copy.
Both halves commit together, so a move that fails partway leaves nothing to reconcile, and running
the same move twice changes nothing. If you revised the note after an earlier share, the move
refreshes the team's copy onto your current text first, so it can never strand the team on an older
draft.

Because a move takes something out of recall, it only ever touches your own private documents. It
can never pull evidence out from under the other members of a shared scope, and it needs a real
organization as its destination, since moving a private note into the private scope it already
occupies is not a move at all.

## The Docs organization

`Docs` is the public organization for durable, agent-maintained findings about tools, libraries,
languages, aizk itself, onboarding, and note-taking. When you work out how a tool behaves and the
next person will hit the same wall, that belongs in `Docs`, refreshed when the tool changes rather
than left to rot beside a newer note contradicting it.

What does not belong there is anything about a project, customer, person, or credential.

:::caution[Public means everyone]
Public grants read access to every signed-in user and nothing else. Writing still needs a member
role, so public never means writable. Never make a private collaboration public, because there is no
partial switch and no way to learn afterward who read what while it was open.
:::

## Membership lives outside aizk

aizk does not own users, organizations, roles, or membership. Your identity provider does, and aizk
reads your standing from the token on every call. Somebody joining a team sees its memory on their
next question and loses it the moment they leave. One place manages access, so the two systems
cannot disagree.

Creating an organization, adding a member by email, and moving somebody between viewer, editor and
admin all happen on the organizations screen in [The web app](/docs/user/using/web-app/), when your
role allows it.

## Next

<div class="not-content">

- [Scopes](/docs/user/concepts/scopes/) is the model this page applies.
- [The web app](/docs/user/using/web-app/) is where you manage members and roles.
- [Who maintains memory](/docs/user/concepts/lifecycle/) covers who is responsible for shared notes.

</div>
