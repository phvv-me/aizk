# GLiNER2 behind FastAPI on the official torch runtime image. The image python is PEP 668
# externally managed, hence --break-system-packages.
# The compose build context is the package root (not this directory) so the shared wire
# models at src/aizk/serving/gate/models.py copy in beside app.py as a flat module,
# one file serving both the sidecar routes and the aizk client without duplication.

FROM pytorch/pytorch:2.13.0-cuda13.2-cudnn9-runtime

RUN pip install --no-cache-dir --break-system-packages \
    "gliner2[local]==1.3.2" "fastapi==0.139.0" "uvicorn==0.51.0" \
    "pydantic-settings==2.14.2" "click==8.3.3" "flashdeberta==0.0.7"

ENV HF_HOME=/root/.aizk/hf-cache
ENV USE_FLASHDEBERTA=1
WORKDIR /srv
COPY src/services/gliner/app.py ./
COPY src/aizk/serving/extract/long_text.py ./long_text.py
COPY src/aizk/serving/gate/models.py ./gate_models.py
COPY src/aizk/serving/extract/models.py ./extract_models.py
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
