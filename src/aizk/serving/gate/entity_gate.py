from patos import Singleton

from ...config import settings
from ...extract import ontology


class EntityGate(Singleton):
    """The single GLiNER2 relevance gate, a 205M CPU-fast pre-filter ahead of the LLM extraction
    call.

    Classifies a chunk against the live ontology's own entity types in milliseconds and clears the
    LLM call, `graph.build.extract_and_consolidate`'s own gate ahead of `extract.llm.
    combined_extract`, only for a chunk carrying a type worth extracting. It runs GLiNER2's
    classification head (`classify_text`, "which of these types is this text about") rather than
    span extraction: with the whole ontology in play span extraction finds some label above
    threshold in almost any sentence (plain small talk scored 0.94+), so it passed everything,
    while the classification head cleanly maps chitchat onto `Person` alone and reserves the
    substantive types for text that actually carries them. In-process only, no serving lane exists
    for it since it is too fast relative to an HTTP round trip to be worth one. A `patos` singleton
    whose checkpoint loads once on first `EntityGate()` construction, so `ops.setup()` must have
    refreshed the ontology cache before this first construction runs.

    model: the loaded gliner2 checkpoint, `settings.gliner_gate_model` resolved to a local path and
        loaded on `settings.gliner_gate_device`.
    labels: the classification vocabulary, `ontology.gate_labels()`'s active entity kind names with
        `Concept` (the extractor's own explicit catch-all) dropped. `Person` deliberately stays in,
        so small talk maps onto it rather than leaking onto a substantive type, and the floor below
        excludes it from clearing the gate.
    floor: entity kinds whose presence does not by itself clear the gate, `settings.
        gliner_gate_floor`, the pronoun-level types small talk lands on. A chunk is relevant only
        when its classified types reach past this floor.
    threshold: the per-label confidence `classify_text` requires to report a type as present.
    """

    def __init__(self) -> None:
        import torch
        from gliner2 import GLiNER2
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError

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
        # resolve the checkpoint to a local path, reusing the persistent HF cache offline on every
        # warm start and downloading exactly once on a cold one. `from_pretrained` used to fetch
        # implicitly and, with no token or cache on a fresh container, hung startup; going through
        # `snapshot_download` with `local_files_only` first makes a warm start a no-network path
        # lookup and leaves the one-time download an explicit fallback, not a startup-blocking
        # surprise.
        try:
            checkpoint = snapshot_download(settings.gliner_gate_model, local_files_only=True)
        except LocalEntryNotFoundError:
            checkpoint = snapshot_download(settings.gliner_gate_model)
        self.model = GLiNER2.from_pretrained(checkpoint, map_location=settings.gliner_gate_device)
        self.floor = settings.gliner_gate_floor
        self.threshold = settings.gliner_gate_threshold

    @property
    def labels(self) -> list[str]:
        """The classification vocabulary, read fresh from the live ontology on every access.

        Auto-created kinds enter the catalog mid-run (`ontology.refresh` runs after each mint), so
        the gate must not freeze its label set at construction: a chunk about a newly minted kind
        would otherwise be scored against a stale vocabulary and wrongly dropped before the LLM
        call ever runs.
        """
        return ontology.gate_labels()

    def relevant(self, text: str) -> bool:
        """Whether a chunk carries an ontology type worth an LLM extraction call.

        A synchronous, CPU-bound forward pass. `graph.build.extract_and_consolidate` runs it
        through `asyncio.to_thread` so one chunk's gate check never blocks another's concurrent
        extraction on the event loop. The classification head reports which ontology types the
        chunk is about, and the chunk clears the gate only when those reach past the floor of
        pronoun-level catch-alls small talk maps onto.

        text: chunk span to classify against the ontology's entity types.
        """
        schema = {
            "present": {
                "labels": self.labels,
                "multi_label": True,
                "cls_threshold": self.threshold,
            }
        }
        present = set(self.model.classify_text(text, schema).get("present", []))
        return bool(present - self.floor)
