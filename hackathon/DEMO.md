# Judge demo and local rehearsal

The demonstration tells one complete story. An agent keeps public project knowledge, background
work turns it into a temporal graph, a later question retrieves grounded evidence, and the operator
shows that CockroachDB and AWS own the durable path.

## Public corpus

The current reproducible corpus is the public AIZK documentation under
`docs/src/content/docs`. It contains architecture, security, retrieval, evaluation, and operator
material with enough cross-document relationships to exercise vector and graph recall. The final
cloud corpus may add public papers and projects, but every item must record its public source.

## Local Lambda rehearsal

Copy the isolated profile and add only the dedicated public demonstration OpenRouter key.

```sh
cp src/deploy/cockroachdb/.env.example .env.cockroachdb
```

Start CockroachDB and the same Lambda image used by AWS.

```sh
docker compose \
  --profile lambda \
  --env-file .env.cockroachdb \
  -f src/deploy/cockroachdb/docker-compose.yml \
  up -d --build db db-init setup lambda-mcp lambda-worker lambda-setup
```

Load the bounded public corpus through locally emulated Lambda events and modern MCP.

```sh
chefe run python hackathon/workload.py load \
  --root docs/src/content/docs \
  --limit 87 \
  --concurrency 1
```

One Lambda runtime emulator accepts one active invocation, so concurrency remains one. AWS
horizontal concurrency uses separate execution environments rather than concurrent requests to one
emulator.

Run the five-question latency and evidence probe twice. The first pass records cold behavior and
the immediate second pass records the warm path.

```sh
chefe run python hackathon/workload.py benchmark --repeats 1
chefe run python hackathon/workload.py benchmark --repeats 1
```

Inspect durable counts and recent failures.

```sh
docker exec aizk-cockroachdb-db-1 cockroach sql \
  --insecure \
  --database craizk_staging \
  --execute "SELECT count(*) FROM document; SELECT count(*) FROM chunk;"

docker compose \
  --profile lambda \
  --env-file .env.cockroachdb \
  -f src/deploy/cockroachdb/docker-compose.yml \
  logs --since 15m lambda-mcp lambda-worker
```

The broad dated baseline is in
[`results/local-docker-2026-08-10.json`](results/local-docker-2026-08-10.json). The committed
workload's latest smoke result is in
[`results/local-docker-2026-08-11.json`](results/local-docker-2026-08-11.json).

## Cloud judge flow

The public flow requires no repository checkout. The site, docs, browser UI, API, and MCP
share this deployed origin.

```text
https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws
```

Ask the agent to set up AIZK from that URL. The public setup page tells the agent to guide the user
through Logto sign-in or account creation and never request a password or verification code. The
nonprivate Maya demonstration account is supplied separately through the secure demo directory and
is never stored in the repository.

Use this expected source for the bounded live write.

```text
# Project Atlas release policy

Project Atlas deploys one immutable artifact to staging and production. Its release gate checks p95
latency and rollback rate before promotion.
```

Use this differently worded question for the grounded read.

```text
What should Atlas verify before promoting a release? Search only memory and keep web off.
```

1. Open the judge-visible client with the supplied nonprivate account.
2. Call `status` and show the caller, empty failure counts, and active limits.
3. Call `keep` with one short public project note.
4. Invoke or wait for the Lambda worker and show the durable queue drain.
5. Ask a differently worded question with `find` and `web` set to `off`.
6. Open the cited source and compare it with the returned excerpt.
7. Show the C-SPANN query plan and matching scoped vector rows in CockroachDB.
8. Show the Lambda invocation, duration, and absence of errors in CloudWatch.
9. Use ccloud for the required cluster inspection. Add Managed MCP only if its final read-only
   authorization is available.

## Codex Luna clean-room rehearsal

The judge client rehearsal passed on August 12, 2026 with Codex `0.147.0`,
`gpt-5.6-luna`, and MCP `2026-07-28`. The modern Codex protocol path is still an explicit
`mcp_2026_07_28` feature in this release.

