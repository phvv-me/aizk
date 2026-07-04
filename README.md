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

A self-hosted multi-tenant memory engine that turns a Zettelkasten into scoped agent-queryable memory over MCP

An AI assistant forgets everything between sessions, and sharing one assistant across a team or
household needs private, project, and overlapping-group knowledge to stay properly separated.
aizk is a single self-hosted Postgres database that gives an assistant real memory over MCP,
with the visibility rules enforced by the database itself. See
[phvv.me/aizk](https://phvv.me/aizk) for what this means in plain language and how it works.

## Install

```sh
pip install aizk
```

## Use

Bring up Postgres and the model containers with `docker compose up`, start the server with
`aizk serve-mcp`, then call its tools from any MCP client.

```python
from fastmcp import Client

async with Client("http://localhost:8000/mcp") as client:
    await client.call_tool("remember", {"text": "aizk runs entirely on local hardware."})
    result = await client.call_tool("recall", {"query": "where does aizk run?"})
    print(result.data)
```

## Configuration

Every docker-compose knob, the `initdb/roles.sh` role password, and every `Settings` default live
in one file, `.env.example`. Copy it to `.env` and edit; docker compose and the app both read the
same `AIZK_`-prefixed variables, so one edit moves both.

## Documentation

Full documentation lives at [https://phvv.me/aizk](https://phvv.me/aizk).

For LLM-assisted use, start with [`llms.txt`](https://phvv.me/aizk/llms.txt).

## Development

The dev environment is managed by [uv](https://docs.astral.sh/uv/).

- Install: `uv sync --extra dev`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Typecheck: `uv run mypy src && uv run pyrefly check`
- Test: `uv run pytest -q`
- Docs: `uv run --extra docs mkdocs build -d site`
- Build: `uv build`
