from alpha.search.strategy_search import StrategySearch, StrategyTemplate


def test_register_and_get():
    ss = StrategySearch()
    t = StrategyTemplate(
        name="test1",
        signal_aggregation="vote",
        threshold=0.5,
        sizing="fixed",
    )
    ss.register(t)
    assert ss.get("test1") is t


def test_best_returns_sorted():
    ss = StrategySearch()
    ss.register(StrategyTemplate(name="a", score=0.5))
    ss.register(StrategyTemplate(name="b", score=0.9))
    best = ss.best(1)
    assert len(best) == 1
    assert best[0].name == "b"


def test_all_returns_all():
    ss = StrategySearch()
    ss.register(StrategyTemplate(name="a"))
    ss.register(StrategyTemplate(name="b"))
    assert len(ss.all()) == 2


def test_get_missing_returns_none():
    ss = StrategySearch()
    assert ss.get("nonexistent") is None


def test_best_respects_k():
    ss = StrategySearch()
    ss.register(StrategyTemplate(name="a", score=0.3))
    ss.register(StrategyTemplate(name="b", score=0.9))
    ss.register(StrategyTemplate(name="c", score=0.6))
    best_two = ss.best(2)
    assert len(best_two) == 2
    assert best_two[0].name == "b"
    assert best_two[1].name == "c"


def test_best_empty_returns_empty():
    ss = StrategySearch()
    assert ss.best(1) == []


def test_register_overwrites():
    ss = StrategySearch()
    t1 = StrategyTemplate(name="x", score=0.5)
    t2 = StrategyTemplate(name="x", score=0.9)
    ss.register(t1)
    ss.register(t2)
    assert ss.get("x") is t2
