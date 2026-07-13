<div class="hero" markdown>

![aizk logo](assets/logo.svg){ .hero-logo }

# aizk

A self-hosted shared memory engine for teams and overlapping projects

</div>

## What it does

Aizk turns notes, messages, files, and session memory into sourced context an agent can recall over
MCP. PostgreSQL stores raw chunks, working memory, immutable graph content, bi-temporal claims,
profiles, and summaries. Dense retrieval, lexical retrieval, and optional graph lanes return one
bounded context pack.

The harder problem is sharing. A person may work in a Toshiba organization with one set of lab
members and a Spread organization with another. A memory may belong to either organization or to
their intersection. Aizk represents that directly as a nonempty set of scope UUIDs. PostgreSQL row
level security checks those sets on every read and write.

## Identity and perspective

Logto is the only source of users, organizations, roles, and public organization metadata. Aizk
derives stable UUIDs from verified token claims and stores no identity, membership, organization,
role, or owner authorization tables.

Authorization scope is not speaker meaning. A shared message also keeps an immutable capture
snapshot with the author label, role, channel, reply, phase, topic, and source time. Objective facts
may consolidate across the shared scope. Experiences, observations, opinions, and preferences stay
separate by speaker so collaborators can disagree without corrupting each other's memory.

## The surface

The network-facing MCP server has four tools. `recall` returns a budgeted context pack. `remember`
captures working memory. `reference` records an external source. `share` creates a
provenance-linked copy in one authorized destination without changing the source. Setup, ingest,
graph maintenance, evaluation, backup, and restore remain operator-only CLI commands.

```python
from fastmcp import Client

async with Client("http://localhost:8000/mcp") as client:
    await client.call_tool("remember", {"text": "The team selected the current assay plan."})
    result = await client.call_tool("recall", {"query": "What assay plan did we select?"})
    print(result.data)
```

## Design sources

The temporal graph follows work from Zep and Graphiti. Consolidation borrows the add, update, and
duplicate framing from Mem0 but resolves confident cases by rule. HippoRAG, GraphRAG, and RAPTOR
informed optional retrieval lanes. GroupMemBench motivates explicit speaker and asker semantics.
Memora motivates forgetting-aware evaluation. Recent ACL evidence that flat structured keys can
beat graph-heavy context is why every optional lane must earn its cost in an ablation.

See [Concepts](concepts.md), [Engine](engine/index.md), [API](api.md),
[Benchmarks](benchmarks.md), [Comparison](comparison.md), and [Provenance](provenance.md).

## Install and run

```sh
pip install aizk
docker compose up
aizk serve-mcp
```