On August 13, crAIZK replaced its embedded OAuth proxy with direct Logto verification. The public
Native client has no secret. Codex now discovers Logto through the raw Function URL, generates the
stable server-specific callback, and reaches an accepted PKCE authorization request. The current
configuration is in [`codex/config.oauth.toml`](codex/config.oauth.toml). A final interactive browser
sign-in followed by `status` remains the clean judge rehearsal gate.

The container ran as the unprivileged `node` user with a read-only root filesystem, all Linux
capabilities dropped, `no-new-privileges`, a 256 process limit, 1 GiB of memory, and two CPUs. It
mounted a disposable read-only workspace, disposable Codex state, and only the existing Codex
authentication file as a read-only credential. It could not see the host home, AIZK source tree,
Docker socket, AWS credentials, or a writable host workspace.

Codex called `status` as Maya Chen, kept one private note, and recalled it with a differently worded
`find` using `web="off"` and `fresh=false`. The resulting document was
`019ff5bf-c172-756e-9a76-08ea9c2c8b1d`, and it appeared as the first evidence item. Complete agent
turns took 16.98 seconds for `status`, 18.46 seconds for `keep`, and 37.33 seconds for `find`.

The dated August 12 result used an external PKCE bootstrap and a short-lived bearer token because it
predates the direct public client. Keep that result as historical client evidence, not as the current
setup instruction. The current path uses the pre-registered Native client, explicit scopes, and the
exact Codex callback without a client secret or HTTPS edge.

The complete command and machine-readable evidence are in
[`codex/README.md`](codex/README.md) and
[`results/codex-luna-clean-room-2026-08-12.json`](results/codex-luna-clean-room-2026-08-12.json).

## Secondary OpenCode compatibility evidence

An earlier external-client rehearsal also passed on August 12, 2026. It used the official OpenCode `1.18.8`
container by immutable multi-platform image digest.

```text
ghcr.io/anomalyco/opencode@sha256:bc4de2a82a5663c9bbc2f3be7cab2a5d7dd34f7af73b59b146aa34c054bf0525
```

The container ran as the host's unprivileged user with a read-only root filesystem, all Linux
capabilities dropped, `no-new-privileges`, a 256 process limit, 1 GiB of memory, and two CPUs. It
mounted only a disposable judge workspace and disposable OpenCode state. It could not see the host
home, AIZK source tree, Docker socket, or AWS credentials. OAuth temporarily used host networking so
the browser could return to OpenCode's fixed loopback callback. Every MCP call afterward used the
ordinary isolated Docker bridge.

The run completed DCR and OAuth as Maya Chen, connected over MCP `2026-07-28`, called `status`, kept
one private note, and recalled it with a differently worded `find` using `web="off"` and
`fresh=false`. The resulting document was `019ff54f-8b2d-72ab-8a34-0b7a57e71ce7`. The MCP portion
of `status` took 2.86 seconds, `keep` took 4.82 seconds, and `find` took 7.26 seconds. The three
DeepSeek V4 Flash conversations cost about $0.0048 through OpenRouter.

Current OpenCode stable is not suitable for this modern-only demo. OpenCode `1.18.8` shipped its MCP
v2 client, then [OpenCode reverted it in `1.18.9`](https://github.com/anomalyco/opencode/pull/39373)
while it resolved compatibility regressions with legacy servers. The August 13 retest used OpenCode
`1.18.18` and the new public Logto client. OAuth completed as Maya Chen and stored a valid token, but
the MCP connection timed out because the client still sent the legacy protocol shape. The pinned
`1.18.8` image then connected with that same direct Logto token and called `status` successfully.
Keep this digest pinned until a later OpenCode release restores MCP `2026-07-28` support. Do not
weaken crAIZK by re-enabling an obsolete protocol for one client.

The machine-readable result is in
[`results/opencode-clean-room-2026-08-12.json`](results/opencode-clean-room-2026-08-12.json). The
direct Logto retest is in
[`results/opencode-direct-logto-2026-08-13.json`](results/opencode-direct-logto-2026-08-13.json).
