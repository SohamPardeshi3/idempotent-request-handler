import uvicorn
import fakeredis
from app import main
from app.idempotency import IdempotencyGuard

main.guard = IdempotencyGuard(fakeredis.FakeRedis(decode_responses=True))

if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=4003, workers=1)
