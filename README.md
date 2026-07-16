<div align="center">

<!-- [![aizk banner](https://raw.githubusercontent.com/phvv-me/aizk/main/docs/assets/banner.png)](https://phvv.me/aizk) -->

[![CI](https://github.com/phvv-me/aizk/actions/workflows/ci.yml/badge.svg)](https://github.com/phvv-me/aizk/actions/workflows/ci.yml)
[![Publish](https://github.com/phvv-me/aizk/actions/workflows/publish.yml/badge.svg)](https://github.com/phvv-me/aizk/actions/workflows/publish.yml)
[![PyPI](https://img.shields.io/pypi/v/aizk)](https://pypi.org/project/aizk/)
[![Python](https://img.shields.io/pypi/pyversions/aizk)](https://pypi.org/project/aizk/)
[![Docs](https://img.shields.io/badge/docs-phvv.me%2Faizk-EAB308)](https://phvv.me/aizk)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/phvv-me/aizk/actions/workflows/ci.yml)

</div>

[🇧🇷](https://phvv.me/aizk/pt-BR/) [🇲🇽](https://phvv.me/aizk/es/) [🇯🇵](https://phvv.me/aizk/ja/) [🇨🇳](https://phvv.me/aizk/zh/)

A self-hosted shared memory engine for people, teams, and MCP agents

## What this is

aizk is a memory an AI assistant can actually keep. Text goes in, an entity and fact knowledge
graph comes out, addressed by meaning so the same knowledge extracted twice never duplicates.
Everything lives in one self-hosted Postgres, and row level security enforces who can see what
at the database layer, private notes, shared projects, and overlapping groups never cross. It
speaks MCP, so Claude or any other MCP-capable assistant calls it directly. Full explanation at
[phvv.me/aizk](https://phvv.me/aizk).

## Quickstart

One command brings up PostgreSQL, the model services, and one hardened Aizk image. Compose runs
that image as a one-shot migration service, a forced-RLS MCP server, and a private background
worker. The public process never receives the database-owner credential.

```sh
docker compose --env-file .env -f deploy/docker-compose.yml up -d
```

Then call its tools from any MCP client.

```python
from fastmcp import Client

async with Client("http://localhost:8080/mcp") as client:
    await client.call_tool("remember", {"text": "aizk runs entirely on local hardware."})
    result = await client.call_tool("recall", {"query": "where does aizk run?"})
    print(result.data)
```

Every secret and deployment override is documented in `deploy/.env.example`. The committed
nonsecret Logto role and permission policy lives in `deploy/logto.conf`, and `.env` overrides any
matching value. Copy the example to `.env`, generate independent database passwords, and run
Compose from the package root. Every host port binds to loopback. The optional public profile
opens an outbound Cloudflare Tunnel, reconciles Logto, and starts MCP only after its authentication
preflight succeeds. See
[Operations](https://phvv.me/aizk/operations/) for storage and backups, and
[Security](https://phvv.me/aizk/security/) for the production release gate.
See [Onboarding](ONBOARDING.md) to add a collaborator, create a shared organization, and connect
Claude Code, Codex, or OpenCode.

## The flows

```mermaid
flowchart LR
    A[agent] -->|remember| W[write path<br/>chunk, extract, consolidate]
    A -->|recall| Re[read path<br/>fused retrieval]
    W --> P[(Postgres<br/>knowledge graph + row level security)]
    Re --> P
    P --> Re --> A
```

Writing turns text into a typed entity and fact graph, one content row shared by meaning plus
one scoped, bi-temporal claim per owner. Reading fuses five retrieval lanes behind one Postgres
round trip, filtered to exactly what the caller's own scopes make visible before a row is ever
considered. The full breakdown of both, with a diagram for each stage, lives in
[Engine](https://phvv.me/aizk/engine/).

Self-describing Markdown may declare any live ontology kind with `- Type <kind>` and any typed
relation with `- <predicate> [<object kind>] <object name>`. Projects and areas use this generic
ontology path rather than dedicated metadata fields.

Generic source tags use `#<kind>: <entity name>`. A same-name tag declares the heading as that live
ontology kind, while other tags connect the note to typed entities through `related_to`. For
example, supporting AIZK notes can use `#project: AIZK Productization` and `#area: Business` without
adding Project or Area to application enums.
