import asyncio
import os
from enum import StrEnum, auto
from pathlib import Path
from typing import Protocol

import fire
import httpx
from dotenv import load_dotenv
from patos import FrozenModel, FrozenOpenModel
from pydantic import JsonValue
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.openai import OpenAIChatModelSettings
from sqlalchemy import text

from aizk.serving.base import close_clients, llm_model
from aizk.store.backend import CockroachDBAdapter, DatabaseRole


class StewardSeverity(StrEnum):
    """Operational severity of one queue inspection."""

    healthy = auto()
    degraded = auto()
    unsafe = auto()


class StewardAction(StrEnum):
    """Bounded next action the steward may recommend but never execute itself."""

    no_action = auto()
    retry_conversion = auto()
    retry_graph = auto()
    retry_profile = auto()
    inspect_worker = auto()
    human_review = auto()


class McpCall(FrozenModel):
    """One fixed call the Managed MCP evidence reader may make."""

    name: str
    arguments: dict[str, JsonValue]


class ManagedMcpReaderProtocol(Protocol):
    """Read a fixed batch of Managed MCP calls."""

    async def read(self, calls: tuple[McpCall, ...]) -> tuple[JsonValue, ...]:
        """Execute the requested read-only calls in order."""
        ...


class ManagedMcpReader:
    """Execute only the two read-only Managed MCP tools used by the steward."""

    allowed_tools = frozenset({"get_cluster", "select_query"})

    def __init__(self, toolset: MCPToolset[None]) -> None:
        self.toolset = toolset

    async def read(self, calls: tuple[McpCall, ...]) -> tuple[JsonValue, ...]:
        """Execute an allowlisted batch over one MCP session."""
        forbidden = {call.name for call in calls} - self.allowed_tools
        if forbidden:
            raise ValueError(
                f"Managed MCP calls are not read-only allowlisted: {sorted(forbidden)}"
            )
        results: list[JsonValue] = []
        async with self.toolset as toolset:
            for call in calls:
                results.append(await toolset.direct_call_tool(call.name, call.arguments))
        return tuple(results)


class CockroachJobReading(FrozenModel):
    """Sanitized counts from one direct read-only `SHOW JOBS` statement."""

    failed: int = 0
    paused: int = 0
    long_running: int = 0


class CockroachJobReaderProtocol(Protocol):
    """Read CockroachDB background job health without exposing job contents."""

    async def read(self) -> CockroachJobReading | None:
        """Return sanitized problem counts, or no reading when SQL is unavailable."""
        ...


class CockroachJobReader:
    """Aggregate background job problems through one fixed SQL statement."""

    query = text(
        """
        WITH jobs AS (SHOW JOBS),
        problems AS (
            SELECT CASE
                       WHEN status = 'failed' THEN 'failed'
                       WHEN status IN ('paused', 'pause-requested') THEN 'paused'
                       WHEN status IN ('running', 'reverting')
                            AND created < now() - INTERVAL '2 hours'
                         THEN 'long_running'
                   END AS health
            FROM jobs
            WHERE status = 'failed'
               OR status IN ('paused', 'pause-requested')
               OR (
                    status IN ('running', 'reverting')
                    AND created < now() - INTERVAL '2 hours'
               )
        )
        SELECT health, count(*)::INT8 AS jobs
        FROM problems
        GROUP BY health
        ORDER BY health
        LIMIT 10
        """
    )

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    async def read(self) -> CockroachJobReading | None:
        """Run one bounded aggregate with no IDs, descriptions, SQL, or errors."""
        engine = CockroachDBAdapter().engine(self.database_url, DatabaseRole.owner)
        try:
            async with engine.connect() as connection:
                rows = (await connection.execute(self.query)).mappings().all()
        finally:
            await engine.dispose()
        counts = {str(row["health"]): int(row["jobs"]) for row in rows}
        return CockroachJobReading(
            failed=counts.get("failed", 0),
            paused=counts.get("paused", 0),
            long_running=counts.get("long_running", 0),
        )


