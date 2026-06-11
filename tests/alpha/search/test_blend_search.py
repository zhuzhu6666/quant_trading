from alpha.search.blend_search import BlendSearch, BlendSolution


def test_equal_weight_blend():
    bs = BlendSearch()
    blend = bs.equal_weight_blend(["a", "b", "c"])
    assert len(blend.coefficients) == 3
    assert all(c == 1.0 / 3 for c in blend.coefficients)
    assert blend.method == "equal_weight"
    assert blend.factor_names == ["a", "b", "c"]


def test_ic_weighted_blend():
    bs = BlendSearch()
    blend = bs.ic_weighted_blend(["a", "b"], [0.03, 0.01])
    # |0.03| = 0.03, |0.01| = 0.01, total = 0.04
    # a = 0.03/0.04 = 0.75, b = 0.01/0.04 = 0.25
    assert abs(blend.coefficients[0] - 0.75) < 1e-6
    assert abs(blend.coefficients[1] - 0.25) < 1e-6
    assert blend.method == "ic_weighted"


def test_best_returns_sorted():
    bs = BlendSearch()
    bs.add_solution(
        BlendSolution(factor_names=["a"], coefficients=[1.0], score=0.5)
    )
    bs.add_solution(
        BlendSolution(factor_names=["b"], coefficients=[1.0], score=0.9)
    )
    best = bs.best(1)
    assert best[0].score == 0.9


def test_equal_weight_blend_empty():
    bs = BlendSearch()
    blend = bs.equal_weight_blend([])
    assert blend.factor_names == []
    assert blend.coefficients == []


def test_ic_weighted_blend_empty():
    bs = BlendSearch()
    blend = bs.ic_weighted_blend([], [])
    assert blend.factor_names == []
    assert blend.coefficients == []


def test_ic_weighted_blend_zero_total():
    bs = BlendSearch()
    blend = bs.ic_weighted_blend(["a", "b"], [0.0, 0.0])
    # Falls back to equal_weight
    assert len(blend.coefficients) == 2
    assert all(c == 0.5 for c in blend.coefficients)
    assert blend.method == "equal_weight"


def test_best_empty():
    bs = BlendSearch()
    assert bs.best(1) == []


def test_add_solution_and_all():
    bs = BlendSearch()
    bs.add_solution(
        BlendSolution(factor_names=["a"], coefficients=[1.0], score=0.5)
    )
    bs.add_solution(
        BlendSolution(factor_names=["b"], coefficients=[1.0], score=0.7)
    )
    assert len(bs.best(5)) == 2


def test_ic_weighted_negative_ic():
    bs = BlendSearch()
    blend = bs.ic_weighted_blend(["a", "b"], [-0.03, 0.01])
    # abs: 0.03, 0.01 -> total 0.04
    # a = 0.03/0.04 = 0.75, b = 0.01/0.04 = 0.25
    assert abs(blend.coefficients[0] - 0.75) < 1e-6
    assert abs(blend.coefficients[1] - 0.25) < 1e-6
