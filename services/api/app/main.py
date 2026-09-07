"""Minimal local FastAPI entrypoint for the Content OS scaffold."""

from fastapi import FastAPI

app = FastAPI(
    title="Content OS API",
    version="0.1.0",
    description="Local-first API scaffold for Content OS.",
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Report that the local API process is ready to accept requests."""
    return {"status": "ok", "service": "content-os-api"}
