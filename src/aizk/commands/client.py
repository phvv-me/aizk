from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from cyclopts import App, Parameter
from pydantic import UUID7, AnyHttpUrl, TypeAdapter

from ..artifacts.models import ArtifactReceipt
from ..client import (
    ClientProfile,
    CommandInput,
    KeepBatchResult,
    KeepRequest,
    LocalUpload,
    MemoryClient,
    ProfileStore,
    ResultSerializer,
    ShareRequest,
)
from ..mcp.models import UploadTicketAccepted
from ..memory import ShareResult, WriteResult
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
            selected = self.profiles.load()
            return selected.model_copy(update={"server": _URL.validate_python(server)})
        return self.profiles.load()

    async def login(
        self,
        server: str | None,
        auth: Literal["oauth", "none"],
        client_id: str,
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
            client_id=client_id,
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

    async def find(
        self,
        query: str | None,
        budget: int | None,
        scopes: tuple[str, ...],
        web: Literal["auto", "off", "force"],
        fresh: bool,
        server: str | None,
        json_output: bool,
    ) -> None:
        """Answer one question through the public MCP tool."""
        resolved = CommandInput.text(query)
        if not resolved:
            raise ValueError("find requires a query argument or piped text")
        result = await MemoryClient(self.profile(server)).find(
            resolved, budget, list(scopes) or None, web, fresh
        )
        print(ResultSerializer.json(result) if json_output else result)

    async def keep(
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
        """Keep authored text, one public source, or explicit local file paths."""
        companion = CommandInput.text(text)
        result: KeepBatchResult | WriteResult | ArtifactReceipt | UploadTicketAccepted
        if paths:
            if source_uri is not None or observed_at is not None or expires_at is not None:
                raise ValueError("file paths cannot be combined with source or time options")
            if preserve_source:
                raise ValueError("preserve-source applies only to source-uri")
            result = await MemoryClient(self.profile(server)).keep_files(
                [LocalUpload(path=path) for path in paths],
                companion_text=companion,
                scopes=list(scopes) or None,
            )
        else:
            result = await MemoryClient(self.profile(server)).keep(
                KeepRequest(
                    text=companion,
                    source_uri=source_uri,
                    observed_at=observed_at,
                    expires_at=expires_at,
                    scopes=list(scopes) or None,
                    preserve_source=preserve_source,
                )
            )
        print(ResultSerializer.json(result) if json_output else self.render_keep(result))

    async def share(
        self,
        documents: tuple[UUID7, ...],
        query: str | None,
        scopes: tuple[str, ...],
        move: bool,
        limit: int,
        dry_run: bool,
        server: str | None,
        json_output: bool,
    ) -> None:
        """Share or move named documents, or preview what a query would select."""
        result = await MemoryClient(self.profile(server)).share(
            ShareRequest(
                documents=list(documents) or None,
                query=query,
                scopes=list(scopes) or None,
                move=move,
                limit=limit,
                dry_run=dry_run,
            )
        )
        print(ResultSerializer.json(result) if json_output else self.render_share(result))

    @staticmethod
    def render_share(result: ShareResult) -> str:
        """Render what a share carried, or would carry, so the operator can verify it."""
        verb = "moved" if result.moved else "shared"
        header = (
            f"preview only, nothing was written. {len(result.documents)} documents would be {verb}"
            if result.preview
            else f"{verb} {len(result.documents)} documents"
        )
        lines = [header]
        lines.extend(
            f"  {item.id}  {item.title or 'untitled'}"
            f"{'' if item.destination is None else f'  now {item.destination}'}"
            for item in result.documents
        )
        return "\n".join(lines)

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
                f"Finds  {usage.summary.finds}  "
                f"Keeps  {usage.summary.keeps}  "
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
    def render_keep(
        result: WriteResult | ArtifactReceipt | UploadTicketAccepted | KeepBatchResult,
    ) -> str:
        """Render one accepted memory operation without hiding its durable identity."""
        if isinstance(result, WriteResult):
            return f"kept document {result.id}"
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
    client_id: str = "",
    callback_host: str = "127.0.0.1",
    callback_port: int = 8912,
    days: int = 30,
    json_output: JsonOutput = False,
) -> None:
    """Sign in to an MCP server and select it for later client commands."""
    await ClientCommands().login(
        server,
        auth,
        client_id,
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


async def find(
    query: str | None = None,
    *,
    budget: int | None = None,
    scope: tuple[str, ...] = (),
    web: Literal["auto", "off", "force"] = "auto",
    fresh: bool = False,
    server: str | None = None,
    json_output: JsonOutput = False,
) -> None:
    """Answer a question from memory and the web, taking a positional query or stdin."""
    await ClientCommands().find(query, budget, scope, web, fresh, server, json_output)


async def keep(
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
    """Keep local paths directly, or keep text and sources through options."""
    await ClientCommands().keep(
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
    query: str | None = None,
    scope: tuple[str, ...] = (),
    move: bool = False,
    limit: int = 20,
    dry_run: bool = False,
    server: str | None = None,
    json_output: JsonOutput = False,
) -> None:
    """Share document IDs into destination scopes, or preview what a query would select."""
    await ClientCommands().share(
        documents, query, scope, move, limit, dry_run, server, json_output
    )


async def status(
    *,
    days: int = 30,
    server: str | None = None,
    json_output: JsonOutput = False,
) -> None:
    """Show account, usage, and processing status for the caller."""
    await ClientCommands().status(days, server, json_output)
