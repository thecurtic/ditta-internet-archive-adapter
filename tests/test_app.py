"""The whole app, wired from a config file, run end to end with no network."""

import json
import threading
import urllib.request

from conftest import FakeSource
from ditta_ia_adapter import config
from ditta_ia_adapter.__main__ import build_app, main
from ditta_ia_adapter.wire import accept


def test_config_to_running_server_answers_queries_offline(tmp_path, sample_results) -> None:
    assert main(["keygen", str(tmp_path / "node.key")]) == 0
    example = (config.Path(__file__).parent.parent / "config.example.toml").read_text()
    (tmp_path / "config.toml").write_text(example.replace("port = 8080", "port = 0"))
    loaded = config.load(tmp_path / "config.toml")

    app = build_app(loaded, source=FakeSource(sample_results))
    thread = threading.Thread(target=app.server.serve_forever, daemon=True)
    thread.start()
    try:
        app.worker.step()  # one background step, against the fake source only
        port = app.server.server_address[1]
        body = json.dumps({"query": "lighthouse", "max_results": 2, "request_id": "a1",
                           "pricing_type": "per_query", "max_price": 0}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}/ditta/query", data=body, method="POST")
        with urllib.request.urlopen(request) as response:
            result_set = accept(response.read(), "result_set")
        assert len(result_set["results"]) == 2
        assert app.node.announcement()["corpus"]["document_count"] == 1000
        assert (tmp_path / "terms.sqlite").exists() and (tmp_path / "peers.sqlite").exists()
    finally:
        app.server.shutdown()
        app.stop()
