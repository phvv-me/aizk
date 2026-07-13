# GLiNER2 on GPU behind FastAPI, on the official torch CUDA 13 runtime image, which already
# carries the C compiler torch.compile's inductor needs. The image python is PEP 668
# externally managed, hence --break-system-packages.
# The compose build context is the package root (not this directory) so the shared wire
# contract at src/aizk/serving/gate/contract.py copies in beside app.py as a flat module,
# one file serving both the sidecar routes and the aizk client without duplication.

FROM pytorch/pytorch:2.13.0-cuda13.2-cudnn9-runtime

RUN pip install --no-cache-dir --break-system-packages \
    "gliner2[local]>=1.3.2,<2" "fastapi>=0.115,<1" "uvicorn>=0.30,<1" \
    "pydantic-settings>=2.5,<3" "chonkie[code]>=1.7,<2"

ENV HF_HOME=/root/.aizk/hf-cache
WORKDIR /srv
COPY services/gliner/app.py src/aizk/serving/gate/contract.py ./
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