class UnavailableCockroachJobReader:
    """Represent a run that has no private CockroachDB SQL credential."""

    async def read(self) -> CockroachJobReading | None:
        """Keep missing visibility explicit instead of turning it into zero."""
        return None


class ClusterReading(FrozenOpenModel):
    """Sanitized cluster fields returned by Managed MCP."""

    state: str
    plan: str | None = None
    cockroach_version: str | None = None


class CloudCluster(FrozenOpenModel):
    """One cluster visible to the scoped service account."""

    id: str


class CloudClusters(FrozenOpenModel):
    """CockroachDB Cloud cluster listing."""

    clusters: tuple[CloudCluster, ...]


class ClusterResolver:
    """Resolve the sole cluster visible to a least-privilege service account."""

    def resolve(self, api_key: str, requested: str | None) -> str:
        """Use an explicit cluster or require exactly one accessible cluster."""
        if requested:
            return requested
        response = httpx.get(
            "https://cockroachlabs.cloud/api/v1/clusters",
            headers={"Authorization": f"Bearer {api_key}", "Cc-Version": "2024-09-16"},
            timeout=20.0,
        )
        response.raise_for_status()
        clusters = CloudClusters.model_validate(response.json()).clusters
        if len(clusters) != 1:
            raise ValueError(
                "cluster_id is required unless the service account can access exactly one cluster"
            )
        return clusters[0].id


class QueueTaskGroup(FrozenOpenModel):
    """Aggregate current queue tasks for one state and entrypoint."""

    status: str
    entrypoint: str
    tasks: int


class ConversionStateGroup(FrozenOpenModel):
    """Aggregate artifact contents in one conversion state."""

    state: str
    contents: int


class FailureEventGroup(FrozenOpenModel):
    """Aggregate recent queue history without payloads or error messages."""

    status: str
    entrypoint: str
    error_type: str | None = None
    events: int


class DoctorSummary(FrozenOpenModel):
    """Bounded queue and conversion counters from the stored doctor reading."""

    current_failed_jobs: int = 0
    failed_conversions: int = 0
    fresh_active_conversions: int = 0
    long_running_picked_jobs: int = 0
    orphaned_active_conversions: int = 0
    queued_active_conversions: int = 0
    recent_exception_events: int = 0
    stale_active_conversions: int = 0
    stale_picked_jobs: int = 0
    unreadable_conversions: int = 0


class DoctorReading(FrozenOpenModel):
    """Sanitized stored doctor reading."""

    healthy: bool
    summary: DoctorSummary


class McpRows(FrozenOpenModel):
    """Rows returned by one Managed MCP select query."""

    rows: tuple[dict[str, JsonValue], ...] = ()


