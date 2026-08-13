#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repository_root"
ccloud_result=/tmp/craizk-full-ccloud.json

heading() {
  printf '\n\033[1;36m%s\033[0m\n\n' "$1"
  sleep 4
}

printf '\033[2J\033[H'
printf '\033[1;36mcrAIZK infrastructure evidence\033[0m\n\n'
printf 'Live cloud state and committed redacted measurements\n'
sleep 6

heading '1  Public AWS endpoint health'
curl -fsS -o /dev/null -w 'HTTP %{http_code}  total %{time_total}s  TLS %{time_appconnect}s\n' \
  https://xpc3f5mjuv5edgnsfsfbffcoda0ckvij.lambda-url.ap-southeast-1.on.aws/
sleep 7

heading '2  Live CockroachDB Cloud cluster through ccloud'
bat --style=plain --paging=never --color=always "$ccloud_result"
sleep 10

heading '3  Scoped C-SPANN plan from the live cloud database'
bat --style=plain --paging=never --color=always \
  hackathon/results/craizk-cspann-cloud-2026-08-12.txt
sleep 12

heading '4  Cloud workload, durable inventory and Lambda health'
jq '{deployment, corpus, profile, ingestion: {documents: .ingestion.documents, elapsed_seconds: .ingestion.elapsed_seconds, queue_final: .ingestion.queue_final}, recall: {queries: .recall.queries, warm_lambda_find_p50_ms: .recall.warm_lambda_find_p50_ms, warm_lambda_find_p95_ms: .recall.warm_lambda_find_p95_ms}, database_inventory, lambda_observability}' \
  hackathon/results/craizk-swe-cloud-2026-08-12.json
sleep 16

printf '\n\033[1;32mInfrastructure walkthrough complete\033[0m\n'
printf 'CockroachDB Cloud holds sources, vectors, graph, scopes, usage and the durable queue.\n'
printf 'AWS Lambda serves MCP and processes background work. S3 preserves bounded original files.\n'
sleep 10
