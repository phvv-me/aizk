---
title: "The bi-temporal model"
description: "Valid time, recorded time, and the constraint that makes a correction append-only."
---

Every fact claim carries two independent time ranges. This page explains what they mean, the
one index that makes corrections append-only, and how a query replays a belief the system no
longer holds. It assumes you know that a claim is the scoped assertion and the content is the
immutable statement, which [The data model](/docs/dev/store/data-model/) covers. The code is
`src/aizk/store/models/tables/fact.py`.

:::note[Where this comes from]
Keeping valid time and recorded time apart on an immutable graph is adopted from
[Zep and Graphiti](https://arxiv.org/abs/2501.13956). The append-only correction, closing a range
instead of overwriting it, is enforced here by one index rather than by convention. The full map
is at [References and lineage](/docs/dev/prior-art/references/).
:::

## Two ranges, two questions

```python
valid = sql.Field(Range[datetime] | None, sa_type=TSTZRANGE)
recorded = sql.Field(
    Range[datetime],
    sa_type=TSTZRANGE,
    server_default=func.tstzrange(func.now(), None, "[)"),
)
```

`valid` answers when the fact held in the world. It is nullable, because plenty of statements
are simply true with no known window. `recorded` answers when the system believed it, and it is
never null. The server default opens it at the database's own `now()` with no upper bound, so
a row written without touching the column is live from the instant PostgreSQL accepted it. That
matters because the clock is always PostgreSQL's, never the application host's.

```text
                  recorded (what the system believed)
                  ├────────────── v1 ───────────────┤
                                                    ├───── v2, open ─────▶
      ────────────┼──────────────┼──────────────────┼──────────────────────▶
                  Jan               Mar             May            wall clock

                  valid (when it held in the world)
                  ├──────────── "works at Acme" ────┤
                                                    ├─ "works at Globex" ─▶

      point-in-time read at Mar  ▲  sees v1 only, the belief of that day
```

Closing v1 and opening v2 is one correction. Nothing was overwritten and nothing was deleted,
so the March reader still gets the March answer.

## The constraint that does the work

Four indexes sit on `fact_claim`. Two are plain GiST indexes over `valid` and `recorded` for
range containment. One is partial.

```python
Index("ix_fact_claim_live", "valid", postgresql_using="gist",
      postgresql_where=func.upper_inf(SAColumn("recorded"), type_=Boolean))
Index("uq_fact_claim_live", "content_id", "scopes", "perspective_key",
      unique=True,
      postgresql_where=func.upper_inf(SAColumn("recorded"), type_=Boolean))
```

`ix_fact_claim_live` keeps the hot path small, since only open claims are indexed and history
never grows it. `uq_fact_claim_live` is the load-bearing one. It says that for a given
statement, scope set and perspective there may be at most **one** claim whose `recorded` range
is still open. Any number of closed versions may sit beneath it.

That single index buys three things at once. A duplicate write cannot create a second live
claim, so `FactWriter` can insert a whole batch with `on_conflict_do_nothing` arbitrating on
exactly this index through `index_where=Fact.Claim.recorded.f.upper_inf()`. A correction has to
close the old row before the new one can land, which is what makes the history append-only by
construction rather than by convention. And two scope sets can hold contradicting live claims
on the same content at the same time without seeing each other, because `scopes` is part of the
key.

## Closing versus deleting

Nothing in the fact graph is deleted. Three methods close ranges instead, and each stamps the
reason into the claim's `attributes` bag so the history explains itself.

| Method | What it closes | Marker written |
|---|---|---|
| `revise` | the claim a correction supersedes | none, the new claim carries the story |
| `archive_stale` | live claims below a relevance floor | `decayed` with the timestamp |
| `retract_from_documents` | live claims derived from changed or forgotten sources | the caller's reason, `forgotten` for a forget |

All three close with `tstzrange(recorded.lower, now())`, so the lower bound is preserved and
the upper bound becomes the database's current instant. `archive_stale` computes decay entirely
in SQL, `power(0.5, half_lives) * (1 + access_count)`, so the clock, the range close and the
staleness decision all come from one statement with no host-side time conversion.

`revise` has one wrinkle worth knowing. A backdated correction, one whose `valid_from` is
earlier than the standing claim's lower bound, leaves the standing claim untouched and instead
trims the incoming claim's `valid_to` down to that lower bound. A forward correction closes
`valid` at `greatest(valid_from, lower)` and closes `recorded` at `now()`. It runs as one
`UPDATE` over a typed `VALUES` relation so a whole batch of corrections is one round trip.

## Reading the present, and reading the past

`FactClaim.is_current` is a hybrid property, which means it works both in Python and as SQL. It
is `upper_inf(recorded)` and either a null `valid` or a `valid` range containing `now()`. The
`live_fact` view bakes the same predicate into its `WHERE`, described in
[Graph tables](/docs/dev/store/graph-tables/).

Because it would be easy to forget, the gate is also applied automatically. A
`do_orm_execute` listener in `src/aizk/store/events.py` attaches
`with_loader_criteria(Fact.Claim, lambda cls: cls.is_current)` to every top-level ORM select.
A statement that genuinely wants history opts out through one execution option.

```python
select(Fact.Claim).execution_options(**{settings.skip_live_gate: True})
```

A point-in-time read swaps the predicate rather than the table. `FactClaim.visible_at(as_of)`
returns the two predicates that reconstruct a past belief.

```python
if as_of is None:
    return (cls._is_current_predicate(),)
return (
    or_(cls.valid.is_(None), cls.valid.contains(as_of)),
    cls.recorded.contains(as_of),
)
```

Read that as two questions asked together. Did the fact hold in the world at that moment, and
did the system believe it at that moment. Both are range containment, both are served by the
GiST indexes, and the answer is the belief of that day rather than today's belief filtered
backward. The two questions being separable is the whole point of keeping two ranges instead of
one `updated_at`.

## Next

<div class="not-content">

- [Graph tables](/docs/dev/store/graph-tables/) has the rest of the `fact_claim` columns and the view.
- [Grounding and consolidation](/docs/dev/write/consolidation/) shows who decides a correction is needed.
- [Profiles, insights, decay](/docs/dev/passes/profiles-insights/) covers the pass that calls `archive_stale`.
- [Time and history](/docs/user/concepts/time/) is the same idea without SQL.

</div>
