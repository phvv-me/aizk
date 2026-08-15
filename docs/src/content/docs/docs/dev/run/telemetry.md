---
title: "Telemetry"
description: "Traces, metrics and logs in one Grafana, and how to follow one query end to end."
---

Three signals answer three different questions about the same request. A trace says what the
request did and in what order. Metrics say how often that happens and how fast. Logs say why one
of them failed. All three land in one Grafana, and each links to the other two, which is the
whole point of running them together. Durable product usage is separate and lives in the
`usage_event` ledger described in [Observability](/docs/dev/run/observability/).

```text
  aizk processes ──OTLP──▶ alloy ──▶ tempo ───span metrics───┐
                             │                               │
  container logs ────────────┴────▶ loki                      ▼
                                      │              victoriametrics
                                      └───────▶ grafana ◀─────┘
```

## The stack

`--profile observability` starts Alloy, Loki, Tempo, VictoriaMetrics and Grafana.

Alloy is the one collector. It reads container logs from the Docker socket, receives OTLP spans
from every aizk process, and scrapes metrics. Pointing the runtime at Alloy rather than at Tempo
means a Tempo restart costs a retry in one place instead of dropped spans in every process.

Tempo stores traces and ships no UI of its own, which is exactly why it was chosen. Grafana stays
the single pane. Its metrics generator turns the same spans into RED series and a service graph
and writes them to VictoriaMetrics, so the dashboard needs no hand-rolled counters anywhere in
the application.

VictoriaMetrics is the metrics store, Apache-2.0 and one binary, holding the same series in a
fraction of the memory Prometheus needs on a host whose RAM already belongs to models.

## What the spans already carry

Tracing is on by default in Compose, so `AIZK_OTLP_ENDPOINT` needs no edit. Run without the
observability profile and set it empty, otherwise processes dial a collector that is absent.

The instrumentation in `src/aizk/usage.py` covers SQLAlchemy, httpx and Starlette. Because httpx
is instrumented, every outbound call already becomes a client span naming who answered, which is
how the dashboard separates external providers such as OpenRouter and Firecrawl from Compose
services such as `vllm-emb`, `vllm-rerank`, `gliner` and `docling`. Nothing counts them by hand.

`CallerAnnotator` stamps the caller, the operation and the touched scopes onto every span opened
under a request, not only the first one. That is what makes per-user cost answerable, because a
model call opens its own span long after the transport identified who asked.

pydantic-ai emits GenAI semantic convention spans for every model turn, carrying the model, the
token counts and the latency. Prompts and completions are deliberately excluded, since a chunk of
somebody's private memory is exactly what they would contain.

## Following one query end to end

Open Grafana at `admin.example.com/grafana`, or `admin.example.com/traces` to land straight in the trace
explorer. Both sit behind the same operator sign-in.

1. Start on the **AIZK overview** dashboard and find the spike, whether that is a slow operation,
   a failing peer or a user spending more tokens than expected.
2. Open the trace explorer and select **AIZK traces**. Narrow with TraceQL, for example
   `{span.aizk.operation = "recall"}` for one operation, `{span.aizk.user_id = "..."}` for one
   caller, or `{duration > 3s}` for the slow ones.
3. Open a trace. One request expands into the MCP server span, the retrieval lanes, each SQL
   statement, each outbound call to the embedder and the reranker, and each model turn with its
   token counts. This is the answer to what the query actually did.
4. Press **Logs for this span**. Grafana queries Loki for the same trace id and returns the lines
   that request wrote, which is where a failure explains itself.
5. From any log line the reverse also works. The **View trace** button appears because
   `correlate_trace` puts the trace id into every serialized record and the Loki data source
   lifts it back out.

## Still to add

Container, GPU and Postgres metrics are deliberately not here yet. Host metrics come from Alloy's
built-in exporter, which costs no extra container, and the model lanes publish their own vLLM
metrics. The remaining three each want a decision rather than a default. cAdvisor needs Alloy to
mount `/var/lib/docker`, the Postgres exporter wants a monitoring role rather than the restricted
application credential, and GPU metrics need their own container, where
`utkuozdemir/nvidia_gpu_exporter` is the right choice over dcgm-exporter because dcgm cannot load
its profiling module on GeForce class cards.

## Next

<div class="not-content">

- [Observability](/docs/dev/run/observability/) covers the durable ledger and the stuck queue.
- [The operator console](/docs/dev/run/console/) explains the sign-in in front of Grafana.
- [Deployment topology](/docs/dev/run/topology/) lists every service.

</div>
