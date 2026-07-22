<p align="center">
  <a href="https://aizk.phvv.me"><img src="https://raw.githubusercontent.com/phvv-me/aizk/main/docs/src/assets/banner.png" alt="aizk" width="100%"></a>
</p>

<p align="center">
  <a href="https://pypi.org/project/aizk/"><img src="https://img.shields.io/pypi/v/aizk?color=4F46E5&label=pypi" alt="PyPI version"></a>
  <a href="https://pypi.org/project/aizk/"><img src="https://img.shields.io/pypi/pyversions/aizk?color=4F46E5" alt="Python versions"></a>
  <a href="https://github.com/phvv-me/aizk/actions/workflows/ci.yml"><img src="https://github.com/phvv-me/aizk/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/phvv-me/aizk/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-4F46E5.svg" alt="License"></a>
  <a href="https://aizk.phvv.me/docs"><img src="https://img.shields.io/badge/docs-aizk.phvv.me-4F46E5" alt="Docs"></a>
</p>

Memory your agents actually keep. aizk is a self-hosted AI Zettelkasten for people, teams, and MCP agents.

## How it works

You tell aizk something and it remembers it for good. Text, files, and public HTTPS sources become an entity-and-fact knowledge graph addressed by meaning, so the same thing learned twice never duplicates. When you ask a question, aizk ranks the evidence you are allowed to see and answers with its sources.

- **One SQL database owns everything** that matters, including the graph, metadata, temporal state, and job queue. PostgreSQL provides the full local stack. CockroachDB provides the portable cloud path with its native vector index and durable queue.
- **Row level security is the boundary.** Private notes, shared projects, and overlapping groups are separated at the database, not in application code, so memory never crosses where it should not.
- **Files stay immutable.** Original bytes live in private S3-compatible storage, scanned and converted, and recall stays text-first until you ask for the exact original.
- **It speaks MCP.** Claude or any MCP client calls `recall`, `remember`, and `share` directly. A web dashboard over the same service shows what each account can see.

## Quickstart

One command brings up PostgreSQL, object storage, malware scanning, document conversion, the model lanes, and the hardened aizk image as a migration service, an MCP server, a background worker, and the dashboard.

```sh
docker compose --env-file .env -f src/deploy/docker-compose.yml up -d
```

Then call it from any MCP client.

```python
from fastmcp import Client

async with Client("http://localhost:8080/mcp") as client:
    await client.call_tool("remember", {"text": "aizk runs entirely on local hardware."})
    result = await client.call_tool("recall", {"query": "where does aizk run?"})
    print(result.data)
```

Full explanation, deployment, and the engine internals at [aizk.phvv.me/docs](https://aizk.phvv.me/docs).

The isolated CockroachDB profile uses OpenRouter for Qwen3 embeddings and DeepSeek extraction without changing the existing local deployment. Its setup and AWS OpenTofu path live in [`src/deploy/cockroachdb`](src/deploy/cockroachdb) and [`infra/aws`](infra/aws).
