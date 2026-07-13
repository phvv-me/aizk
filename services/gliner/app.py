# The gliner sidecar: the GLiNER2 gate model behind HTTP so the aizk server process never
# imports torch. GLiNER2's custom DeBERTa multitask architecture has no vLLM/SGLang serving
# path (vllm-project/vllm#42091), so plain torch behind FastAPI is the honest optimum;
# gliner2 honors USE_nDEBERTA=1 for flash-attention DeBERTa when that package is installed.
# Chunking also serves from here so every server language shares chonkie's exact boundaries.
# Every route speaks the shared wire contract the image copies in beside this file from
# src/aizk/serving/gate/contract.py, the same models the aizk client validates against.


from functools import cache

from chonkie import CodeChunker, RecursiveChunker
from contract import (
    ChunkRequest,
    ChunkResponse,
    ClassifyRequest,
    ClassifyResponse,
    ExtractRequest,
    ExtractResponse,
    HealthResponse,
)
from fastapi import FastAPI
from gliner2 import GLiNER2
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GLINER_")
    checkpoint: str = "fastino/gliner2-base-v1"
    device: str = "cuda"
    quantize: bool = True
    compile: bool = True


settings = Settings()
# Loading at import blocks uvicorn's startup until the weights are resident, so the port
# only opens on a ready model and no separate initialization hook is needed.
model = GLiNER2.from_pretrained(settings.checkpoint, map_location=settings.device)
if settings.quantize:
    model.quantize()
if settings.compile:
    model.compile()
app = FastAPI(title="GLiNER2")


@app.get("/health")
def health() -> HealthResponse:
    """Liveness for the compose healthcheck, reporting the device the weights landed on
    so a silent CUDA-to-CPU fallback is visible from outside."""
    return HealthResponse(status="ok", device=str(model.device))


@app.post("/classify")
def classify(request: ClassifyRequest) -> ClassifyResponse:
    return ClassifyResponse.model_validate(model.classify_text(**request.model_dump()))


@app.post("/extract")
def extract(request: ExtractRequest) -> ExtractResponse:
    return ExtractResponse.model_validate(model.extract_entities(**request.model_dump()))


@cache
def chunker(kind: str, chunk_size: int) -> CodeChunker | RecursiveChunker:
    return (
        CodeChunker(chunk_size=chunk_size, language="auto")
        if kind == "code"
        else RecursiveChunker(chunk_size=chunk_size)
    )


@app.post("/chunk")
def chunk(request: ChunkRequest) -> ChunkResponse:
    spans = (
        span.text.strip() for span in chunker(request.kind, request.chunk_size).chunk(request.text)
    )
    return ChunkResponse(spans=[span for span in spans if span])
