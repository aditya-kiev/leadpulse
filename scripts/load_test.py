"""
Phase 3 — Load Testing Script

Simulates realistic concurrent traffic for a 15-50 tenant footprint.

Usage:
    # Start the app first (separate terminal):
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

    # Run load test:
    python scripts/load_test.py --base-url http://localhost:8000 --concurrency 10 --requests 50

Requirements:
    pip install httpx

Output: p50/p95/p99 latency, error rate, and throughput.
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid

import httpx

RESULTS: dict[str, list[float]] = {"send_message": []}
ERRORS: int = 0
SUCCESSES: int = 0


async def send_message(client: httpx.AsyncClient, base_url: str, session_id: str, api_key: str):
    global ERRORS, SUCCESSES
    try:
        start = time.monotonic()
        resp = await client.post(
            f"{base_url}/webhook/message",
            json={"session_id": session_id, "message": "Hello, I'm looking for your services"},
            headers={"X-Api-Key": api_key} if api_key else {},
            timeout=60,
        )
        elapsed = time.monotonic() - start
        if resp.status_code == 200:
            RESULTS["send_message"].append(elapsed)
            SUCCESSES += 1
        else:
            ERRORS += 1
            print(f"  ERROR: HTTP {resp.status_code} for session={session_id}")
    except Exception as e:
        ERRORS += 1
        print(f"  ERROR: {e} for session={session_id}")


async def worker(
    worker_id: int,
    base_url: str,
    api_key: str,
    requests_per_worker: int,
    concurrency_per_worker: int,
):
    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(concurrency_per_worker)
        tasks = []
        for i in range(requests_per_worker):
            session_id = f"loadtest-{worker_id}-{i}-{uuid.uuid4().hex[:8]}"
            tasks.append(_limited(sem, send_message(client, base_url, session_id, api_key)))
        await asyncio.gather(*tasks)


async def _limited(sem: asyncio.Semaphore, coro):
    async with sem:
        return await coro


def compute_percentiles(data: list[float], name: str):
    if not data:
        print(f"  {name}: no data")
        return
    data.sort()
    p50 = data[len(data) // 2]
    p95 = data[int(len(data) * 0.95)]
    p99 = data[int(len(data) * 0.99)]
    avg = statistics.mean(data)
    rps = len(data) / (data[-1] - data[0]) if len(data) > 1 and data[-1] > data[0] else 0
    print(f"  {name}: avg={avg:.3f}s  p50={p50:.3f}s  p95={p95:.3f}s  p99={p99:.3f}s  "
          f"rps={rps:.1f}  count={len(data)}")


def main():
    parser = argparse.ArgumentParser(description="Lead Agent load test")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, default=10, help="Total concurrent workers")
    parser.add_argument("--requests", type=int, default=50, help="Total requests per worker")
    parser.add_argument("--api-key", default="", help="X-Api-Key header value (if auth_enabled)")
    parser.add_argument("--concurrency-per-worker", type=int, default=3,
                        help="Max concurrent requests per worker")
    args = parser.parse_args()

    api_key = args.api_key or None
    print(f"Load test: {args.concurrency} workers x {args.requests} requests "
          f"(concurrency/worker={args.concurrency_per_worker})")
    print(f"Target: {args.base_url}")
    print(f"Auth: {'api-key' if api_key else 'none'}")
    print()

    start_time = time.monotonic()

    async def run():
        tasks = [
            worker(i, args.base_url, api_key, args.requests, args.concurrency_per_worker)
            for i in range(args.concurrency)
        ]
        await asyncio.gather(*tasks)

    asyncio.run(run())

    total_time = time.monotonic() - start_time
    total_reqs = SUCCESSES + ERRORS

    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"  Total time: {total_time:.2f}s")
    print(f"  Total requests: {total_reqs}")
    print(f"  Successes: {SUCCESSES}")
    print(f"  Errors: {ERRORS}")
    print(f"  Error rate: {ERRORS / total_reqs * 100:.1f}%" if total_reqs else "  Error rate: N/A")
    print(f"  Overall throughput: {total_reqs / total_time:.1f} req/s" if total_time else "")
    print()
    print("Per-endpoint latencies:")
    compute_percentiles(RESULTS["send_message"], "POST /webhook/message")
    print()
    print("Note: Latencies include Gemini API call time. In a cold-start scenario,")
    print("the first request may be slower. Run a warm-up request before measuring.")
    print()
    print("For a real benchmark, run with --concurrency equal to expected peak concurrent conversations")
    print("(e.g. 5-10 simultaneous conversations per tenant x 15-50 tenants).")


if __name__ == "__main__":
    main()
