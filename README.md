# idempotent-request-handler

A Redis-backed idempotency guard for HTTP APIs, demonstrated on a mock
payment-creation endpoint (FastAPI). Ensures a client retry - after a
timeout, a dropped connection, or any case where the client doesn't know
if the original request landed - doesn't double-execute the operation.

## The problem

Payment and order-creation APIs get retried constantly, on purpose:
clients can't always tell whether a request failed before or after the
server-side effect happened. A naive retry means "did this payment go
through?" turns into "did I just charge them twice?" The standard fix is
a client-supplied idempotency key: same key means same logical request,
so the server executes it once and replays the stored result on any
retry.

## Design

- **Atomic claim**: `SET key value NX EX ttl` is a single Redis command,
  so claiming a key is atomic without a separate lock.
- **Body-hash scoping**: the key is stored together with a hash of the
  request body. Reusing a key with a *different* payload returns `409`
  instead of silently replaying stale (or worse, wrong) data - mirrors how
  Stripe's public API treats this exact case.
- **Two states per key**: `IN_PROGRESS` (claimed, not yet finished) and
  `COMPLETED` (final response stored for replay). A concurrent request
  hitting an `IN_PROGRESS` key also gets `409`, rather than queuing or
  blocking.
- **Bounded lock lifetime**: `IN_PROGRESS` claims expire after
  `lock_ttl_seconds` (default 30s). If the process crashes mid-request,
  the key doesn't stay locked forever - a retry after that window is
  allowed to proceed fresh. Documented tradeoff: a handler slower than
  this window could theoretically let a concurrent retry through: real
  systems should set this based on their slowest realistic handler
  duration, not leave it as a guess.
- **Wait-and-poll for genuine concurrency**: a sequential retry (client
  waits for a response before retrying) hits `begin()` and gets the
  replayed result cleanly. A *genuinely concurrent* duplicate - e.g. a
  double-clicked submit button firing two requests almost simultaneously -
  instead polls via `wait_for_completion()` for the in-flight request to
  finish, then replays its result, rather than immediately returning 409.
  Falls back to 409 with `Retry-After` only if the wait times out.

## Usage

```python
from app.idempotency import IdempotencyGuard, IdempotencyConflict, IdempotencyInProgress

guard = IdempotencyGuard(redis_client)

try:
    existing = guard.begin(idempotency_key, request_body)
except IdempotencyConflict:
    # same key, different body - client error
    ...
except IdempotencyInProgress:
    # a concurrent duplicate is already running - wait for it instead of failing
    existing = guard.wait_for_completion(idempotency_key, request_body, timeout_seconds=5.0)

if existing is not None:
    return existing.body  # replay, don't re-run the operation

result = do_the_actual_work()
guard.complete(idempotency_key, request_body, 200, result)
return result
```

## Running it

```bash
pip install -r requirements-dev.txt
pytest -v                              # 11 tests, no real Redis needed (fakeredis)
REDIS_URL=redis://localhost:6379 uvicorn app.main:app --reload   # real server
```

## Try it live

```bash
curl -X POST localhost:8000/payments \
  -H "Idempotency-Key: order-1" -H "Content-Type: application/json" \
  -d '{"amount": 4200, "currency": "USD"}'

# retry with the same key + body -> same payment_id, not a new payment
curl -X POST localhost:8000/payments \
  -H "Idempotency-Key: order-1" -H "Content-Type: application/json" \
  -d '{"amount": 4200, "currency": "USD"}'

# same key, different body -> 409
curl -X POST localhost:8000/payments \
  -H "Idempotency-Key: order-1" -H "Content-Type: application/json" \
  -d '{"amount": 9999, "currency": "USD"}'
```

## Possible extensions

- Per-route TTL configuration (a fast lookup endpoint and a slow batch
  endpoint shouldn't share the same lock timeout)
- Idempotency key expiry sweep metrics - how often keys are reused past
  their result TTL, which would indicate clients waiting too long to retry