class QueueSnapshot(FrozenModel):
    """Normalized immutable evidence supplied to the model."""

    cluster_state: str
    cluster_plan: str | None
    cockroach_version: str | None
    doctor_available: bool
    doctor_healthy: bool | None
    queue_failure_count: int
    queued_task_count: int
    picked_task_count: int
    failed_entrypoints: tuple[str, ...]
    failed_conversion_count: int
    unreadable_conversion_count: int
    stale_picked_count: int
    long_running_picked_count: int
    recent_exception_count: int
    recent_failure_event_count: int
    cockroach_job_failure_count: int | None = None
    cockroach_job_paused_count: int | None = None
    cockroach_job_long_running_count: int | None = None
    cockroach_job_check_available: bool = False
    mcp_tools_used: tuple[str, ...] = ("get_cluster", "select_query")
    sql_statements_used: tuple[str, ...] = ()

    def policy_severity(self) -> StewardSeverity:
        """Derive the minimum truthful severity from current evidence."""
        if self.cluster_state.upper() not in {"CREATED", "RUNNING"}:
            return StewardSeverity.unsafe
        if (
            not self.doctor_available
            or self.doctor_healthy is not True
            or self.queue_failure_count
            or self.failed_conversion_count
            or self.stale_picked_count
            or self.long_running_picked_count
            or self.cockroach_job_failure_count
            or self.cockroach_job_paused_count
            or self.cockroach_job_long_running_count
        ):
            return StewardSeverity.degraded
        return StewardSeverity.healthy

    def evidence(self) -> tuple[str, ...]:
        """Render only bounded facts that came from the fixed collector."""
        plan = f" on the {self.cluster_plan} plan" if self.cluster_plan else ""
        doctor = (
            f"Stored doctor reading is {'healthy' if self.doctor_healthy else 'unhealthy'}"
            if self.doctor_available
            else "Stored doctor reading is unavailable"
        )
        cockroach_jobs = (
            (
                "CockroachDB background jobs have "
                f"{self.cockroach_job_failure_count} failed, "
                f"{self.cockroach_job_paused_count} paused, and "
                f"{self.cockroach_job_long_running_count} running longer than two hours"
            )
            if self.cockroach_job_check_available
            else "CockroachDB background job health is unavailable"
        )
        return (
            f"CockroachDB cluster state is {self.cluster_state}{plan}",
            (
                f"Current AIZK queue has {self.queue_failure_count} failed, "
                f"{self.picked_task_count} picked, and {self.queued_task_count} queued tasks"
            ),
            (
                f"Artifact conversions have {self.failed_conversion_count} failed and "
                f"{self.unreadable_conversion_count} unreadable contents"
            ),
            (
                f"{doctor}, with {self.stale_picked_count} stale and "
                f"{self.long_running_picked_count} long-running picked tasks"
            ),
            (
                f"Stored doctor history records {self.recent_exception_count} exception events, "
                f"while the fixed two-hour history query returned "
                f"{self.recent_failure_event_count}; neither is a current failure"
            ),
            cockroach_jobs,
        )


class QueueSnapshotCollector:
    """Collect a fixed sanitized queue snapshot without model-selected tool calls."""

    def __init__(
        self,
        reader: ManagedMcpReaderProtocol,
        job_reader: CockroachJobReaderProtocol,
        database: str,
    ) -> None:
        self.reader = reader
        self.job_reader = job_reader
        self.database = database

    async def collect(self) -> QueueSnapshot:
        """Run the complete read-only evidence plan and normalize its results."""
        mcp_result, job_reading = await asyncio.gather(
            self.reader.read(self.calls()),
            self.job_reader.read(),
        )
        cluster, queue, doctor, conversions, history = mcp_result
        cluster_reading = ClusterReading.model_validate(cluster)
        queue_rows = self.rows(queue, QueueTaskGroup)
        doctor_rows = self.rows(doctor, DoctorReading)
        conversion_rows = self.rows(conversions, ConversionStateGroup)
        history_rows = self.rows(history, FailureEventGroup)
        doctor_reading = doctor_rows[0] if doctor_rows else None
        doctor_summary = doctor_reading.summary if doctor_reading else DoctorSummary()
        return QueueSnapshot(
            cluster_state=cluster_reading.state,
            cluster_plan=cluster_reading.plan,
            cockroach_version=cluster_reading.cockroach_version,
            doctor_available=doctor_reading is not None,
            doctor_healthy=doctor_reading.healthy if doctor_reading else None,
            queue_failure_count=sum(row.tasks for row in queue_rows if row.status == "failed"),
            queued_task_count=sum(row.tasks for row in queue_rows if row.status == "queued"),
            picked_task_count=sum(row.tasks for row in queue_rows if row.status == "picked"),
            failed_entrypoints=tuple(
                sorted(row.entrypoint for row in queue_rows if row.status == "failed")
            ),
            failed_conversion_count=sum(
                row.contents for row in conversion_rows if row.state == "failed"
            ),
            unreadable_conversion_count=sum(
                row.contents for row in conversion_rows if row.state == "unreadable"
            ),
            stale_picked_count=doctor_summary.stale_picked_jobs,
            long_running_picked_count=doctor_summary.long_running_picked_jobs,
            recent_exception_count=doctor_summary.recent_exception_events,
            recent_failure_event_count=sum(row.events for row in history_rows),
            cockroach_job_failure_count=job_reading.failed if job_reading else None,
            cockroach_job_paused_count=job_reading.paused if job_reading else None,
            cockroach_job_long_running_count=(job_reading.long_running if job_reading else None),
            cockroach_job_check_available=job_reading is not None,
            sql_statements_used=("SHOW JOBS",) if job_reading else (),
        )

    def calls(self) -> tuple[McpCall, ...]:
        """Build the fixed evidence plan without identifiers, payloads, or error text."""
        return (
            McpCall(name="get_cluster", arguments={}),
            self.query(
                """
                SELECT status, entrypoint, count(*) AS tasks
                FROM public.queue_task
                GROUP BY status, entrypoint
                ORDER BY status, entrypoint
                LIMIT 100
                """
            ),
            self.query(
                """
                SELECT report->>'healthy' AS healthy,
                       report->'summary' AS summary
                FROM public.operator_snapshot
                WHERE key = 'doctor'
                LIMIT 1
                """
            ),
            self.query(
                """
                SELECT state, count(*) AS contents
                FROM public.artifact_content
                GROUP BY state
                ORDER BY state
                LIMIT 100
                """
            ),
            self.query(
                """
                SELECT status, entrypoint, error_type, count(*) AS events
                FROM public.queue_event
                WHERE status IN ('failed', 'exception')
                  AND created_at >= now() - INTERVAL '2 hours'
                GROUP BY status, entrypoint, error_type
                ORDER BY status, entrypoint, error_type
                LIMIT 100
                """
            ),
        )

    def query(self, sql: str) -> McpCall:
        """Create one bounded SELECT call for the configured database."""
        return McpCall(
            name="select_query",
            arguments={"database": self.database, "query": sql},
        )

    @staticmethod
    def rows[T: FrozenOpenModel](payload: JsonValue, model: type[T]) -> tuple[T, ...]:
        """Validate external row dictionaries as one expected evidence model."""
        return tuple(model.model_validate(row) for row in McpRows.model_validate(payload).rows)


