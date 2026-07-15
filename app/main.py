import os
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from redis import Redis

from .idempotency import IdempotencyConflict, IdempotencyGuard, IdempotencyInProgress

app = FastAPI(title="Idempotent Payments Demo")

redis_client = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
guard = IdempotencyGuard(redis_client)


@app.post("/payments")
async def create_payment(request: Request, idempotency_key: str = Header(..., alias="Idempotency-Key")):
    """
    Simulates a payment-creation endpoint. Clients are expected to send the
    same Idempotency-Key header on retries of the same logical request
    (e.g. after a network timeout where they're unsure if the original
    request landed). Retries with the same key + body replay the original
    result instead of creating a second payment.
    """
    body = await request.json()

    try:
        existing = guard.begin(idempotency_key, body)
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except IdempotencyInProgress as e:
        raise HTTPException(status_code=409, detail=str(e))

    if existing is not None:
        return JSONResponse(content=existing.body, status_code=existing.status_code)

    # --- simulate the actual payment processing that only runs once ---
    payment_id = str(uuid.uuid4())
    response_body = {
        "payment_id": payment_id,
        "amount": body.get("amount"),
        "currency": body.get("currency", "USD"),
        "status": "succeeded",
    }

    guard.complete(idempotency_key, body, 200, response_body)

    return response_body
