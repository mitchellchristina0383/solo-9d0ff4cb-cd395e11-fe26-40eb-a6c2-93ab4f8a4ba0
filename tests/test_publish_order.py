"""交错完成与失败情形的发布顺序测试。

模拟“两个服务实例共用同一 SQLite”：直接调用 db.run_generation
（与后台工作线程同一入口，各自打开独立连接），以可控顺序/并发
驱动两个任务的完成，验证 /result 的发布顺序规则：

- /result 始终指向已成功任务中编号最新的一项；
- 后完成的旧任务保留自己的成功明细，但不覆盖更新任务的发布；
- 编号更新的任务失败时，成功的旧任务仍可发布；
- 发布事务中途失败不留虚假成功状态，也不损坏原发布结果。
"""
from __future__ import annotations

import threading

from app import db

OLD_V1 = {"items": [{"id": "g1", "rect": [0, 0, 10, 10], "households": 7}]}
OLD_V2 = {"items": [{"id": "g1", "rect": [0, 0, 10, 10], "households": 50}]}
NEW_V1 = {"items": [{"id": "d1", "rect": [0, 0, 10, 10]}]}
# 只覆盖旧格左半 -> 覆盖校验必失败
NEW_PARTIAL = {"items": [{"id": "dX", "rect": [0, 0, 5, 10]}]}


def _submit(client, path, payload):
    r = client.post(path, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _create_generation_direct() -> int:
    """不走 API/执行池，直接在库中创建任务（快照规则与 POST /generations 相同）。

    这样测试可以精确控制每个任务何时由“某个实例的工作者”执行。
    """
    conn = db.get_conn()
    try:
        with conn:
            return db.create_generation(conn)
    finally:
        conn.close()


def _parts_of(gen_id: int) -> list[dict]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM result_parts WHERE generation_id=? ORDER BY id", (gen_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def test_interleaved_completion_keeps_newest_published(client):
    """题目场景：甲按旧户数创建，修订后创建乙；乙先完成发布，甲后完成不得退回。"""
    _submit(client, "/old-grids", OLD_V1)
    _submit(client, "/new-districts", NEW_V1)
    gen_a = _create_generation_direct()  # 任务甲：固定 7 户快照
    _submit(client, "/old-grids", OLD_V2)  # 统计员修订户数
    gen_b = _create_generation_direct()  # 任务乙：固定 50 户快照
    assert gen_b > gen_a

    # 乙先完成并发布（另一个服务实例）
    db.run_generation(gen_b)
    res = client.get("/result").json()
    assert res["generation_id"] == gen_b
    assert res["total_households"] == 50

    # 甲随后完成：自身仍成功、明细保留，但 /result 不得退回旧户数
    db.run_generation(gen_a)
    assert client.get(f"/generations/{gen_a}").json()["status"] == "succeeded"
    assert client.get(f"/generations/{gen_b}").json()["status"] == "succeeded"
    res2 = client.get("/result").json()
    assert res2["generation_id"] == gen_b
    assert res2["total_households"] == 50
    assert res2["published_at"] == res["published_at"]  # 发布时刻未被旧任务刷新

    # 甲的成功明细完整保留在库中（7 户全部分给 d1）
    parts_a = _parts_of(gen_a)
    assert len(parts_a) == 1
    assert parts_a[0]["district_id"] == "d1"
    assert parts_a[0]["households"] == 7


def test_newer_generation_failure_still_lets_older_publish(client):
    """乙生成失败时，后完成的甲仍可正常发布。"""
    _submit(client, "/old-grids", OLD_V1)
    _submit(client, "/new-districts", NEW_V1)
    gen_a = _create_generation_direct()
    _submit(client, "/new-districts", NEW_PARTIAL)  # 乙的快照无法完整覆盖
    gen_b = _create_generation_direct()

    # 乙先完成（失败），甲后完成（成功）
    db.run_generation(gen_b)
    assert client.get("/result").status_code == 404  # 失败不发布
    db.run_generation(gen_a)

    assert client.get(f"/generations/{gen_b}").json()["status"] == "failed"
    assert "未被新片区完整覆盖" in client.get(f"/generations/{gen_b}").json()["error"]
    assert client.get(f"/generations/{gen_a}").json()["status"] == "succeeded"
    res = client.get("/result").json()
    assert res["generation_id"] == gen_a
    assert res["total_households"] == 7


def test_publish_failure_rolls_back_without_false_success(client):
    """发布事务中途失败：任务落 failed、明细回滚、原发布结果完好、可恢复。"""
    _submit(client, "/old-grids", OLD_V1)
    _submit(client, "/new-districts", NEW_V1)
    gen1 = _create_generation_direct()
    db.run_generation(gen1)
    good = client.get("/result").json()
    assert good["generation_id"] == gen1

    # 注入数据库级故障：禁止写入 current_result（模拟发布中途崩溃）
    conn = db.get_conn()
    try:
        conn.execute(
            "CREATE TRIGGER fail_publish BEFORE INSERT ON current_result "
            "BEGIN SELECT RAISE(ABORT, 'injected publish failure'); END"
        )
        conn.commit()
    finally:
        conn.close()

    _submit(client, "/old-grids", OLD_V2)
    gen2 = _create_generation_direct()
    db.run_generation(gen2)

    # 不得留下虚假的成功状态
    g2 = client.get(f"/generations/{gen2}").json()
    assert g2["status"] == "failed"
    assert "injected publish failure" in g2["error"]
    # 原发布结果完好无损
    assert client.get("/result").json() == good
    # 失败任务的明细不得残留（事务已整体回滚）
    assert _parts_of(gen2) == []

    # 解除故障后再生成：成功发布并推进到最新任务
    conn = db.get_conn()
    try:
        conn.execute("DROP TRIGGER fail_publish")
        conn.commit()
    finally:
        conn.close()
    gen3 = _create_generation_direct()
    db.run_generation(gen3)
    assert client.get(f"/generations/{gen3}").json()["status"] == "succeeded"
    res = client.get("/result").json()
    assert res["generation_id"] == gen3
    assert res["total_households"] == 50


def test_concurrent_workers_always_publish_newest(client):
    """两个工作者线程（模拟两个实例）并发完成，/result 始终是编号最新者。"""
    _submit(client, "/new-districts", NEW_V1)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(gen_id: int) -> None:
        try:
            barrier.wait(timeout=5)
            db.run_generation(gen_id)
        except BaseException as exc:  # pragma: no cover - 失败时由断言报告
            errors.append(exc)

    for round_no in range(5):
        _submit(client, "/old-grids", {
            "items": [{"id": "g1", "rect": [0, 0, 10, 10], "households": round_no + 1}]
        })
        gen_a = _create_generation_direct()
        _submit(client, "/old-grids", {
            "items": [
                {"id": "g1", "rect": [0, 0, 10, 10], "households": 100 + round_no}
            ]
        })
        gen_b = _create_generation_direct()

        t1 = threading.Thread(target=worker, args=(gen_a,))
        t2 = threading.Thread(target=worker, args=(gen_b,))
        t1.start()
        t2.start()
        t1.join(15)
        t2.join(15)
        assert not t1.is_alive() and not t2.is_alive(), "工作者线程超时"
        assert not errors, f"工作者线程异常: {errors}"
        assert client.get(f"/generations/{gen_a}").json()["status"] == "succeeded"
        assert client.get(f"/generations/{gen_b}").json()["status"] == "succeeded"
        res = client.get("/result").json()
        assert res["generation_id"] == gen_b
        assert res["total_households"] == 100 + round_no
