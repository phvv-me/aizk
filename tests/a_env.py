import os

os.environ["AIZK_DB_NAME"] = os.environ.get("AIZK_TEST_DB_NAME", f"aizk_test_{os.getpid()}")
os.environ["AIZK_DB_NULL_POOL"] = "1"
os.environ.setdefault("AIZK_LOG_LEVEL", "")
# The suite is hermetic above the database seam: an ambient rerank or gliner endpoint from a
# developer's .env must not reroute recall or the gate through live services. The gate client
# assumes a live sidecar, so tests point it at a fake host and stub the transport or the
# gate functions themselves.
os.environ["AIZK_RERANK_URL"] = ""
os.environ["AIZK_GLINER_URL"] = "http://gate.test"


def configured() -> bool:
    return True
