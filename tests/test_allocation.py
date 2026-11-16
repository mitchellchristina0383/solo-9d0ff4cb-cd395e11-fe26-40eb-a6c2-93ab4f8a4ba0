"""分摊纯逻辑单元测试：几何、覆盖校验、最大余数法、守恒。"""
from __future__ import annotations

import random

import pytest

from app import allocation as alloc
from app.allocation import (
    NewDistrict,
    OldCell,
    allocate,
    largest_remainder_share,
    overlap_area,
    rect_area,
    summarize,
)


def test_rect_area_and_overlap_basic():
    assert rect_area((0, 0, 10, 10)) == 100
    assert overlap_area((0, 0, 10, 10), (5, 5, 15, 15)) == 25
    # 仅边界相接不算重叠
    assert overlap_area((0, 0, 5, 5), (5, 0, 10, 5)) == 0
    assert overlap_area((0, 0, 5, 5), (6, 6, 9, 9)) == 0


def test_internal_overlap_detection():
    with pytest.raises(ValueError):
        alloc.assert_no_internal_overlap(
            [("a", (0, 0, 5, 5)), ("b", (4, 4, 9, 9))]
        )
    # 相接不报错
    alloc.assert_no_internal_overlap([("a", (0, 0, 5, 5)), ("b", (5, 0, 10, 5))])


def test_full_coverage_required():
    old = [OldCell("g1", (0, 0, 10, 10), 100)]
    # 片区只覆盖一半面积 -> 拒绝
    districts = [NewDistrict("d1", (0, 0, 5, 10))]
    with pytest.raises(ValueError, match="未被新片区完整覆盖"):
        allocate(old, districts)


def test_district_may_extend_beyond_old_grid():
    # 片区延伸到旧网格之外是允许的，只要旧格被完整覆盖
    old = [OldCell("g1", (0, 0, 10, 10), 40)]
    districts = [
        NewDistrict("d1", (-5, -5, 5, 15)),   # 与旧格重叠 5*10=50
        NewDistrict("d2", (5, 0, 20, 10)),    # 重叠 5*10=50
    ]
    result = allocate(old, districts)
    assert sum(a.households for a in result) == 40


def test_even_split_with_max_remainder_and_id_tiebreak():
    # 两个等面积片区分 3 户：各 1.5，余数并列，编号升序者 +1
    old = [OldCell("g1", (0, 0, 10, 10), 3)]
    districts = [
        NewDistrict("d2", (5, 0, 10, 10)),
        NewDistrict("d1", (0, 0, 5, 10)),
    ]
    result = {a.district_id: a.households for a in allocate(old, districts)}
    assert result == {"d1": 2, "d2": 1}


def test_largest_remainder_general():
    # 权重 1:1:1，分 10 户 -> 3,3,4；最大余数给编号升序最后一个？
    # 精确值 3.333 三个余数相同，+1 顺序按编号升序：d1 先得
    shares = largest_remainder_share([("d1", 1), ("d2", 1), ("d3", 1)], 10)
    assert shares == {"d1": 4, "d2": 3, "d3": 3}
    assert sum(shares.values()) == 10

    # 权重 3:1 分 5 户：3.75 / 1.25 -> 3/1，余数 0.75 给 d1 -> 4/1
    shares = largest_remainder_share([("d1", 3), ("d2", 1)], 5)
    assert shares == {"d1": 4, "d2": 1}


def test_numeric_ids_sorted_numerically():
    # 编号 "2" 与 "10"：数值序 2 在 10 前；字符串序会相反
    old = [OldCell("g1", (0, 0, 10, 10), 1)]
    districts = [
        NewDistrict("10", (0, 0, 5, 10)),
        NewDistrict("2", (5, 0, 10, 10)),
    ]
    shares = largest_remainder_share([("10", 50), ("2", 50)], 1)
    assert sorted(shares, key=alloc._id_sort_key) == ["2", "10"]
    assert shares["2"] == 1
    result = allocate(old, districts)
    assert result[0].district_id == "2" and result[0].households == 1


def test_zero_households_cell_conservation():
    old = [OldCell("g1", (0, 0, 10, 10), 0)]
    districts = [
        NewDistrict("d1", (0, 0, 5, 10)),
        NewDistrict("d2", (5, 0, 10, 10)),
    ]
    result = allocate(old, districts)
    assert all(a.households == 0 for a in result)
    assert sum(a.households for a in result) == 0


