import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import main
from app.idempotency import IdempotencyGuard


@pytest.fixture(autouse=True)
def fake_redis_guard():
    """Swaps the app's real Redis-backed guard for a fakeredis-backed one for every test."""
    fake_client = fakeredis.FakeRedis(decode_responses=True)
    main.guard = IdempotencyGuard(fake_client, lock_ttl_seconds=1, result_ttl_seconds=60)
    yield


@pytest.fixture
def client():
    return TestClient(main.app)


def test_creates_a_payment(client):
    res = client.post(
        "/payments",
        json={"amount": 500, "currency": "USD"},
        headers={"Idempotency-Key": "order-1"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "succeeded"
    assert res.json()["amount"] == 500


def test_retrying_same_key_and_body_does_not_create_a_second_payment(client):
    headers = {"Idempotency-Key": "order-2"}
    body = {"amount": 250, "currency": "USD"}

    first = client.post("/payments", json=body, headers=headers)
    second = client.post("/payments", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    # Same payment_id proves the second call replayed the first result
    # rather than running the payment logic again.
    assert first.json()["payment_id"] == second.json()["payment_id"]


def test_reusing_key_with_different_body_returns_409(client):
    headers = {"Idempotency-Key": "order-3"}

    client.post("/payments", json={"amount": 100}, headers=headers)
    conflicting = client.post("/payments", json={"amount": 999}, headers=headers)

    assert conflicting.status_code == 409


def test_missing_idempotency_key_header_is_rejected(client):
    res = client.post("/payments", json={"amount": 100})
    assert res.status_code == 422  # FastAPI's required-header validation error