class StewardAssessment(FrozenModel):
    """Typed advisory assessment produced from an immutable snapshot."""

    severity: StewardSeverity
    action: StewardAction
    rationale: str
    next_step: str


class DiagnosticianProtocol(Protocol):
    """Classify one normalized queue snapshot."""

    async def assess(self, snapshot: QueueSnapshot) -> StewardAssessment:
        """Return a typed advisory assessment."""
        ...


class DeepSeekDiagnostician:
    """Use DeepSeek once for typed diagnosis without giving it database tools."""

    def __init__(self, openrouter_api_key: str) -> None:
        if not openrouter_api_key:
            raise ValueError("AIZK_DEMO_OPENROUTER_API_KEY is required")
        model = llm_model(
            "https://openrouter.ai/api/v1",
            openrouter_api_key,
            "deepseek/deepseek-v4-flash-0731",
            120.0,
        )
        self.agent = Agent(
            model,
            output_type=StewardAssessment,
            instructions=self.instructions(),
            model_settings=OpenAIChatModelSettings(
                temperature=0.0,
                max_tokens=1000,
                extra_body={
                    "reasoning": {"enabled": False},
                    "session_id": "aizk-queue-steward",
                },
            ),
            retries=2,
        )

    async def assess(self, snapshot: QueueSnapshot) -> StewardAssessment:
        """Classify the supplied evidence without collecting or changing data."""
        result = await self.agent.run(
            "Classify this immutable AIZK queue snapshot. Do not invent missing evidence. "
            "Recent history is not a current failure. Background job health is unknown when its "
            f"check is unavailable.\n\n{snapshot.model_dump_json(indent=2)}"
        )
        return result.output

    @staticmethod
    def instructions() -> str:
        """Load the AIZK workflow and exact CockroachDB operational skills."""
        skill_root = Path(__file__).with_name("skills")
        names = (
            "aizk-repair-queue",
            "reviewing-cluster-health",
            "monitoring-background-jobs",
        )
        guidance = "\n\n".join(
            (skill_root / name / "SKILL.md").read_text(encoding="utf-8") for name in names
        )
        return (
            f"{guidance}\n\n"
            "Runtime boundary\n\n"
            "The program already collected every database fact through fixed read-only Managed "
            "MCP calls and one fixed direct SHOW JOBS aggregate. You have no tools and must not "
            "request more evidence. Classify only the normalized snapshot. Treat CockroachDB "
            "background job health as unknown only when its check is unavailable. Recommend no "
            "action for a healthy current queue even when recent exception history is nonzero. "
            "Never recommend a retry without current failure evidence. The program applies a "
            "deterministic approval policy after your assessment."
        )


