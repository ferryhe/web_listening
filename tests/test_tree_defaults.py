from web_listening.tree_defaults import PRODUCTION_TREE_LIMITS


def test_production_tree_limits_are_large_enough_for_whole_site_monitoring():
    assert PRODUCTION_TREE_LIMITS.max_depth == 4
    assert PRODUCTION_TREE_LIMITS.max_pages == 120
    assert PRODUCTION_TREE_LIMITS.max_files == 40
