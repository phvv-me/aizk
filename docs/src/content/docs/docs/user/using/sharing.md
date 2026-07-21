---
title: "Sharing and organizations"
description: "Writing into a team scope, sharing an existing note, and what an intersection means."
---

Read [Scopes](/docs/user/concepts/scopes/) first. It explains why a memory carries a set of
organizations and why naming two makes it narrower, not wider. This page is the practical part,
getting a note into the right team without guessing.

:::note[Where this comes from]
The split between private and shared memory follows
[Collaborative Memory](https://arxiv.org/abs/2505.18279). The intersection model, where naming two
teams narrows a memory instead of widening it, is aizk's own. The full
[map of sources](/docs/dev/prior-art/references/) has the rest.
:::

## Check your standing first

Before the first shared write in a task, ask for your standing. `status` tells you who you are and,
for every organization you belong to, your roles, what the team is for, who else is in it, whether it
is public, and whether you may write to it.

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

Being on a team is not the same as being able to add to it. Reading and writing are separate
permissions. A viewer sees everything the team stores and adds nothing.

## Name organizations exactly

Organization names come from your identity provider and aizk keeps no copy, so the name in your call
has to match the real one character for character. `Book Club` is not `book-club` and not `Book club`.
Copy it from what `status` returned rather than from memory.

## One name for a team, several for an overlap

Passing one organization writes into that team's memory.

```text
aizk.remember(
  text="# Meeting nights\n\nWe meet the first Monday of each month through August.",
  scopes=["Book Club"],
)
```

Passing two writes something only people who belong to both can read.

```text
aizk.remember(
  text="# Joint sci-fi evening\n\nThe two clubs co-host one science fiction pick each quarter.",
  scopes=["Book Club", "Sci-Fi Circle"],
)
```

Reach for an intersection only when the knowledge genuinely belongs to the overlap, and only when
both organizations came back writable. It is the right home for one specific collaboration and the
wrong home for anything either team would want on its own, because a member of only one of them will
never see it.

## Write where it belongs, do not move it later

The habit that saves the most trouble is writing straight into the destination. A note a teammate
will need should go to the team on the first call, not privately with a plan to share it once it is
polished.

That is because sharing an existing document makes a copy rather than moving it.

```text
   private note  ──── share ────▶  team snapshot
        │                              │
        │ you edit it later            │ unchanged
        ▼                              ▼
   your version                   the team's version
   moves on                       stays where it was
```

`share` takes the document IDs that `remember` returned and one destination.

```text
aizk.share(documents=["019b2d0a-1d42-7d6e-a9aa-8f8443ec6f4a"], scopes=["Book Club"])
```

Your private original stays yours and stays exactly as it was. The copy in the team keeps its
provenance so people can see where it came from, and later edits to your version never reach it. A
shared file reuses the same stored bytes rather than duplicating them, while still getting its own
scoped record.

That is a deliberate trade. A team can rely on a shared note being stable rather than shifting under
them because one person revised a private copy. It also means a copy is a fork, and forks drift,
which is the whole reason to write into the scope in the first place.

## The Docs organization

`Docs` is the public organization for durable, agent-maintained findings about tools, libraries,
languages, aizk itself, onboarding, and note-taking. When you work something out about how a tool
really behaves and the next person will hit the same wall, that belongs in `Docs`, and it should be
refreshed when the tool changes rather than left to rot beside a newer note that contradicts it.

What does not belong there is anything about a project, a customer, a person, or a credential.

:::caution[Public means everyone]
Public grants read access to every signed-in user and nothing else. Writing still needs a member
role, so public never means writable. Never make a private collaboration public, because there is no
partial switch and no way to learn afterward who read what while it was open.
:::

## Membership lives outside aizk

aizk does not own users, organizations, roles, or membership. Your identity provider does, and aizk
reads your standing from the token on every call. So when somebody joins a team, their next question
sees that team's memory, and when they leave it stops at once. There is one place to manage access
and no chance of the two systems disagreeing.

Creating an organization, adding a member by email, and moving somebody between viewer, editor, and
admin all happen on the organizations screen in [The web app](/docs/user/using/web-app/), when your
own role allows it.

## Next

<div class="not-content">

- [Scopes](/docs/user/concepts/scopes/) is the model this page applies.
- [The web app](/docs/user/using/web-app/) is where you manage members and roles.
- [Who maintains memory](/docs/user/concepts/lifecycle/) covers who is responsible for shared notes.

</div>