class StewardVerdict(FrozenModel):
    """Auditable result of one skill-guided Managed MCP inspection."""

    severity: StewardSeverity
    summary: str
    queue_failure_count: int
    cockroach_job_failure_count: int | None
    evidence: tuple[str, ...]
    mcp_tools_used: tuple[str, ...]
    sql_statements_used: tuple[str, ...]
    skills_used: tuple[str, ...]
    model_assessment: StewardAssessment
    action: StewardAction
    requires_approval: bool
    next_step: str


class RepairPolicy:
    """Turn an advisory model assessment into a bounded effective verdict."""

    skills = (
        "aizk-repair-queue",
        "reviewing-cluster-health",
        "monitoring-background-jobs",
    )

    def apply(self, snapshot: QueueSnapshot, assessment: StewardAssessment) -> StewardVerdict:
        """Enforce evidence-derived severity and human approval for every action."""
        severity = snapshot.policy_severity()
        action = self.action(snapshot, assessment.action)
        return StewardVerdict(
            severity=severity,
            summary=self.summary(snapshot, severity),
            queue_failure_count=snapshot.queue_failure_count,
            cockroach_job_failure_count=snapshot.cockroach_job_failure_count,
            evidence=snapshot.evidence(),
            mcp_tools_used=snapshot.mcp_tools_used,
            sql_statements_used=snapshot.sql_statements_used,
            skills_used=self.skills,
            model_assessment=assessment,
            action=action,
            requires_approval=action is not StewardAction.no_action,
            next_step=self.next_step(snapshot, assessment, action),
        )

    @staticmethod
    def action(snapshot: QueueSnapshot, proposed: StewardAction) -> StewardAction:
        """Allow only actions justified by the evidence available to this run."""
        severity = snapshot.policy_severity()
        if severity is StewardSeverity.healthy:
            return StewardAction.no_action
        if severity is StewardSeverity.unsafe:
            return StewardAction.human_review
        if (
            snapshot.cockroach_job_failure_count
            or snapshot.cockroach_job_paused_count
            or snapshot.cockroach_job_long_running_count
        ):
            return StewardAction.human_review
        if snapshot.stale_picked_count or snapshot.long_running_picked_count:
            if proposed in {StewardAction.inspect_worker, StewardAction.human_review}:
                return proposed
            return StewardAction.inspect_worker
        return StewardAction.human_review

    @staticmethod
    def summary(snapshot: QueueSnapshot, severity: StewardSeverity) -> str:
        """Describe current evidence without allowing generated factual claims."""
        jobs = (
            "CockroachDB background jobs are healthy."
            if snapshot.cockroach_job_check_available
            else "CockroachDB background job status is unavailable."
        )
        if (
            snapshot.cockroach_job_failure_count
            or snapshot.cockroach_job_paused_count
            or snapshot.cockroach_job_long_running_count
        ):
            jobs = "CockroachDB background jobs require operator review."
        if severity is StewardSeverity.healthy:
            return f"The cluster and current AIZK queue are healthy. {jobs}"
        if severity is StewardSeverity.unsafe:
            return f"The cluster state requires operator review. {jobs}"
        return (
            f"The current AIZK queue requires attention with "
            f"{snapshot.queue_failure_count} failed tasks and "
            f"{snapshot.stale_picked_count} stale picked tasks. {jobs}"
        )

    @staticmethod
    def next_step(
        snapshot: QueueSnapshot,
        assessment: StewardAssessment,
        action: StewardAction,
    ) -> str:
        """Explain an override instead of presenting an unsupported model action."""
        if action is StewardAction.no_action:
            return "No repair is needed. Repeat the inspection before the demonstration."
        if action is assessment.action:
            return assessment.next_step
        if action is StewardAction.inspect_worker:
            return "Inspect the worker logs and confirm lease ownership before any retry."
        if snapshot.failed_conversion_count:
            return "Review the sanitized conversion failure identity before approving a retry."
        return "Review the current failure outside the model before approving a repair."


