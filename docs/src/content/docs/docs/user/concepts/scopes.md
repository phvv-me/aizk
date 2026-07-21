---
title: "Scopes"
description: "Who can read a memory, expressed as a set of organizations rather than a permission list."
---

A scope is the answer to "who can read this". Every single thing aizk stores carries one, and it
is the only mechanism controlling visibility. This page assumes you know roughly
[what aizk is](/docs/user/what-is-aizk/) and nothing else.

## A scope is a set, not a setting

Most tools give a document an owner and then a list of people allowed in. aizk does something
different. Each memory carries a **set of organizations**, and you can read it only if you
belong to every organization in that set.

That one rule produces everything else.

```text
        ┌───────────────┐
        │  { A , B }    │   both organizations
        │  intersection │   readable only by people in A and in B
        └───────┬───────┘
    ┌───────────┴───────────┐
┌───┴───┐               ┌───┴───┐
│ { A } │ one team      │ { B } │ one team
└───┬───┘               └───┬───┘
    └───────────┬───────────┘
         ┌──────┴──────┐
         │  { you }    │   private, and the default
         └─────────────┘

  membership in A alone does not open { A , B }
```

Name nothing and the memory is private to you. Name one organization and that team can read it.
Name two and only the people who belong to both can, which is narrower than either team on its
own, not wider. That is the part people usually get backwards on first reading. Adding a second
organization to a note **restricts** it.

## Why intersections are the useful part

Say you work at a company and also collaborate with a university on one project. Some of what
you learn belongs to the company, some belongs to the university, and some belongs specifically
to the thing the two are doing together.

Without intersections you would have to pick one home for that third category and hope nobody
minds, or keep two copies and watch them drift. With intersections it has a real home. A note
scoped to both is invisible to a colleague who is only in the company and invisible to a
professor who is only at the university, and it appears for the handful of people actually on
the joint project.

## Reading spans everything you can see

You never choose a scope when you ask a question. One question searches everything visible to
you at once, which is your private memory plus every team scope you stand in plus every
intersection you qualify for.

The evidence that comes back tells you where each piece lived. An item might say it came from
your private memory, or from one organization, or from the overlap of two. That labeling is what
lets you tell a personal note from a team decision without asking.
[Evidence and provenance](/docs/user/concepts/evidence/) covers how to read those labels.

## Writing names one destination

Writing works the other way around. A memory goes to exactly one place, and the default is
private. Your assistant has to name organizations deliberately for anything else to happen, and
it can only name organizations you are actually allowed to write to. Being able to read a team's
memory does not imply being able to add to it.

The practical habit is that personal working notes stay private, and anything a teammate would
need goes straight into the team scope rather than being written privately and moved later.

## Sharing copies, it does not move

If something private turns out to be worth sharing, your assistant can share it into a team
scope. This makes a copy. The private original stays yours and stays unchanged, and later edits
to your private version do not update the shared copy.

That is a deliberate trade. It means a shared note is a stable thing a team can rely on rather
than something that can change under them because one person revised their own copy.
[Sharing and organizations](/docs/user/using/sharing/) walks through doing it.

## Public organizations

An organization can be marked public, which lets any signed-in user read it. It never lets them
write. Public is for shared reference material that is genuinely meant for everyone, and it is
the one thing you should never turn on for a private collaboration.

## Who decides membership

aizk does not. Organizations, members and roles all live in Logto, the identity system your
deployment runs, and aizk keeps no copy of them. When somebody joins a team there, their next
question to aizk sees that team's memory. When they leave, it stops.

This matters because it means there is exactly one place to manage access, and no chance of aizk
and the identity system disagreeing about who is on a team.

## The rule underneath

The check is not a filter the application applies before returning results. PostgreSQL evaluates
it on every row of every table, with the setting that would let the application bypass it turned
off entirely.

The practical consequence is that a bug in aizk cannot leak a memory across a boundary. Code that
forgets to filter returns nothing rather than returning everything. If you want the mechanics,
[Row level security](/docs/dev/store/rls/) has them.

## Next

<div class="not-content">

- [Sharing and organizations](/docs/user/using/sharing/) is the practical how-to.
- [Time and history](/docs/user/concepts/time/) covers the other dimension every memory carries.
- [Scope sets in depth](/docs/dev/identity/scope-sets/) is the developer version of this page.

</div>
