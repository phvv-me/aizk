from patos import Singleton

from ...config import settings
from ...extract.ontology import EntityType


class EntityGate(Singleton):
    """The single GLiNER2 relevance gate, a 205M CPU-fast pre-filter ahead of the LLM extraction
    call.

    Scores a chunk against the ontology's own entity types in milliseconds; a chunk that names
    none of them clears no LLM call at all, `graph.build.extract_and_consolidate`'s own gate ahead
    of `extract.llm.combined_extract`. In-process only, no serving lane exists for it since it is
    too fast relative to an HTTP round trip to be worth one. A `patos` singleton, the checkpoint
    loaded once on first `EntityGate()` construction.

    model: the loaded gliner2 checkpoint, `settings.gliner_gate_model` on `settings.
        gliner_gate_device`.
    labels: the schema every chunk is scored against, the ontology's extractable entity type
        names minus `Concept`. Concept is the extractor's own explicit catch-all ("when nothing
        fits use Concept"), and calibration against real prose showed it matches nearly any noun
        phrase (a plain "the weather" scored 0.79), which would make the gate pass everything;
        dropping it widened the measured separation between filler and ontology-bearing text from
        roughly 0.5-0.8 down to 0.0-0.5 for filler and 0.9+ for the real thing.
    threshold: the confidence a chunk's best entity match must clear to count as relevant.
    """

    def __init__(self) -> None:
        import torch
        from gliner2 import GLiNER2

        # torch's CPU intra-op default is one thread per core for every single forward pass; the
        # gate's own concurrency instead comes from `asyncio.to_thread`'s worker pool running many
        # calls side by side, so leaving torch's own default in place means every one of those
        # concurrent calls independently fans out across all cores at once. Measured under
        # settings.graph_build_concurrency-wide concurrent extraction, that oversubscription
        # (dozens of workers each spawning a core's worth of threads) collapsed real throughput to
        # near-single-threaded and starved the LLM endpoint of any request at all, GPU idle the
        # whole time; capping torch to one thread per call and letting the worker pool provide the
        # real parallelism is the standard fix for many concurrent small CPU inferences. Global on
        # the process since nothing else here runs in-process torch (embed/rerank/llm all serve
        # over HTTP from their own vLLM containers).
        torch.set_num_threads(1)
        self.model = GLiNER2.from_pretrained(
            settings.gliner_gate_model, map_location=settings.gliner_gate_device
        )
        self.labels = [
            member.value for member in EntityType.extractable() if member != EntityType.CONCEPT
        ]
        self.threshold = settings.gliner_gate_threshold

    def relevant(self, text: str) -> bool:
        """Whether a chunk names at least one entity of the ontology's own types.

        A synchronous, CPU-bound forward pass; `graph.build.extract_and_consolidate` runs it
        through `asyncio.to_thread` so one chunk's gate check never blocks another's concurrent
        extraction on the event loop.

        text: chunk span to score against the ontology's entity labels.
        """
        result = self.model.extract_entities(text, self.labels, include_confidence=True)
        return any(
            candidate["confidence"] >= self.threshold
            for matches in result.get("entities", {}).values()
            for candidate in matches
        )
