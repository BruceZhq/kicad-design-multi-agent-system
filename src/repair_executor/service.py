"""Private authenticated repair broker; no browser-facing port."""

import hmac
import os
import threading

from fastapi import FastAPI, Header, HTTPException, Request

from ratsnestpro.repair.contracts import SandboxRequest, SandboxResult
from repair_executor.docker_runner import run

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_slot = threading.BoundedSemaphore(1)


@app.middleware("http")
async def bounded_body(request: Request, call_next):
    if request.method == "POST":
        from starlette.responses import JSONResponse

        key = os.environ.get("RATSNEST_REPAIR_EXECUTOR_TOKEN", "")
        if len(key) < 32 or not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + key
        ):
            return JSONResponse(
                {"detail": "repair executor authorization required"}, status_code=403
            )
        # Reject unknown-length transfer before buffering untrusted payloads.
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            size = 0
        if not 0 < size <= 34_000_000:
            return JSONResponse({"detail": "bounded content-length required"}, status_code=413)
    return await call_next(request)


@app.post("/v1/repair", response_model=SandboxResult)
def repair(body: SandboxRequest, authorization: str = Header(default="")):
    key = os.environ.get("RATSNEST_REPAIR_EXECUTOR_TOKEN", "")
    if len(key) < 32 or not hmac.compare_digest(authorization, "Bearer " + key):
        raise HTTPException(403, "repair executor authorization required")
    image = os.environ.get("RATSNEST_REPAIR_SANDBOX_IMAGE", "")
    if not image:
        raise HTTPException(503, "sandbox image not configured")
    if not _slot.acquire(blocking=False):
        raise HTTPException(429, "repair executor busy")
    try:
        return run(body, image=image)
    finally:
        _slot.release()


@app.get("/health")
def health():
    return {"status": "ok"}
