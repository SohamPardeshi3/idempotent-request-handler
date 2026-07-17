import threading
import time

import fakeredis
import pytest

from app.idempotency import (
    IdempotencyConflict,
    IdempotencyGuard,
    IdempotencyInProgress,
    IdempotencyStillInProgress,
)


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def guard(redis_client):
    return IdempotencyGuard(redis_client, lock_ttl_seconds=1, result_ttl_seconds=60)


def test_first_call_proceeds(guard):
    result = guard.begin("key-1", {"amount": 100})
    assert result is None  # None means "you won the claim, go ahead"


def test_retry_after_completion_replays_stored_response(guard):
    body = {"amount": 100}
    guard.begin("key-2", body)
    guard.complete("key-2", body, 200, {"payment_id": "abc", "status": "succeeded"})

    replayed = guard.begin("key-2", body)
    assert replayed is not None
    assert replayed.status_code == 200
    assert replayed.body == {"payment_id": "abc", "status": "succeeded"}


def test_concurrent_in_progress_request_is_rejected(guard):
    body = {"amount": 100}
    guard.begin("key-3", body)  # first request claims the key, never completes

    with pytest.raises(IdempotencyInProgress):
        guard.begin("key-3", body)


def test_same_key_different_body_raises_conflict(guard):
    guard.begin("key-4", {"amount": 100})

    with pytest.raises(IdempotencyConflict):
        guard.begin("key-4", {"amount": 200})


def test_same_key_different_body_conflict_detected_even_after_completion(guard):
    body_a = {"amount": 100}
    guard.begin("key-5", body_a)
    guard.complete("key-5", body_a, 200, {"status": "succeeded"})

    with pytest.raises(IdempotencyConflict):
        guard.begin("key-5", {"amount": 999})


def test_abandoned_lock_expires_and_allows_retry(guard):
    body = {"amount": 100}
    guard.begin("key-6", body)  # claims but never completes - simulates a crash

    time.sleep(1.2)  # past the 1-second lock_ttl_seconds set in the fixture

    result = guard.begin("key-6", body)
    assert result is None, "expired lock should allow a fresh attempt"


def test_different_keys_are_fully_independent(guard):
    guard.begin("key-7a", {"amount": 100})
    result = guard.begin("key-7b", {"amount": 100})
    assert result is None, "a different idempotency key should not be affected by another key's state"


def test_wait_for_completion_returns_result_once_original_finishes(guard):
    body = {"amount": 100}
    guard.begin("key-8", body)  # claim it, simulating an in-flight request

    def finish_it_shortly():
        time.sleep(0.15)
        guard.complete("key-8", body, 200, {"payment_id": "xyz", "status": "succeeded"})

    threading.Thread(target=finish_it_shortly).start()

    # poll_interval smaller than the 0.15s delay above so we actually catch
    # the IN_PROGRESS state at least once before it completes
    result = guard.wait_for_completion("key-8", body, timeout_seconds=2.0, poll_interval_seconds=0.03)

    assert result is not None
    assert result.body == {"payment_id": "xyz", "status": "succeeded"}


def test_wait_for_completion_times_out_if_never_completed(guard):
    body = {"amount": 100}
    guard.begin("key-9", body)  # claimed, never completed

    with pytest.raises(IdempotencyStillInProgress):
        guard.wait_for_completion("key-9", body, timeout_seconds=0.2, poll_interval_seconds=0.05)


def test_wait_for_completion_raises_conflict_on_hash_mismatch(guard):
    guard.begin("key-10", {"amount": 100})

    with pytest.raises(IdempotencyConflict):
        guard.wait_for_completion("key-10", {"amount": 999}, timeout_seconds=0.3, poll_interval_seconds=0.05)
