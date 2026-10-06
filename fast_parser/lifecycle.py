"""Local, token-scoped graceful shutdown; never kill a process by name or port."""
import asyncio
import json
import secrets
import socket
import time
from pathlib import Path

from .config import ROOT


def control_paths(port: int, directory: Path | None = None):
    directory = directory or ROOT / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"server-{port}.json", directory / f"stop-{port}.json"


async def watch_stop(server, token: str, request_path: Path):
    while not server.should_exit:
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if secrets.compare_digest(str(request.get("token", "")), token):
                server.should_exit = True
                return
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.25)


def serve(port: int):
    import uvicorn

    config = uvicorn.Config("fast_parser.app:app", host="127.0.0.1", port=port, workers=1, log_level="info")
    # Bind first: a second launch must not replace the existing server's control token.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows SO_REUSEADDR permits two listeners on the same port.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.set_inheritable(True)
    except OSError as exc:
        sock.close()
        raise SystemExit(f"Cannot bind port {port}: {exc}") from exc
    runtime, request = control_paths(port)
    token = secrets.token_urlsafe(32)
    import os
    runtime.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": token}), encoding="utf-8")
    server = uvicorn.Server(config)

    async def run():
        watcher = asyncio.create_task(watch_stop(server, token, request))
        try:
            await server.serve(sockets=[sock])
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(run())
    finally:
        sock.close()
        # Only remove control files belonging to this launch.
        for path in (runtime, request):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("token") == token:
                    path.unlink()
            except (OSError, ValueError):
                pass


def stop(port: int, timeout=25, directory: Path | None = None) -> bool:
    runtime, request = control_paths(port, directory)
    if not runtime.exists():
        print(f"No managed Fast Parser server on port {port}.")
        print("Servers started before stop.cmd was added must be stopped once with Ctrl+C.")
        return True
    try:
        info = json.loads(runtime.read_text(encoding="utf-8"))
        token = info["token"]
        if not isinstance(token, str) or len(token) < 20:
            raise ValueError("Invalid control token")
        request.write_text(json.dumps({"token": token}), encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        print(f"Cannot request shutdown: {exc}")
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not runtime.exists() or json.loads(runtime.read_text(encoding="utf-8")).get("token") != token:
                print(f"Fast Parser on port {port} stopped.")
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    print("Shutdown not confirmed. The server may be busy or its control file stale; no processes were force-killed.")
    return False
