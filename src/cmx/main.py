"""CMX Decision Engine API."""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

from cmx import __version__
from cmx.engine import evaluate_decision
from cmx.models import DecisionRequest, DecisionResponse

app = FastAPI(
    title="CMX Decision Engine",
    summary="Evidence-aware decision support for Customer & Market Experience.",
    description=(
        "A local-first decision engine that compares supplied candidate records "
        "against explicit requirements. It does not browse the web, validate sources, "
        "or connect to an LLM in this version."
    ),
    version=__version__,
)


@app.get("/health", tags=["Operations"])
def health() -> dict[str, str]:
    """Readiness endpoint; does not claim external dependencies are healthy."""
    return {"status": "ok", "service": "cmx-decision-engine", "version": __version__}


@app.get("/api/v1", tags=["Operations"])
def api_info() -> dict[str, str]:
    return {
        "name": "CMX Decision Engine",
        "version": __version__,
        "brand": "CMX",
        "meaning": "Customer & Market Experience",
    }


@app.post(
    "/api/v1/assessments/evaluate",
    response_model=DecisionResponse,
    tags=["Decision Support"],
)
def evaluate_assessment(request: DecisionRequest) -> DecisionResponse:
    """Evaluate supplied candidates using deterministic rules and explicit evidence."""
    try:
        return evaluate_decision(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def run() -> None:
    """Run locally. Public deployment must add authentication and controls."""
    import uvicorn

    host = os.getenv("CMX_HOST", "127.0.0.1")
    port = int(os.getenv("CMX_PORT", "8000"))
    uvicorn.run("cmx.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