class QueueSteward:
    """Orchestrate deterministic evidence collection and typed diagnosis."""

    def __init__(
        self,
        collector: QueueSnapshotCollector,
        diagnostician: DiagnosticianProtocol,
        policy: RepairPolicy,
    ) -> None:
        self.collector = collector
        self.diagnostician = diagnostician
        self.policy = policy

    async def diagnose(self) -> StewardVerdict:
        """Collect evidence, obtain an advisory assessment, and enforce policy."""
        snapshot = await self.collector.collect()
        assessment = await self.diagnostician.assess(snapshot)
        return self.policy.apply(snapshot, assessment)


class QueueStewardCLI:
    """Run the hackathon queue steward from the repository environment."""

    def diagnose(
        self,
        cluster_id: str | None = None,
        database: str | None = None,
        job_database_url: str | None = None,
        oauth: bool = False,
    ) -> dict[str, JsonValue]:
        """Inspect one cluster with an API key or explicit interactive OAuth."""
        package_root = Path(__file__).parents[2]
        workspace_root = Path(__file__).parents[4]
        load_dotenv(Path(__file__).with_name(".env"))
        load_dotenv(package_root / ".env")
        load_dotenv(workspace_root / ".env")
        openrouter_api_key = os.environ.get("AIZK_DEMO_OPENROUTER_API_KEY", "")
        cockroach_api_key = os.environ.get("CRDB_SERVICE_API_KEY") or os.environ.get(
            "AIZK_COCKROACH_MCP_API_KEY"
        )
        requested_cluster_id = cluster_id or os.environ.get("AIZK_COCKROACH_CLUSTER_ID")
        selected_database = (
            database
            or os.environ.get("AIZK_COCKROACH_DATABASE")
            or os.environ.get("AIZK_DB_NAME")
            or "aizk"
        )
        selected_job_database_url = job_database_url or os.environ.get(
            "AIZK_COCKROACH_JOB_DATABASE_URL"
        )
        if not cockroach_api_key and not oauth:
            raise ValueError(
                "CRDB_SERVICE_API_KEY or AIZK_COCKROACH_MCP_API_KEY is required "
                "outside interactive OAuth"
            )
        if oauth and not requested_cluster_id:
            raise ValueError("cluster_id is required for interactive OAuth")
        selected_cluster_id = (
            ClusterResolver().resolve(cockroach_api_key, requested_cluster_id)
            if cockroach_api_key
            else requested_cluster_id
        )
        if not selected_cluster_id:
            raise ValueError("cluster_id is required")
        headers = {"mcp-cluster-id": selected_cluster_id}
        if cockroach_api_key:
            headers["Authorization"] = f"Bearer {cockroach_api_key}"
        toolset = MCPToolset(
            "https://cockroachlabs.cloud/mcp",
            auth=None if cockroach_api_key else "oauth",
            headers=headers,
            tool_error_behavior="error",
        )
        steward = QueueSteward(
            collector=QueueSnapshotCollector(
                ManagedMcpReader(toolset),
                (
                    CockroachJobReader(selected_job_database_url)
                    if selected_job_database_url
                    else UnavailableCockroachJobReader()
                ),
                selected_database,
            ),
            diagnostician=DeepSeekDiagnostician(openrouter_api_key),
            policy=RepairPolicy(),
        )
        return asyncio.run(self.run(steward)).model_dump(mode="json")

    @staticmethod
    async def run(steward: QueueSteward) -> StewardVerdict:
        """Close shared model clients after either success or failure."""
        try:
            return await steward.diagnose()
        finally:
            await close_clients()


if __name__ == "__main__":
    fire.Fire(QueueStewardCLI)
