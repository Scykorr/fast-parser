"""Synthetic 100k-match API benchmark, isolated from the real application's database."""
import asyncio
import gzip
import json
import os
import platform
import statistics
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx

from fast_parser.app import create_app
from fast_parser.config import ROOT
from fast_parser.domain import Match, stamp, utcnow


async def main():
    target = ROOT / "data" / "benchmarks" / uuid.uuid4().hex / "football.sqlite3"
    app = create_app(target, start_worker=False)
    store = app.state.store
    now = utcnow()
    comps = [{"id": f"bench:{i}", "shortcut": f"bench:{i}", "season": "2026", "name": f"Synthetic League {i}", "country": "Synthetic", "category": "Test"} for i in range(100)]
    store.catalog(comps)
    insert_start = time.perf_counter()
    for ci, comp in enumerate(comps):
        batch = []
        for i in range(1000):
            mid = ci * 1000 + i
            m = Match(id=f"bench:{mid:06}", competition_id=comp["id"], source="benchmark", external_id=str(mid),
                      home_team={"id": "1", "name": "Synthetic Home"}, away_team={"id": "2", "name": "Synthetic Away"},
                      kickoff_at=stamp(now + timedelta(minutes=i)), status="unknown" if i < 10 else "scheduled")
            batch.append(m)
        store.upsert_matches(batch, now)
    insertion_seconds = time.perf_counter() - insert_start
    latencies = []
    sem = asyncio.Semaphore(20)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        async def query(i):
            async with sem:
                start = time.perf_counter()
                r = await client.get("/api/v1/matches", params={"competition_id": f"bench:{i%100}", "limit": 100})
                assert r.status_code == 200 and len(r.json()["items"]) == 100
                latencies.append((time.perf_counter() - start) * 1000)
        await asyncio.gather(*(query(i) for i in range(200)))
    # Measure physical expiry of a batch without real-time waiting.
    for i in range(1000):
        m = Match(id=f"expired:{i:04}", competition_id=comps[0]["id"], source="benchmark", external_id=f"e{i}",
                  home_team={"id": "1", "name": "Synthetic Home"}, away_team={"id": "2", "name": "Synthetic Away"},
                  kickoff_at=stamp(now - timedelta(hours=2)), status="finished")
        store.upsert_matches([m], now)
    cleanup_start = time.perf_counter()
    deleted = store.cleanup(now + timedelta(hours=73))
    cleanup_ms = (time.perf_counter() - cleanup_start) * 1000
    assert deleted == 1000
    size = sum(len(gzip.compress(p.read_bytes())) for p in [ROOT / "fast_parser/templates/index.html", ROOT / "fast_parser/static/app.js", ROOT / "fast_parser/static/style.css"])
    result = {"python": platform.python_version(), "platform": platform.platform(), "logical_cpus": os.cpu_count(),
              "profile": "100000 synthetic matches; 100 leagues; 1000 unresolved; 20 concurrent in-process ASGI clients; 200 requests",
              "limitations": "Host resource limits were not constrained to 2 vCPU / 4 GB. Browser runs and HTTP socket checks are separate.",
              "insertion_seconds": round(insertion_seconds, 3), "api_p50_ms": round(statistics.median(latencies), 3),
              "api_p95_ms": round(sorted(latencies)[189], 3), "cleanup_1000_ms": round(cleanup_ms, 3),
              "frontend_gzip_bytes": size}
    (ROOT / "data/qa").mkdir(parents=True, exist_ok=True)
    (ROOT / "data/qa/benchmark-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
