<p align="center">
  <a href="https://github.com/phvv-me/aizk"><img src="docs/public/brain-box.webp" alt="The AIZK memory cube" width="360"></a>
</p>

<h1 align="center">aizk</h1>

<p align="center">The open AI Zettelkasten for people, teams, and agents</p>

<p align="center">
  <a href="https://pypi.org/project/aizk/"><img src="https://img.shields.io/pypi/v/aizk?color=315DFF&label=pypi" alt="PyPI version"></a>
  <a href="https://pypi.org/project/aizk/"><img src="https://img.shields.io/pypi/pyversions/aizk?color=315DFF" alt="Python versions"></a>
  <a href="https://github.com/phvv-me/aizk/actions/workflows/ci.yml"><img src="https://github.com/phvv-me/aizk/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/phvv-me/aizk/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-315DFF.svg" alt="License"></a>
  <a href="docs/src/content/docs/docs/index.mdx"><img src="https://img.shields.io/badge/docs-included-315DFF" alt="Docs"></a>
</p>

> **Warning** AIZK is early `0.0.x` software. Its interfaces and schema may still change.

## What it is

AIZK is a self-hosted AI Zettelkasten for people, teams, and MCP agents. It turns notes, public web
sources, and uploaded files into scoped evidence that agents can find later.

- Sources and provenance stay attached to every finding.
- Row level security separates private and shared memory.
- Temporal facts preserve what changed and when.
- PostgreSQL and CockroachDB support local and serverless deployments.
- Modern MCP exposes `status`, `find`, `keep`, `report`, and `share`.

## Connect an agent

Claude Code

```sh
claude plugin marketplace add phvv-me/aizk && claude plugin install aizk@aizk
claude
```

Approve AIZK, open `/mcp` and choose sign in.

Codex

```sh
codex plugin marketplace add phvv-me/aizk && codex plugin add aizk@aizk
codex -c mcp_oauth_callback_port=8912 mcp login aizk
```

Complete browser sign in, then ask the agent to call `status`. The plugin includes the AIZK skill
and MCP connection. It does not remove an existing server or replace project instructions.

## Start locally

The complete stack needs Docker and an NVIDIA container runtime.

```sh
cp src/deploy/.env.example .env
docker compose --env-file .env -f src/deploy/docker-compose.yml up -d
```

Continue with the [first start guide](docs/src/content/docs/docs/dev/run/first-start.mdx) or the
[agent quickstart](docs/src/content/docs/docs/user/quickstart.mdx).

## Development

`chefe` owns the environment and verification tasks.

```sh
uv tool install "chefe>=0.0.25"
chefe install
chefe run lint
chefe run test
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing the project. Security reports follow
[SECURITY.md](SECURITY.md). AIZK is licensed under [Apache 2.0](LICENSE).
