# The GLiNER sidecar keeps the torch model behind HTTP so the Aizk server never
# imports torch. GLiNER2's custom DeBERTa multitask architecture has no vLLM/SGLang serving
# path (vllm-project/vllm#42091), so eager torch behind FastAPI is the honest optimum.
# Every route speaks the shared wire models copied beside this file, which are also validated
# by the Aizk client.


from extract_models import GraphRequest, GraphResponse
from fastapi import FastAPI
from gate_models import (
    ClassifyRequest,
    ClassifyResponse,
    ExtractRequest,
    ExtractResponse,
    HealthResponse,
)
from gliner2 import GLiNER2
from long_text import LongTextExtractor
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GLINER_")
    checkpoint: str = "fastino/gliner2-large-v1"
    device: str = "cuda"
    quantize: bool = True
    compile_model: bool = Field(default=False, validation_alias="GLINER_COMPILE")
    long_chunk_size: int = 384
    long_chunk_overlap: int = 64
    batch_size: int = 8


settings = Settings()
# Loading at import blocks uvicorn's startup until the weights are resident, so the port
# only opens on a ready model and no separate initialization hook is needed.
model = GLiNER2.from_pretrained(
    settings.checkpoint,
    map_location=settings.device,
    quantize=settings.quantize,
    compile=settings.compile_model,
)
long_text = LongTextExtractor(
    window_size=settings.long_chunk_size,
    overlap=settings.long_chunk_overlap,
    batch_size=settings.batch_size,
)
app = FastAPI(title="GLiNER2")


@app.get("/health")
def health() -> HealthResponse:
    """Liveness for the compose healthcheck, reporting the device the weights landed on
    so a silent CUDA-to-CPU fallback is visible from outside."""
    return HealthResponse(
        status="ok",
        device=str(model.device),
        checkpoint=settings.checkpoint,
    )


@app.post("/classify")
def classify(request: ClassifyRequest) -> ClassifyResponse:
    return ClassifyResponse.model_validate(model.classify_text(**request.model_dump()))


@app.post("/extract")
def extract(request: ExtractRequest) -> ExtractResponse:
    return ExtractResponse.model_validate(model.extract_entities(**request.model_dump()))


@app.post("/graph")
def graph(request: GraphRequest) -> GraphResponse:
    """Extract grounded ontology entities and relations in one encoder pass."""
    schema = model.create_schema().entities(request.entity_types).relations(request.relation_types)
    result = long_text.extract(model, request.text, schema, request.threshold)
    return GraphResponse.model_validate(result)
