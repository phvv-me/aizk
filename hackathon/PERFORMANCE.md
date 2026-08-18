# Cloud find performance

This record separates Lambda startup, query embedding, CockroachDB execution, and response work.
The staging corpus is still small, so these measurements guide the demo configuration rather than
claiming a production service level.

The August 12 measurements below retain the exact image digest that produced them. The current
August 13 deployment uses direct Logto verification and immutable image
`sha256:1f99af4ef0eb88d59550c72626fa58e28a01fa94dfd1a4cf0a5ac3d406cde04b`. No later performance
claim is inferred from the deployment-only update.

## Reference point

Five calls against the full self-hosted profile measured about 3.4 seconds of server time per call. Observed connector time
ranged from 3.5 to 3.9 seconds.

The first AIZK configuration never became warm because its 25 second timeout killed each new
environment during cold initialization. A 60 second timeout removed that failure loop.

## Impact map

| Operation | Tiny smoke corpus | SWE reference corpus | Decision |
| --- | --- | --- | --- |
| Query embedding | Commonly 0.29 to 0.65 seconds | 0.32 second warm median | Pin DeepInfra without fallback |
| CockroachDB find | 0.65 to 0.81 seconds | 1.55 second pooled warm median | Keep quality-safe lanes and optimize the composed statement next |
| Access recording | Disabled | Disabled | Keep enabled by default and disable only for AIZK |
| Reranking | Disabled | Disabled | Keep the full profile behavior and omit it in AIZK |
| Packing | Below 1 millisecond | Below 1 millisecond | Keep it |
| Cold initialization | About 25 to 28 seconds | 32.68 second post-deployment find | Use 2048 MB and a five-minute warm event |

Graph expansion and derived lanes contributed no selected evidence on the original one-document
smoke corpus. The six-document SWE workload changed that conclusion. Communities, source passages,
and graph facts all contribute useful evidence, so the final quality-safe profile retains
communities, the entity catalog, and graph expansion. Profiles and RAPTOR remain disabled.

Access recording does not change the current answer. Disabling it removes the recency and frequency
feedback that improves future ranking, so the full profile keeps it enabled.

## Provider comparison

Each provider used the same Qwen3 Embedding 8B model and the same stored vectors. This avoided a
database re-embedding during the comparison.

| Provider | Ten-call median | Maximum | Result |
| --- | --- | --- | --- |
| DeepInfra | 3.045 seconds | 8.42 seconds | Selected |
| Nebius | 4.05 seconds | 19.04 seconds | Rejected |
| SiliconFlow | 3.735 seconds | 7.00 seconds | Rejected |

The remaining tail risk comes from the external embedding request rather than CockroachDB.

## Initial live result

The final five-call sample measured 2.66, 2.69, 2.76, 3.11, and 4.91 seconds through the AWS CLI
wrapper. The median was 2.76 seconds. Internal steady find commonly measured 1.07 to 1.43 seconds.
The modern MCP response still returned the same three facts with their source document handle.

That pass used image digest
`7f598d178d8428ae69fe45a580b3ea9d2a3b9e6a356e6d441b2c8382f578c447`.

## Settings

All find settings default to true. This preserves the full profile behavior.

- `AIZK_FIND_GRAPH_EXPANSION_ENABLED`
- `AIZK_FIND_COMMUNITIES_ENABLED`
- `AIZK_FIND_ENTITY_CATALOG_ENABLED`
- `AIZK_FIND_PROFILES_ENABLED`
- `AIZK_FIND_RAPTOR_ENABLED`
- `AIZK_FIND_SOURCES_FIRST`
- `AIZK_FIND_ACCESS_RECORDING_ENABLED`

The AWS CDK inputs use the matching `AIZK_AWS_FIND_*` names. The quality-safe AIZK profile keeps
communities, the entity catalog, and graph expansion. It disables profiles, RAPTOR, and access
recording. Because AIZK has no cross-encoder reranker, it also serves dense sources before graph
facts. The full self-hosted profile keeps facts first and every default enabled.

## SWE reference workload

The August 12 local Lambda simulation used a fresh `craizk_swe_eval` database with six public
software engineering reference notes derived only from public sources. Native declarations connected the
notes into 37 facts, 15 entities, and three communities. The eight local, global, and multihop
questions covered 26 of 29 expected answer phrases. Warm Lambda find median was 867 ms. Internal
database median was 462 ms, with warm samples from 444 to 536 ms.

The first facts-first run buried correct source passages behind broadly related graph facts. The
source-first fallback recovered answer coverage without a paid reranker while retaining community
and fact evidence after the dense source candidates. The compact evidence is committed in
[`results/craizk-swe-local-2026-08-12.json`](results/craizk-swe-local-2026-08-12.json).

## SWE cloud result

The same six notes were then written through the deployed IAM-protected MCP Lambda. The worker
produced 40 facts, 19 entities, and four communities in CockroachDB Cloud. After every extraction
and maintenance wave drained, the durable queue held zero pending, running, or failed jobs.

The initial sixteen cloud finds repeated the eight-query set twice. The overall median was 2.83
seconds and the warm-only median was 2.80 seconds. Warm p95 was 3.28 seconds. Internal warm medians
separated into 0.32 seconds for embedding and 2.08 seconds for the CockroachDB statement.

That profile exposed a fresh TLS connection on every find. Changing the AIZK setting from a null
pool to one reusable connection per warm Lambda environment reduced the warm end-to-end median to
2.14 seconds and the database median to 1.55 seconds. Warm p95 remained 3.16 seconds. The exact same
query set retained the same answer coverage. `AIZK_AWS_DB_NULL_POOL` keeps the old behavior available
as a diagnostic fallback.

The first call immediately after the environment update took 32.68 seconds. Its internal find was
8.05 seconds, leaving about 24 seconds in process and application initialization. The five-minute
warm schedule normally pays that startup before a user request, but the result remains recorded as
the honest cold maximum.

The exact phrase check again matched 26 of 29 expectations. Manual inspection found that all three
misses were grammatical variants in passages that answered the questions. The evidence said that
the code or rule can be improved, every target runs the same artifact, and external dependencies
can be replaced. This supports 29 of 29 semantic coverage without changing the recorded lexical
score after observing the result.

CloudWatch reported no Lambda errors or throttles during the run. The MCP used at most 500 MB of its
2048 MB allocation, while the worker used at most 540 MB. The larger allocation remains justified
by cold-start CPU rather than memory pressure. The live image digest is
`c9d3a7df72e01ed4df144126149df6cd72aa784b9259bd5543a732ca46b880af`.
The structured evidence is committed in
[`results/craizk-swe-cloud-2026-08-12.json`](results/craizk-swe-cloud-2026-08-12.json).

The live scoped C-SPANN probe planned in 2 ms and executed in 7 ms for eight rows at an estimated
3.50 request units. Its plan selected `ix_scoped_vector_embedding` with exact kind and scope prefix
spans. The vector index is therefore working and is not the 1.55 second composed database limit.
The redacted plan is committed in
[`results/craizk-cspann-cloud-2026-08-12.txt`](results/craizk-cspann-cloud-2026-08-12.txt).
