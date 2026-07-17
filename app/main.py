import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis import Redis

from .idempotency import (
    IdempotencyConflict,
    IdempotencyGuard,
    IdempotencyInProgress,
    IdempotencyStillInProgress,
)

app = FastAPI(title="Idempotent Payments Demo")

redis_client = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
guard = IdempotencyGuard(redis_client)

# Simulated latency for the "actual payment processing" step below - real
# payment gateways take non-trivial time, which is exactly what makes the
# in-progress window observable in the first place. Kept small so tests
# stay fast while still being long enough to reliably exercise the
# wait-and-poll path in a concurrent test.
SIMULATED_PROCESSING_SECONDS = 0.3


class PaymentRequest(BaseModel):
    amount: int
    currency: str = "USD"


def resolve_idempotency(idempotency_key: str, body: dict):
    """
    Returns a StoredResponse to replay immediately, or None if this call
    now holds the claim and should proceed with the actual operation.
    Raises HTTPException(409) for a genuine conflict or an exhausted wait.
    """
    try:
        return guard.begin(idempotency_key, body)
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except IdempotencyInProgress:
        pass  # a concurrent duplicate is in flight - wait for it below instead of failing immediately

    try:
        existing = guard.wait_for_completion(idempotency_key, body, timeout_seconds=5.0)
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except IdempotencyStillInProgress as e:
        raise HTTPException(status_code=409, detail=str(e), headers={"Retry-After": "1"})

    if existing is not None:
        return existing

    # The claim we were waiting on vanished (its lock expired without
    # completing - e.g. that process crashed). No one holds this key
    # anymore, so try to claim it ourselves now.
    try:
        return guard.begin(idempotency_key, body)
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except IdempotencyInProgress as e:
        # Someone else grabbed it in the tiny window between our checks.
        # Rare, but possible - ask the client to retry rather than looping.
        raise HTTPException(status_code=409, detail=str(e), headers={"Retry-After": "1"})


@app.post("/payments")
def create_payment(payment: PaymentRequest, idempotency_key: str = Header(..., alias="Idempotency-Key")):
    """
    Simulates a payment-creation endpoint. Clients are expected to send the
    same Idempotency-Key header on retries of the same logical request
    (e.g. after a network timeout where they're unsure if the original
    request landed). Retries with the same key + body replay the original
    result instead of creating a second payment. Genuinely concurrent
    duplicates (not just sequential retries) wait briefly for the
    in-flight request to finish rather than failing immediately.

    Defined as a sync route (not async def) so FastAPI runs each call in
    its thread pool - that's what makes the blocking sleep below and the
    wait-and-poll path actually run concurrently across requests instead
    of serializing on a single event loop.
    """
    body = payment.model_dump()

    existing = resolve_idempotency(idempotency_key, body)
    if existing is not None:
        return JSONResponse(content=existing.body, status_code=existing.status_code)

    # --- simulate the actual payment processing that only runs once ---
    time.sleep(SIMULATED_PROCESSING_SECONDS)

    payment_id = str(uuid.uuid4())
    response_body = {
        "payment_id": payment_id,
        "amount": body.get("amount"),
        "currency": body.get("currency", "USD"),
        "status": "succeeded",
    }

    guard.complete(idempotency_key, body, 200, response_body)

    return response_body
