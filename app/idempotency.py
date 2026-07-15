"""
Ensures an operation identified by a client-supplied idempotency key runs
at most once, even if the client retries after a timeout or dropped
connection - the standard problem for payment/order-creation APIs, where a
blind retry must not double-charge or double-create.

Design mirrors how Stripe's public API handles idempotency keys: the key
is scoped together with a hash of the request body, so reusing a key with
a *different* payload is treated as a client error, not silently replayed
with stale data. That distinction catches a real bug class - a client
library reusing a key across genuinely different requests, e.g. after a
copy-paste error in a retry wrapper.

Kept independent of FastAPI (or any framework) so the same guard could
back a gRPC handler, a queue consumer, or a CLI tool - the FastAPI route
in main.py is a thin integration on top of this.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class IdempotencyState(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class IdempotencyConflict(Exception):
    """Raised when the same idempotency key is reused with a different request body."""


class IdempotencyInProgress(Exception):
    """Raised when a request with this exact key and body is already being processed."""


@dataclass
class StoredResponse:
    status_code: int
    body: dict


class IdempotencyGuard:
    def __init__(self, redis_client, lock_ttl_seconds: int = 30, result_ttl_seconds: int = 86400):
        """
        lock_ttl_seconds: how long an IN_PROGRESS claim survives before it's
        considered abandoned (e.g. the process crashed mid-request) and a
        retry is allowed to proceed again. Short on purpose - this is the
        accepted tradeoff: if a slow request takes longer than this to
        finish, a concurrent retry could double-execute. Real systems set
        this based on the slowest realistic handler duration, not left as
        a guess.

        result_ttl_seconds: how long a COMPLETED response is remembered
        for replay to retries. Default 24h, generous for payment-style
        retry windows.
        """
        self.redis = redis_client
        self.lock_ttl_seconds = lock_ttl_seconds
        self.result_ttl_seconds = result_ttl_seconds

    @staticmethod
    def _hash_body(body: dict) -> str:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _key(self, idempotency_key: str) -> str:
        return f"idempotency:{idempotency_key}"

    def begin(self, idempotency_key: str, request_body: dict) -> Optional[StoredResponse]:
        """
        Attempts to claim the idempotency key for this request.

        Returns None if the caller should proceed with the actual operation
        (this call won the claim).
        Returns a StoredResponse if a completed result already exists and
        should be replayed as-is, without re-running the operation.

        Raises IdempotencyConflict if the key was previously used with a
        different request body.
        Raises IdempotencyInProgress if another request with this exact key
        is currently mid-flight.
        """
        request_hash = self._hash_body(request_body)
        key = self._key(idempotency_key)

        record = json.dumps({"state": IdempotencyState.IN_PROGRESS.value, "hash": request_hash})

        # SET ... NX is a single atomic Redis command - the claim itself
        # needs no separate locking scheme.
        claimed = self.redis.set(key, record, nx=True, ex=self.lock_ttl_seconds)
        if claimed:
            return None

        existing_raw = self.redis.get(key)
        if existing_raw is None:
            # Rare race: the lock expired between our failed SET NX and this
            # GET. The original attempt either finished or died - either
            # way it's safe to let this retry proceed as a fresh attempt.
            return None

        existing = json.loads(existing_raw)

        if existing["hash"] != request_hash:
            raise IdempotencyConflict(
                f"Idempotency key '{idempotency_key}' was already used with a different request body"
            )

        if existing["state"] == IdempotencyState.IN_PROGRESS.value:
            raise IdempotencyInProgress(
                f"A request with idempotency key '{idempotency_key}' is already in progress"
            )

        return StoredResponse(status_code=existing["status_code"], body=existing["body"])

    def complete(self, idempotency_key: str, request_body: dict, status_code: int, response_body: dict) -> None:
        """Stores the final response so future retries with this key replay it instead of re-executing."""
        request_hash = self._hash_body(request_body)
        key = self._key(idempotency_key)

        record = json.dumps({
            "state": IdempotencyState.COMPLETED.value,
            "hash": request_hash,
            "status_code": status_code,
            "body": response_body,
        })

        self.redis.set(key, record, ex=self.result_ttl_seconds)
