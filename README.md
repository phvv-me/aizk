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

Memory your agents can keep, question, and share. AIZK is a self-hosted AI Zettelkasten for
people, teams, and MCP agents.

> **Warning** AIZK is early `0.0.x` software. Its interfaces and schema may still change.

## What it does

AIZK turns authored notes, public web sources, and uploaded files into sourced evidence that an
agent can retrieve later. It preserves the original source beside a temporal entity and fact graph,
then returns the evidence a caller is allowed to read instead of hiding provenance inside a summary.

- **One database owns queryable state.** PostgreSQL powers the complete self-hosted stack.
  CockroachDB provides the serverless cloud path with Distributed Vector Indexing.
- **Row level security is the boundary.** Private notes and shared organizations are separated by
  policies in the database rather than filters in request handlers.
- **Sources remain authoritative.** Derived facts, themes, profiles, and summaries can be rebuilt
  from the material that produced them.
- **Files keep their original bytes.** Uploads are scanned, converted, and recalled as text until a
  caller explicitly asks for the stored artifact.
- **Modern MCP is the public agent interface.** Clients negotiate MCP `2026-07-28` and receive the
  five tools `status`, `find`, `keep`, `report`, and `share`.

## Run the local stack

The full deployment needs Docker with the NVIDIA container runtime. Copy the committed environment
template, give each required secret its own value, and start the stack.

```sh
cp src/deploy/.env.example .env
docker compose --env-file .env -f src/deploy/docker-compose.yml up -d
```

The [first start guide](docs/src/content/docs/docs/dev/run/first-start.mdx) explains every required
value, service, and health check. The smaller
[development setup](docs/src/content/docs/docs/dev/contributing/setup.md) runs the test suite with
only PostgreSQL and in-process model doubles.

## Call the MCP server

The private local endpoint is `http://localhost:8080/mcp`. This example uses the same FastMCP client
and protocol version exercised by the test suite.

```python
import asyncio

from fastmcp import Client


async def main() -> None:
    client = Client("http://localhost:8080/mcp", mode="2026-07-28")
    async with client:
        await client.call_tool("keep", {"text": "# Deployment\n\nAIZK runs locally."})
        result = await client.call_tool("find", {"query": "Where does AIZK run?", "web": "off"})
        print(result.data)


asyncio.run(main())
```

The [quickstart](docs/src/content/docs/docs/user/quickstart.md) covers authenticated clients and the
[tool reference](docs/src/content/docs/docs/user/reference/tools.mdx) documents every argument and
privacy receipt.

## Deployment paths

- [`src/deploy`](src/deploy) contains the complete PostgreSQL, model, artifact, identity, browser,
  and observability stack.
- [`src/deploy/cockroachdb`](src/deploy/cockroachdb) contains the isolated CockroachDB and Lambda
  emulator profile.
- [`infra/aws`](infra/aws) contains the cost-bounded AWS CDK deployment.
- [`docs`](docs) contains the complete user, operator, architecture, evaluation, and contribution
  documentation and can be built locally.

## Development

`chefe` owns the environment and every verification task.

```sh
uv tool install "chefe>=0.0.25"
chefe install
chefe run lint
chefe run lint-imports
chefe run typecheck
chefe run test
chefe run infra-check
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing the project. Security reports follow
[SECURITY.md](SECURITY.md). AIZK is licensed under [Apache 2.0](LICENSE).
