---
title: "Sharing and organizations"
description: "Writing into a team scope, sharing an existing note, and what an intersection means."
---

This page assumes you have read [Scopes](/docs/user/concepts/scopes/), which explains why a memory
carries a set of organizations and why naming two makes it narrower rather than wider. Here we do
the practical part, which is getting a note into the right team scope without guessing.

## Check your standing first

Before the first shared write in a task, call `status`. It answers who you are and, for every
organization you belong to, what your roles are, what the organization is for, who else is in it,
whether it is public, and whether you may write to it.

```json
{ "days": 30 }
```

The field that decides everything is `writable`. Read it rather than assuming.

```text
  organization        member?   writable?   can I write there?
  ─────────────────────────────────────────────────────────────
  Research Lab          yes        yes           yes
  Toshiba               yes        no            no, read only
  Docs                  no         no            no, but readable
```

Being in a team is not the same as being able to add to it. Reading and writing are separate
permissions, and a viewer sees everything the team stores while adding nothing.

## Name organizations exactly

Organization names come from your identity provider and aizk keeps no copy of them, so the name in
your call has to match the real one character for character. `Research Lab` is not `research-lab`
and not `Research lab`. Take the name from what `status` returned rather than from memory.

## One name for a team, several for an overlap

Passing one organization writes into that team's memory.

```json
{
  "text": "# Plate reader booking\n\nThe reader is booked Mondays and Wednesdays through August.",
  "scopes": ["Research Lab"]
}
```

Passing two writes something only people who belong to both can read.

```json
{
  "text": "# Joint assay milestones\n\nThe university runs the statistics and we run the plates.",
  "scopes": ["Research Lab", "Kyoto Chemistry"]
}
```

Use an intersection only when the knowledge genuinely belongs to the overlap, and only when both
organizations came back writable. It is the right home for one specific collaboration and the wrong
home for anything either team would want on its own, because a colleague in only one of them will
never see it.

## Write where it belongs, do not move it later

The habit that saves the most trouble is writing straight into the destination scope. A note a
teammate will need should go to the team on the first call, not privately with a plan to share it
once it is polished.

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

```json
{
  "documents": ["019b2d0a-1d42-7d6e-a9aa-8f8443ec6f4a"],
  "scopes": ["Research Lab"]
}
```

Your private original stays yours and stays exactly as it was. The copy in the team scope keeps its
provenance so people can see where it came from, and later edits to your private version do not
reach it. A shared file reuses the same stored original rather than duplicating the bytes, while
still getting its own scoped record.

That is a deliberate trade. It means a team can rely on a shared note being stable rather than
changing under them because one person revised their own copy. It also means a copy is a fork, and
forks drift, which is the reason to write into the scope in the first place.

## The Docs organization

`Docs` is the public organization for durable, agent-maintained findings about tools, libraries,
languages, aizk itself, onboarding, and note-taking. When you work something out about how a tool
actually behaves and the next person will hit the same thing, that belongs in `Docs`, and it should
be refreshed when the tool changes rather than left to rot beside a newer contradicting note.

What does not belong there is anything about a project, a customer, a person, or a credential.
Public means every signed-in user can read it, so treat it as if it were on a wall in a corridor.

## Public never means writable

Marking an organization public grants read access to every authenticated user and grants nothing
else. Writing still requires being a member with the right role, and public status does not change
that.

Never make a private collaboration organization public. There is no partial version of the switch,
no per-note exception, and no way to work out afterward who read what while it was open. Public is
for reference material meant for everyone from the start.

## Membership lives outside aizk

aizk does not own users, organizations, roles, or membership. Your identity provider does, and aizk
reads standing from the token on every call. So when somebody joins a team, their next question to
aizk sees that team's memory, and when they leave, it stops immediately. There is exactly one place
to manage access and no chance of the two systems disagreeing.

Creating an organization, adding a member by email, and changing somebody between viewer, editor,
and admin all happen on the organizations screen described in
[The web app](/docs/user/using/web-app/), when your own role allows it.

## Next

<div class="not-content">

- [Scopes](/docs/user/concepts/scopes/) is the model this page applies.
- [The web app](/docs/user/using/web-app/) is where you manage members and roles.
- [Who maintains memory](/docs/user/concepts/lifecycle/) covers who is responsible for shared notes.

</div>
