import asyncio
import json
import statistics
import time
from itertools import count
from pathlib import Path
from typing import cast

import fire
import httpx
from loguru import logger

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


class LambdaMCP:
    """Call modern AIZK MCP through the local Lambda runtime emulator."""

    protocol_version = "2026-07-28"

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.ids = count(1)

    def metadata(self) -> dict[str, Json]:
        """Build the client metadata repeated on every modern MCP request."""
        return {
            "io.modelcontextprotocol/protocolVersion": self.protocol_version,
            "io.modelcontextprotocol/clientInfo": {
                "name": "aizk-docker-workload",
                "version": "1",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        }

    def event(
        self,
        method: str,
        params: dict[str, Json],
        request_id: int,
        name: str | None = None,
    ) -> dict[str, Json]:
        """Wrap one JSON-RPC request in the API Gateway event Lambda receives."""
        headers: dict[str, Json] = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "host": "lambda.test",
            "mcp-method": method,
            "mcp-protocol-version": self.protocol_version,
        }
        if name is not None:
            headers["mcp-name"] = name
        return {
            "version": "2.0",
            "routeKey": "$default",
            "rawPath": "/mcp",
            "rawQueryString": "",
            "headers": headers,
            "requestContext": {
                "accountId": "local",
                "apiId": "local",
                "domainName": "lambda.test",
                "domainPrefix": "local",
                "http": {
                    "method": "POST",
                    "path": "/mcp",
                    "protocol": "HTTP/1.1",
                    "sourceIp": "127.0.0.1",
                    "userAgent": "aizk-docker-workload",
                },
                "requestId": f"workload-{request_id}",
                "routeKey": "$default",
                "stage": "$default",
                "time": "10/Aug/2026:00:00:00 +0000",
                "timeEpoch": 0,
            },
            "body": json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            ),
            "isBase64Encoded": False,
        }

    async def invoke(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: dict[str, Json],
        name: str | None = None,
    ) -> tuple[dict[str, Json], float]:
        """Invoke Lambda and return its JSON-RPC result with wall latency."""
        request_id = next(self.ids)
        started = time.perf_counter()
        response = await client.post(
            self.endpoint,
            json=self.event(method, params, request_id, name),
        )
        latency_ms = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        envelope = cast(dict[str, Json], response.json())
        body = envelope.get("body")
        if not isinstance(body, str):
            raise RuntimeError(f"Lambda returned no string body for {method}")
        message = cast(dict[str, Json], json.loads(body))
        if error := message.get("error"):
            raise RuntimeError(f"MCP {method} failed with {error}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"MCP {method} returned no result")
        return result, latency_ms

    async def call_tool(
        self,
        client: httpx.AsyncClient,
        name: str,
        arguments: dict[str, Json],
    ) -> tuple[dict[str, Json], float]:
        """Call one modern MCP tool through the Lambda adapter."""
        return await self.invoke(
            client,
            "tools/call",
            {"name": name, "arguments": arguments, "_meta": self.metadata()},
            name,
        )


class Workload:
    """Load public AIZK documentation and measure the Lambda MCP surface."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:9090/2015-03-31/functions/function/invocations",
    ) -> None:
        self.mcp = LambdaMCP(endpoint)

    @staticmethod
    def _percentile(samples: list[float], percentile: float) -> float:
        """Linearly interpolate one percentile from sorted wall-time samples."""
        ordered = sorted(samples)
        position = (len(ordered) - 1) * percentile / 100
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    @staticmethod
    def _structured(result: dict[str, Json]) -> dict[str, Json]:
        """Unwrap FastMCP union results while leaving ordinary models unchanged."""
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise RuntimeError(f"tool returned no structured content with result {result}")
        wrapped = structured.get("result")
        return wrapped if isinstance(wrapped, dict) else structured

    async def _load(self, root: Path, limit: int, concurrency: int) -> dict[str, Json]:
        """Keep a bounded public documentation corpus through Lambda MCP."""
        paths = sorted(
            path for path in root.rglob("*") if path.is_file() and path.suffix in {".md", ".mdx"}
        )[:limit]
        semaphore = asyncio.Semaphore(concurrency)
        latencies: list[float] = []

        async with httpx.AsyncClient(timeout=180) as client:

            async def keep(path: Path) -> str:
                async with semaphore:
                    relative = path.resolve().relative_to(Path.cwd())
                    source = path.read_text(encoding="utf-8", errors="replace")
                    result, latency = await self.mcp.call_tool(
                        client,
                        "keep",
                        {
                            "text": source,
                            "source_uri": (
                                f"https://github.com/phvv-me/aizk/blob/main/{relative.as_posix()}"
                            ),
                        },
                    )
                    latencies.append(latency)
                    structured = self._structured(result)
                    document_id = structured.get("id")
                    if not isinstance(document_id, str):
                        raise RuntimeError(
                            f"keep returned no document id for {relative} with result {result}"
                        )
                    logger.info("kept {} in {:.1f} ms as {}", relative, latency, document_id)
                    return document_id

            started = time.perf_counter()
            documents = await asyncio.gather(*(keep(path) for path in paths))
        elapsed = time.perf_counter() - started
        return {
            "documents": len(documents),
            "source_bytes": sum(path.stat().st_size for path in paths),
            "elapsed_seconds": round(elapsed, 3),
            "documents_per_second": round(len(documents) / elapsed, 3),
            "lambda_keep_p50_ms": round(statistics.median(latencies), 3),
            "lambda_keep_p95_ms": round(self._percentile(latencies, 95), 3),
            "lambda_keep_p99_ms": round(self._percentile(latencies, 99), 3),
        }

    def load(
        self,
        root: str = "docs/src/content/docs",
        limit: int = 87,
        concurrency: int = 1,
    ) -> str:
        """Load the selected public corpus and return a compact JSON report."""
        report = asyncio.run(self._load(Path(root), limit, concurrency))
        return json.dumps(report, sort_keys=True)

    async def _benchmark(self, repeats: int) -> dict[str, Json]:
        """Measure status and grounded find over production-shaped questions."""
        queries = (
            "How does AIZK keep private and shared memory separate?",
            "Why does AIZK preserve original source evidence?",
            "How does AIZK build and retrieve temporal knowledge?",
            "What is the AIZK architecture for background jobs?",
            "How is retrieval quality evaluated in AIZK?",
        )
        status_samples: list[float] = []
        find_samples: list[float] = []
        evidence_counts: list[int] = []
        async with httpx.AsyncClient(timeout=180) as client:
            for _ in range(repeats):
                _, latency = await self.mcp.call_tool(client, "status", {"days": 1})
                status_samples.append(latency)
            for query in queries:
                for _ in range(repeats):
                    result, latency = await self.mcp.call_tool(
                        client,
                        "find",
                        {"query": query, "web": "off"},
                    )
                    find_samples.append(latency)
                    structured = self._structured(result)
                    rendered = structured.get("result")
                    evidence_counts.append(
                        rendered.count("Document `") if isinstance(rendered, str) else 0
                    )
        return {
            "status_samples": len(status_samples),
            "status_p50_ms": round(statistics.median(status_samples), 3),
            "status_p95_ms": round(self._percentile(status_samples, 95), 3),
            "find_samples": len(find_samples),
            "find_p50_ms": round(statistics.median(find_samples), 3),
            "find_p95_ms": round(self._percentile(find_samples, 95), 3),
            "find_p99_ms": round(self._percentile(find_samples, 99), 3),
            "evidence_min": min(evidence_counts),
            "evidence_max": max(evidence_counts),
        }

    def benchmark(self, repeats: int = 3) -> str:
        """Run the end-to-end modern Lambda MCP latency sample."""
        return json.dumps(asyncio.run(self._benchmark(repeats)), sort_keys=True)

    async def _evaluate(
        self, query_file: Path, repeats: int, include_answers: bool
    ) -> dict[str, Json]:
        """Run a labeled find set and retain answers beside simple expectation coverage."""
        loaded = json.loads(query_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError("query file must contain a JSON list")
        reports: list[Json] = []
        all_latencies: list[float] = []
        async with httpx.AsyncClient(timeout=180) as client:
            for entry in loaded:
                if not isinstance(entry, dict):
                    raise ValueError("every query entry must be a JSON object")
                kind = entry.get("kind")
                query = entry.get("query")
                expected = entry.get("expected")
                if not isinstance(kind, str) or not isinstance(query, str):
                    raise ValueError("every query needs string kind and query fields")
                if not isinstance(expected, list) or not all(
                    isinstance(value, str) for value in expected
                ):
                    raise ValueError("every query needs a string expected list")
                answer = ""
                latencies: list[float] = []
                for _ in range(repeats):
                    result, latency = await self.mcp.call_tool(
                        client,
                        "find",
                        {"query": query, "web": "off"},
                    )
                    latencies.append(latency)
                    structured = self._structured(result)
                    rendered = structured.get("result")
                    if not isinstance(rendered, str):
                        raise RuntimeError(f"find returned no text for {query}")
                    answer = rendered
                folded = answer.casefold()
                matched = [value for value in expected if value.casefold() in folded]
                all_latencies.extend(latencies)
                report: dict[str, Json] = {
                    "kind": kind,
                    "query": query,
                    "latency_ms": [round(value, 3) for value in latencies],
                    "expectations_matched": len(matched),
                    "expectations_total": len(expected),
                    "matched": matched,
                    "evidence_documents": answer.count("Document `"),
                }
                if include_answers:
                    report["answer"] = answer
                reports.append(report)
        return {
            "queries": reports,
            "samples": len(all_latencies),
            "find_p50_ms": round(statistics.median(all_latencies), 3),
            "find_p95_ms": round(self._percentile(all_latencies, 95), 3),
        }

    def evaluate(
        self,
        query_file: str = "hackathon/corpus/swe-practices/queries.json",
        repeats: int = 1,
        include_answers: bool = False,
    ) -> str:
        """Evaluate one labeled query set through the modern Lambda MCP surface."""
        return json.dumps(
            asyncio.run(self._evaluate(Path(query_file), repeats, include_answers)),
            ensure_ascii=False,
            sort_keys=True,
        )


if __name__ == "__main__":
    fire.Fire(Workload)
