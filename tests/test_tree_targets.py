from web_listening.tree_targets import load_dev_tree_targets, load_smoke_tree_targets


def test_load_dev_tree_targets_contains_required_dev_sites():
    targets = load_dev_tree_targets()
    site_keys = {item.site_key for item in targets}

    assert {"soa", "cas", "iaa"}.issubset(site_keys)
    assert all(item.catalog == "dev" for item in targets)
    assert all(item.allowed_page_prefixes == ["/"] for item in targets)
    assert all(item.allowed_file_prefixes == ["/"] for item in targets)


def test_load_smoke_tree_targets_contains_large_catalog():
    targets = load_smoke_tree_targets()

    assert len(targets) >= 30
    assert all(item.catalog == "smoke" for item in targets)
    assert all(item.seed_url.startswith("https://") for item in targets)
