import asyncio
import json
from types import SimpleNamespace

from fast_parser.lifecycle import stop, watch_stop


def test_no_server_stop_is_safe(tmp_path):
    assert stop(8765, timeout=0, directory=tmp_path)


def test_wrong_token_cannot_stop_server(tmp_path):
    async def run():
        server = SimpleNamespace(should_exit=False)
        path = tmp_path / "request.json"
        path.write_text(json.dumps({"token": "other-token"}), encoding="utf-8")
        task = asyncio.create_task(watch_stop(server, "expected-token", path))
        await asyncio.sleep(0.05)
        assert not server.should_exit
        path.write_text(json.dumps({"token": "expected-token"}), encoding="utf-8")
        await asyncio.wait_for(task, 1)
        assert server.should_exit
    asyncio.run(run())
