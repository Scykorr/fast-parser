import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from .collector import Collector, Deferred, SourceFailure
from .config import ROOT
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Бесплатный сбор футбола и локальный web UI")
    parser.add_argument("command", choices=["serve", "stop", "run-worker", "sync-fixtures", "sync-results", "validate-config", "source-check", "coverage-report", "plan-collection", "cleanup"], nargs="?", default="serve")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "serve":
        from .lifecycle import serve
        serve(args.port)
        return
    if args.command == "stop":
        from .lifecycle import stop
        raise SystemExit(0 if stop(args.port) else 1)
    store = Store(Path(os.environ.get("FAST_PARSER_DB", str(ROOT / "data" / "football.sqlite3"))))
    if args.command == "cleanup":
        print(json.dumps({"expired": store.cleanup(dry_run=args.dry_run), "dry_run": args.dry_run}))
    elif args.command == "validate-config":
        print(store.settings().model_dump_json(indent=2))
    elif args.command == "coverage-report":
        print(json.dumps(store.competitions(), ensure_ascii=True, indent=2))
    elif args.command == "plan-collection":
        n = sum(c["enabled"] for c in store.competitions() if c["id"].startswith("ol:"))
        print(json.dumps({"enabled": n, "minimum_sweep_seconds": n * 12.5, "free_only": True, "limit_rpm": 4.8, "html_limit_rpm": 3}, indent=2))
    else:
        async def run():
            collector = Collector(store)
            if args.command == "run-worker":
                await collector.run()
                return
            if not store.lease("collector", collector.owner, 60):
                raise SystemExit("Worker уже работает. Используйте кнопку проверки в web UI или остановите сервер.")
            try:
                if args.command == "source-check" or not store.competitions():
                    print("catalog", await collector.discover())
                if args.command in {"sync-fixtures", "sync-results"}:
                    for comp in store.competitions():
                        if comp["enabled"] and comp["id"].startswith("ol:"):
                            while True:
                                try:
                                    print(comp["id"], await collector.sync_league(comp))
                                    break
                                except Deferred:
                                    store.lease("collector", collector.owner, 60)
                                    await asyncio.sleep(1)
                                except SourceFailure as exc:
                                    print("source unavailable:", str(exc))
                                    raise SystemExit(1) from exc
            finally:
                await collector.client.aclose()
                store.release(collector.owner)
        try:
            asyncio.run(run())
        except (Deferred, SourceFailure) as exc:
            parser.exit(1, f"Source unavailable or in cooldown: {exc}\n")


if __name__ == "__main__":
    main()
