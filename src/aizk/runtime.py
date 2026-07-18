from dataclasses import dataclass
from types import TracebackType
from typing import Self

from .artifacts.configured import ArtifactServices, build_artifact_services, build_byte_store
from .artifacts.uploads import UploadBox
from .auth import Auth
from .config import Settings
from .extract.extractor import Extractor
from .graph.build import GraphClients
from .integrations.logto import LogtoClient
from .serving.base import close_clients
from .serving.embed import EmbedClient
from .serving.extract import LLM, GLiNER
from .serving.gate import GateClient
from .serving.rerank import RerankClient
from .storage import ByteStore
from .store.engine import Database


@dataclass(frozen=True)
class Runtime:
    """The process composition root, every shared service built once from settings.

    Entrypoints (`serve-mcp`, `serve-api`, `worker`) and the test suite construct
    exactly one `Runtime`, thread each component only the dependencies it consumes,
    and close it once at the end of the process lifetime.

    Two boundaries intentionally stay process-global rather than runtime-injected:
    `Database.app()`/`Database.owner()` remain the one cached engine pair because every
    `User` session resolves its engine there (see the note in `store.engine`), and the
    Logto snapshot-cache TTLs are bound at class build time. Both read the module
    settings, so a `Runtime` assembled with alternate settings shares them.
    """

    settings: Settings
    database: Database
    store: ByteStore
    artifacts: ArtifactServices
    uploads: UploadBox
    logto: LogtoClient
    auth: Auth
    embed: EmbedClient
    rerank: RerankClient
    gate: GateClient
    llm: LLM
    extractor: Extractor
    graph: GraphClients

    @classmethod
    def assemble(cls, settings: Settings) -> Self:
        """Build the complete service graph from the given settings, once per process."""
        store = build_byte_store(settings)
        artifacts = build_artifact_services(settings, store)
        logto = LogtoClient(settings)
        embed = EmbedClient.from_settings(settings)
        gate = GateClient.from_settings(settings)
        llm = LLM.from_settings(settings)
        extractor = Extractor.configured(settings, llm, GLiNER.from_settings(settings))
        return cls(
            settings=settings,
            database=Database.app(),
            store=store,
            artifacts=artifacts,
            uploads=UploadBox.from_settings(settings, artifacts.intake),
            logto=logto,
            auth=Auth(logto, settings),
            embed=embed,
            rerank=RerankClient.from_settings(settings),
            gate=gate,
            llm=llm,
            extractor=extractor,
            graph=GraphClients(extractor=extractor, gate=gate, embed=embed, llm=llm),
        )

    async def aclose(self) -> None:
        """Close every owned network client exactly once, ending this runtime's lifetime."""
        await self.artifacts.aclose()
        await self.logto.close()
        await close_clients()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
