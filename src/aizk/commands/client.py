from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import App, Parameter
from pydantic import UUID7, AnyHttpUrl, TypeAdapter

from ..artifacts.models import ArtifactReceipt
from ..client import (
    ClientProfile,
    CommandInput,
    LocalUpload,
    MemoryClient,
    ProfileStore,
    RememberBatchResult,
    RememberRequest,
    ResultSerializer,
    ShareRequest,
)
from ..mcp.models import UploadTicketAccepted
from ..memory import WriteResult
from ..status import StageEstimate, StatusReport

JsonOutput = Annotated[bool, Parameter(name="--json")]
_URL = TypeAdapter(AnyHttpUrl)

auth_app = App(name="auth", help="Sign in, inspect credentials, or sign out.")


class ClientCommands:
    """Execute the public MCP client surface over one persisted connection profile."""

    def __init__(self, profiles: ProfileStore | None = None) -> None:
        self.profiles = profiles or ProfileStore()

    def profile(self, server: str | None = None) -> ClientProfile:
        """Resolve an explicit server or the profile selected during login."""
        if server is not None:
            return ClientProfile(server=_URL.validate_python(server))
        return self.profiles.load()

    async def login(
        self,
        server: str | None,
        auth: Literal["oauth", "none"],
        callback_host: str,
        callback_port: int,
        days: int,
        json_output: bool,
    ) -> None:
        """Authenticate interactively and persist the nonsecret server selection."""
        if server is None:
            existing = self.profiles.load()
            server = str(existing.server)
        profile = ClientProfile(
            server=_URL.validate_python(server),
            auth=auth,
            callback_host=callback_host,
            callback_port=callback_port,
        )
        report = await MemoryClient(profile).login(days)
        self.profiles.save(profile)
        print(
            ResultSerializer.json(report)
            if json_output
            else f"signed in as {report.caller.label or report.caller.username or 'AIZK user'}"
        )

    async def logout(self, server: str | None, json_output: bool) -> None:
        """Remove the selected server's OAuth material from the system keyring."""
        profile = self.profile(server)
        await MemoryClient(profile).logout()
        if json_output:
            print(ResultSerializer.json(profile))
        else:
            print(f"signed out from {profile.server}")

    async def authentication_status(
        self,
        server: str | None,
        days: int,
        json_output: bool,
    ) -> None:
        """Check stored credentials without opening an authorization browser."""
        result = await MemoryClient(self.profile(server)).authentication_status(days)
        if json_output:
            print(ResultSerializer.json(result))
        elif result.authenticated and result.status is not None:
            caller = result.status.caller
            print(f"authenticated as {caller.label or caller.username or 'AIZK user'}")
        else:
            print("not authenticated")

    async def recall(
        self,
        query: str | None,
        budget: int | None,
        server: str | None,
        json_output: bool,
    ) -> None:
        """Recall evidence through the public MCP tool."""
        resolved = CommandInput.text(query)
        if not resolved:
            raise ValueError("recall requires a query argument or piped text")
        result = await MemoryClient(self.profile(server)).recall(resolved, budget)
        print(ResultSerializer.json(result) if json_output else result)

    async def remember(
        self,
        paths: tuple[Path, ...],
        text: str | None,
        source_uri: str | None,
        observed_at: datetime | None,
        expires_at: datetime | None,
        scopes: tuple[str, ...],
        preserve_source: bool,
        server: str | None,
        json_output: bool,
    ) -> None:
        """Remember authored text, one public source, or explicit local file paths."""
        companion = CommandInput.text(text)
        result: RememberBatchResult | WriteResult | ArtifactReceipt | UploadTicketAccepted
        if paths:
            if source_uri is not None or observed_at is not None or expires_at is not None:
                raise ValueError("file paths cannot be combined with source or time options")
            if preserve_source:
                raise ValueError("preserve-source applies only to source-uri")
            result = await MemoryClient(self.profile(server)).remember_files(
                [LocalUpload(path=path) for path in paths],
                companion_text=companion,
                scopes=list(scopes) or None,
            )
        else:
            result = await MemoryClient(self.profile(server)).remember(
                RememberRequest(
                    text=companion,
                    source_uri=source_uri,
                    observed_at=observed_at,
                    expires_at=expires_at,
                    scopes=list(scopes) or None,
                    preserve_source=preserve_source,
                )
            )
        print(ResultSerializer.json(result) if json_output else self.render_remember(result))

    async def share(
        self,
        documents: tuple[UUID7, ...],
        scopes: tuple[str, ...],
        server: str | None,
        json_output: bool,
    ) -> None:
        """Share visible documents through the public MCP tool."""
        result = await MemoryClient(self.profile(server)).share(
            ShareRequest(
                documents=list(documents),
                scopes=list(scopes) or None,
            )
        )
        print(
            ResultSerializer.json(result) if json_output else f"shared {result.shared} documents"
        )

    async def status(
        self,
        days: int,
        server: str | None,
        json_output: bool,
    ) -> None:
        """Show identity, durable usage, and current processing health."""
        report = await MemoryClient(self.profile(server)).status(days)
        print(ResultSerializer.json(report) if json_output else self.render_status(report))

    @classmethod
    def render_status(cls, report: StatusReport) -> str:
        """Render the expanded status report for a terminal."""
        caller = report.caller
        usage = report.usage
        lines = [
            f"Account  {caller.label or caller.username or 'Anonymous'}",
            f"Organizations  {', '.join(item.name for item in caller.organizations) or 'None'}",
            "",
            f"Usage over {usage.days} days",
            (
                f"Requests  {usage.summary.requests}  "
                f"Recalls  {usage.summary.recalls}  "
                f"Remembers  {usage.summary.remembers}  "
                f"Files  {usage.summary.files}  "
                f"Shares  {usage.summary.shares}"
            ),
            (
                f"Lifetime requests  {usage.lifetime.requests}  "
                f"Lifetime items  {usage.lifetime.items}"
            ),
            "",
            f"Processing  {report.processing.state}",
        ]
        lines.extend(cls.render_stage(stage) for stage in report.processing.stages)
        return "\n".join(lines)

    @classmethod
    def render_stage(cls, stage: StageEstimate) -> str:
        """Render one workload stage with its measured rate and honest ETA state."""
        running = "not tracked" if stage.running is None else str(stage.running)
        failed = "not tracked" if stage.failed is None else str(stage.failed)
        eta = cls.render_eta(stage)
        return (
            f"{stage.key.replace('_', ' ').title()}  "
            f"{stage.queued} queued  {running} active  {failed} failed  "
            f"{stage.throughput_per_hour:.1f} per hour  {eta}"
        )

    @staticmethod
    def render_remember(
        result: WriteResult | ArtifactReceipt | UploadTicketAccepted | RememberBatchResult,
    ) -> str:
        """Render one accepted memory operation without hiding its durable identity."""
        if isinstance(result, WriteResult):
            return f"remembered document {result.id}"
        if isinstance(result, ArtifactReceipt):
            return f"accepted file {result.content_id}  {result.state}"
        if isinstance(result, UploadTicketAccepted):
            return "accepted upload ticket"
        return f"accepted {len(result.files)} files"

    @staticmethod
    def render_eta(stage: StageEstimate) -> str:
        """Render a bounded ETA range or the reason no estimate is available."""
        if stage.eta_status == "complete":
            return "complete"
        if stage.eta_status == "blocked":
            return "blocked"
        if stage.lower_seconds is None or stage.upper_seconds is None:
            return "ETA needs more history"
        return (
            f"ETA {ClientCommands.duration(stage.lower_seconds)} to "
            f"{ClientCommands.duration(stage.upper_seconds)}  {stage.confidence} confidence"
        )

    @staticmethod
    def duration(seconds: int) -> str:
        """Format a queue duration without false precision."""
        minutes = max(1, round(seconds / 60))
        hours, remaining = divmod(minutes, 60)
        if hours == 0:
            return f"{remaining} min"
        if remaining == 0:
            return f"{hours} hr"
        return f"{hours} hr {remaining} min"