def test_multi_cell_conservation_and_summary():
    old = [
        OldCell("g1", (0, 0, 10, 10), 7),
        OldCell("g2", (0, 10, 10, 20), 11),
    ]
    districts = [
        NewDistrict("d1", (0, 0, 5, 20)),
        NewDistrict("d2", (5, 0, 10, 20)),
    ]
    allocations = allocate(old, districts)
    # 逐格守恒
    for cell in old:
        part = [a for a in allocations if a.old_id == cell.id]
        assert sum(a.households for a in part) == cell.households
    # 总守恒
    assert sum(a.households for a in allocations) == 18

    summary = summarize(allocations, districts)
    assert [s["district_id"] for s in summary] == ["d1", "d2"]
    assert sum(s["allocated_households"] for s in summary) == 18
    # 每个片区都有明细（两个旧格各一条）
    assert all(len(s["detail"]) == 2 for s in summary)


def test_district_with_no_overlap_appears_in_summary_with_zero():
    old = [OldCell("g1", (0, 0, 10, 10), 5)]
    districts = [
        NewDistrict("d1", (0, 0, 10, 10)),
        NewDistrict("d2", (100, 100, 110, 110)),  # 与旧格无交集
    ]
    allocations = allocate(old, districts)
    summary = summarize(allocations, districts)
    by_id = {s["district_id"]: s for s in summary}
    assert by_id["d1"]["allocated_households"] == 5
    assert by_id["d2"]["allocated_households"] == 0
    assert by_id["d2"]["detail"] == []


def test_hand_computed_proportional_share():
    # 旧格面积 4*4=16，户数 9；片区按 12:4 面积切分
    # 精确分摊 9*12/16=6.75，9*4/16=2.25 -> 6+2=8，余 1 给余数大的 d1
    old = [OldCell("g1", (0, 0, 4, 4), 9)]
    districts = [
        NewDistrict("d1", (0, 0, 3, 4)),   # 面积 12
        NewDistrict("d2", (3, 0, 4, 4)),   # 面积 4
    ]
    result = {a.district_id: a.households for a in allocate(old, districts)}
    assert result == {"d1": 7, "d2": 2}


def test_largest_remainder_random_conservation():
    """随机权重/总量下最大余数法始终守恒，且每户分摊非负。"""
    rng = random.Random(20261116)
    for _ in range(300):
        n = rng.randint(1, 8)
        weights = [(f"d{i}", rng.randint(0, 20)) for i in range(n)]
        if sum(w for _, w in weights) == 0:
            weights[0] = ("d0", 1)
        total = rng.randint(0, 500)
        shares = largest_remainder_share(weights, total)
        assert sum(shares.values()) == total
        assert all(v >= 0 for v in shares.values())
        # 每个份额不超过按比例的“上取整”上界（最大余数法性质）
        wsum = sum(w for _, w in weights)
        for id_, w in weights:
            if w == 0:
                assert shares[id_] == 0
            else:
                assert shares[id_] <= -(total * w // -wsum)  # 上取整


def test_allocate_random_grid_conservation():
    """随机把整行旧格切给若干片区，逐格与总量都守恒。"""
    rng = random.Random(42)
    for _ in range(50):
        # 用 1x1 的旧格拼成一行，片区为连续不重叠切片，保证完整覆盖
        n_cells = rng.randint(1, 10)
        households = [rng.randint(0, 30) for _ in range(n_cells)]
        old = [
            OldCell(f"g{i}", (i, 0, i + 1, 1), households[i])
            for i in range(n_cells)
        ]
        # 随机切点划分片区
        cuts = {0, n_cells}
        if n_cells > 1:
            for _ in range(2):
                cuts.add(rng.randint(1, n_cells - 1))
        cuts = sorted(cuts)
        districts = [
            NewDistrict(f"d{k}", (cuts[k], 0, cuts[k + 1], 1))
            for k in range(len(cuts) - 1)
        ]
        allocations = allocate(old, districts)
        for cell in old:
            part = [a for a in allocations if a.old_id == cell.id]
            assert sum(a.households for a in part) == cell.households
        assert sum(a.households for a in allocations) == sum(households)
        summary = summarize(allocations, districts)
        assert sum(s["allocated_households"] for s in summary) == sum(households)


def test_max_items_boundary_twelve_allowed():
    """12 个互不重叠条目是允许的（上限边界）。"""
    items = [(f"g{i}", (i, 0, i + 1, 1)) for i in range(12)]
    alloc.assert_no_internal_overlap(items)  # 不抛异常即通过


def test_string_id_tiebreak_lexicographic():
    """非整数编号按字符串升序打破余数并列。"""
    shares = largest_remainder_share([("b", 1), ("a", 1)], 1)
    assert shares == {"b": 0, "a": 1}
