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


def test_archive_keys_use_the_librarys_variable_names(tmp_path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text(
        '[node]\nendpoint = "https://example.com"\n[archive]\ncontact_url = "https://example.com"\n'
    )
    monkeypatch.setenv("IA_ACCESS_KEY_ID", "official-access")
    monkeypatch.setenv("IA_SECRET_ACCESS_KEY", "official-secret")
    monkeypatch.setenv("IA_ACCESS_KEY", "old-access")
    loaded = config.load(tmp_path / "config.toml")
    assert (loaded.access_key, loaded.secret_key) == ("official-access", "official-secret")

    monkeypatch.delenv("IA_ACCESS_KEY_ID")
    monkeypatch.delenv("IA_SECRET_ACCESS_KEY")
    monkeypatch.setenv("IA_SECRET_KEY", "old-secret")
    loaded = config.load(tmp_path / "config.toml")
    assert (loaded.access_key, loaded.secret_key) == ("old-access", "old-secret")


# --- statistics scope -----------------------------------------------------------


def scoped(tmp_path, **settings) -> config.Config:
    loaded = config.Config(endpoint="https://example.com/ditta", contact_url="https://example.com",
                           key_file=tmp_path / "node.key")
    for name, value in settings.items():
        setattr(loaded, name, value)
    return loaded


def test_counts_scope_changes_with_backend_and_filters_only(tmp_path) -> None:
    from ditta_ia_adapter.__main__ import counts_scope

    base = counts_scope(scoped(tmp_path))
    assert counts_scope(scoped(tmp_path, port=9, max_results=10, candidate_pool=300)) == base
    assert counts_scope(scoped(tmp_path, languages=["EN"])) == base  # tags are case-blind
    for change in (
        {"backend": "fulltext"},
        {"languages": ["fr"]},
        {"filter_languages": False},
        {"include_unlabeled": False},
        {"mediatypes": ["texts", "audio"]},
    ):
        assert counts_scope(scoped(tmp_path, **change)) != base, change


def test_switching_backend_restarts_the_announced_statistics(tmp_path) -> None:
    from ditta_ia_adapter.terms.worker import DOCUMENT_COUNT

    main(["keygen", str(tmp_path / "node.key")])
    settings = dict(port=0, terms_db=tmp_path / "terms.sqlite", peers_db=tmp_path / "peers.sqlite")

    first = build_app(scoped(tmp_path, **settings), source=FakeSource())
    first.worker.step()  # corpus size
    first.node._terms.note_terms(["lighthouse"])
    first.worker.step()  # one term
    assert first.node.announcement()["corpus"]["term_count"] == 1
    first.stop()

    same = build_app(scoped(tmp_path, **settings), source=FakeSource())
    assert same.node.announcement()["corpus"]["term_count"] == 1
    same.stop()

    switched = build_app(scoped(tmp_path, backend="fulltext", **settings), source=FakeSource())
    corpus = switched.node.announcement()["corpus"]
    assert (corpus["document_count"], corpus["term_count"], corpus["updated"]) == (0, 0, 0)
    switched.worker.step()
    assert switched.node._terms.get_meta(DOCUMENT_COUNT) == 1000  # recounted first
    switched.stop()