@auth_app.command(name="login")
async def login(
    server: str | None = None,
    *,
    auth: Literal["oauth", "none"] = "oauth",
    callback_host: str = "127.0.0.1",
    callback_port: int = 8912,
    days: int = 30,
    json_output: JsonOutput = False,
) -> None:
    """Sign in to an MCP server and select it for later client commands."""
    await ClientCommands().login(
        server,
        auth,
        callback_host,
        callback_port,
        days,
        json_output,
    )


@auth_app.command(name="logout")
async def logout(
    *,
    server: str | None = None,
    json_output: JsonOutput = False,
) -> None:
    """Forget OAuth credentials for the selected MCP server."""
    await ClientCommands().logout(server, json_output)


@auth_app.command(name="status")
async def authentication_status(
    *,
    server: str | None = None,
    days: int = 30,
    json_output: JsonOutput = False,
) -> None:
    """Validate stored credentials without opening a browser."""
    await ClientCommands().authentication_status(server, days, json_output)


async def recall(
    query: str | None = None,
    *,
    budget: int | None = None,
    server: str | None = None,
    json_output: JsonOutput = False,
) -> None:
    """Recall evidence for a question, accepting a positional query or stdin."""
    await ClientCommands().recall(query, budget, server, json_output)


async def remember(
    *paths: Path,
    text: str | None = None,
    source_uri: str | None = None,
    observed_at: datetime | None = None,
    expires_at: datetime | None = None,
    scope: tuple[str, ...] = (),
    preserve_source: bool = False,
    server: str | None = None,
    json_output: JsonOutput = False,
) -> None:
    """Remember local paths directly, or remember text and sources through options."""
    await ClientCommands().remember(
        paths,
        text,
        source_uri,
        observed_at,
        expires_at,
        scope,
        preserve_source,
        server,
        json_output,
    )


async def share(
    *documents: UUID7,
    scope: tuple[str, ...] = (),
    server: str | None = None,
    json_output: JsonOutput = False,
) -> None:
    """Share one or more document IDs into authorized destination scopes."""
    await ClientCommands().share(documents, scope, server, json_output)


async def status(
    *,
    days: int = 30,
    server: str | None = None,
    json_output: JsonOutput = False,
) -> None:
    """Show account, usage, and processing status for the caller."""
    await ClientCommands().status(days, server, json_output)
