"""纯函数计算模块：矩形几何、覆盖校验、最大余数法分摊。

本模块不触碰数据库与 Web 层，便于单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# 矩形表示为 (x1, y1, x2, y2)，均为整数且 x1<x2、y1<y2。
RectTuple = tuple[int, int, int, int]


@dataclass(frozen=True)
class OldCell:
    id: str
    rect: RectTuple
    households: int


@dataclass(frozen=True)
class NewDistrict:
    id: str
    rect: RectTuple


def rect_area(rect: RectTuple) -> int:
    x1, y1, x2, y2 = rect
    return (x2 - x1) * (y2 - y1)


def overlap_area(a: RectTuple, b: RectTuple) -> int:
    """两个轴对齐矩形的重叠面积（不相交返回 0）。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    w = min(ax2, bx2) - max(ax1, bx1)
    h = min(ay2, by2) - max(ay1, by1)
    if w <= 0 or h <= 0:
        return 0
    return w * h


def assert_no_internal_overlap(rects: list[tuple[str, RectTuple]]) -> None:
    """同版条目内部不得重叠（边界相接允许）。

    rects: (编号, 矩形) 列表，保持提交顺序用于报错信息。
    """
    for i in range(len(rects)):
        id_a, rect_a = rects[i]
        for j in range(i + 1, len(rects)):
            id_b, rect_b = rects[j]
            if overlap_area(rect_a, rect_b) > 0:
                raise ValueError(f"条目 {id_a} 与 {id_b} 的矩形存在重叠区域")


def assert_full_coverage(old_cells: list[OldCell], districts: list[NewDistrict]) -> None:
    """任一旧格未被片区完整覆盖则拒绝。

    新片区之间互不重叠，因此对每个旧格累加与所有片区的重叠面积，
    必须恰好等于旧格自身面积。片区可以延伸到旧网格之外。
    """
    for cell in old_cells:
        covered = sum(overlap_area(cell.rect, d.rect) for d in districts)
        if covered != rect_area(cell.rect):
            raise ValueError(
                f"旧格 {cell.id} 未被新片区完整覆盖："
                f"面积 {rect_area(cell.rect)}，被覆盖 {covered}"
            )


def _id_sort_key(id_: str):
    """片区编号升序：可解析为整数的按数值升序，否则按字符串升序。"""
    try:
        return (0, int(id_), "")
    except ValueError:
        return (1, 0, id_)


def largest_remainder_share(weights: list[tuple[str, int]], total: int) -> dict[str, int]:
    """最大余数法把整数 total 按权重分摊。

    weights: (编号, 权重/重叠面积) 列表，权重均非负且至少一个为正。
    余数相同时按片区编号升序（_id_sort_key）决定优先获得 +1。
    返回 编号 -> 分摊户数，守恒于 total。
    """
    weight_sum = sum(w for _, w in weights)
    if weight_sum <= 0:
        raise ValueError("权重之和必须为正")

    floors: dict[str, int] = {}
    # 用精确分数表示余数，避免浮点误差导致“余数并列”判定不确定
    remainders: dict[str, Fraction] = {}
    for id_, w in weights:
        exact = Fraction(total * w, weight_sum)
        floors[id_] = exact.numerator // exact.denominator  # 精确向下取整
        remainders[id_] = exact - floors[id_]

    allocated = sum(floors.values())
    leftover = total - allocated
    # 余数降序；余数相同按编号升序
    order = sorted(weights, key=lambda pair: (-remainders[pair[0]], _id_sort_key(pair[0])))
    for id_, _ in order:
        if leftover == 0:
            break
        floors[id_] += 1
        leftover -= 1
    assert leftover == 0
    return floors


@dataclass(frozen=True)
class Allocation:
    district_id: str
    old_id: str
    overlap_area: int
    households: int


def allocate(
    old_cells: list[OldCell], districts: list[NewDistrict]
) -> list[Allocation]:
    """按重叠面积逐格分摊户数。

    每个旧格独立分摊其整数户数到与其相交的片区：
    先按面积比例取整，再用最大余数法分配余数，
    余数并列时按片区编号升序。所有旧格户数之和在结果中守恒。
    """
    assert_full_coverage(old_cells, districts)
    results: list[Allocation] = []

    for cell in old_cells:
        weights = [
            (d.id, area)
            for d in districts
            if (area := overlap_area(cell.rect, d.rect)) > 0
        ]
        if not weights:
            # 覆盖校验已排除这种情况，保留防御性分支
            raise ValueError(f"旧格 {cell.id} 没有任何重叠片区")
        shares = largest_remainder_share(weights, cell.households)
        # 明细按片区编号升序输出，结果稳定
        for district_id in sorted(shares, key=_id_sort_key):
            area = dict(weights)[district_id]
            results.append(
                Allocation(
                    district_id=district_id,
                    old_id=cell.id,
                    overlap_area=area,
                    households=shares[district_id],
                )
            )
    return results


def summarize(
    allocations: list[Allocation], districts: list[NewDistrict]
) -> list[dict]:
    """汇总每个片区（含零分摊片区），按编号升序。"""
    by_district: dict[str, list[Allocation]] = {d.id: [] for d in districts}
    for a in allocations:
        by_district[a.district_id].append(a)

    summary = []
    for d in sorted(districts, key=lambda x: _id_sort_key(x.id)):
        items = by_district[d.id]
        summary.append(
            {
                "district_id": d.id,
                "allocated_households": sum(a.households for a in items),
                "detail": [
                    {
                        "district_id": a.district_id,
                        "old_id": a.old_id,
                        "overlap_area": a.overlap_area,
                        "households": a.households,
                    }
                    for a in sorted(items, key=lambda a: _id_sort_key(a.old_id))
                ],
            }
        )
    return summary
