from backend.api import learning


def test_learning_cache_invalidation_clears_parameter_and_factor_card_views():
    learning._learning_cache_set("parameter_templates:list:db:*:*:10", {"items": [1]})
    learning._learning_cache_set("factor_cards:10:*:*:*:*", {"items": [2]})
    learning._learning_cache_set("unrelated:key", {"items": [3]})

    learning._learning_cache_invalidate("summary")

    assert learning._learning_cache_get("parameter_templates:list:db:*:*:10") is None
    assert learning._learning_cache_get("factor_cards:10:*:*:*:*") is None
    assert learning._learning_cache_get("unrelated:key") == {"items": [3]}

    learning._learning_cache_invalidate()
