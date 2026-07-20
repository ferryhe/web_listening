from types import SimpleNamespace

from tools import run_tree_catalog_validation as validation


def test_validation_binds_fresh_legacy_gateway_and_pacing_before_each_target(monkeypatch):
    entries = [
        {
            "site_key": "one", "abbreviation": "One", "smoke_required": True,
            "homepage_url": "https://one.example/", "monitor_url": "https://one.example/",
            "fetch_mode": "http", "fetch_config_json": {"delay_seconds": 1}, "notes": "",
        },
        {
            "site_key": "two", "abbreviation": "Two", "smoke_required": False,
            "homepage_url": "https://two.example/", "monitor_url": "https://two.example/",
            "fetch_mode": "browser", "fetch_config_json": {"wait_until": "networkidle"}, "notes": "",
        },
    ]
    events = []
    state = SimpleNamespace(tree=None)

    class Storage:
        def __init__(self, path):
            events.append("storage-init")

        def add_site(self, site):
            tree = state.tree
            events.append(("add-site", site.name, tree.acquisition_gateway, dict(tree.pacing_config)))
            return site.model_copy(update={"id": len([e for e in events if isinstance(e, tuple) and e[0] == "add-site"])})

        def close(self):
            events.append("storage-close")

    class Gateway:
        def __init__(self, crawler, *, fetch_mode, fetch_config_json):
            self.crawler = crawler
            self.fetch_mode = fetch_mode
            self.fetch_config_json = dict(fetch_config_json)
            events.append(("bind", self, crawler, fetch_mode, dict(fetch_config_json)))

        def close(self):
            return None

    class Tree:
        def __init__(self, *, storage):
            self.storage = storage
            self.crawler = SimpleNamespace(close=lambda: events.append("crawler-close"))
            self.acquisition_gateway = None
            self.pacing_config = {}
            state.tree = self
            events.append("tree-init")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("tree-close")
            self.crawler.close()

        def bootstrap_scope(self, scope, *, institution, download_files):
            events.append(("bootstrap", institution, self.acquisition_gateway, dict(self.pacing_config)))
            return SimpleNamespace(
                run=SimpleNamespace(status="completed"), pages=[], files=[], page_failures=[],
                skipped_external_pages=0, skipped_external_files=0,
                off_prefix_same_origin_files=0,
            )

    monkeypatch.setattr(validation, "load_smoke_sites", lambda path: entries)
    monkeypatch.setattr(validation, "Storage", Storage)
    monkeypatch.setattr(validation, "LegacyCrawlerGateway", Gateway)
    monkeypatch.setattr(validation, "TreeCrawler", Tree)

    results = validation.run_validation(max_depth=1, max_pages=1, max_files=0)

    bindings = [event for event in events if isinstance(event, tuple) and event[0] == "bind"]
    adds = [event for event in events if isinstance(event, tuple) and event[0] == "add-site"]
    bootstraps = [event for event in events if isinstance(event, tuple) and event[0] == "bootstrap"]
    assert len(results) == 2
    assert len(bindings) == 2
    assert bindings[0][1] is not bindings[1][1]
    assert bindings[0][2] is bindings[1][2]
    for index, entry in enumerate(entries):
        gateway = bindings[index][1]
        assert events.index(bindings[index]) < events.index(adds[index]) < events.index(bootstraps[index])
        assert adds[index][2:] == (gateway, entry["fetch_config_json"])
        assert bootstraps[index][2:] == (gateway, entry["fetch_config_json"])
        assert (gateway.fetch_mode, gateway.fetch_config_json) == (
            entry["fetch_mode"], entry["fetch_config_json"]
        )
    assert events.count("tree-init") == 1
    assert events.count("tree-close") == 1
    assert events.count("crawler-close") == 1
    assert events.count("storage-close") == 1
