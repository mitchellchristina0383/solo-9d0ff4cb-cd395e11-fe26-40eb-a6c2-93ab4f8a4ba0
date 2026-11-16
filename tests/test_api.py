"""API 集成测试：提交校验、版本快照、生成发布、守恒、交错替换、失败隔离。"""
from __future__ import annotations

import threading

from tests.conftest import poll_generation

OLD_V1 = {
    "items": [
        {"id": "g1", "rect": [0, 0, 10, 10], "households": 7},
        {"id": "g2", "rect": [0, 10, 10, 20], "households": 11},
    ]
}
NEW_V1 = {
    "items": [
        {"id": "d1", "rect": [0, 0, 5, 20]},
        {"id": "d2", "rect": [5, 0, 10, 20]},
    ]
}
# 只覆盖上半部分 -> g2 完整、g1 不完整，生成应失败
NEW_PARTIAL = {"items": [{"id": "dX", "rect": [0, 10, 10, 20]}]}


def _submit(client, path, payload, expected=201):
    r = client.post(path, json=payload)
    assert r.status_code == expected, r.text
    return r.json()


def test_health_and_docs(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # 根路径重定向到 /docs
    assert client.get("/", follow_redirects=False).status_code in (302, 307)


def test_submit_validation_rules(client):
    # 超过 12 个 -> 422（Pydantic 层）
    too_many = {
        "items": [
            {"id": f"g{i}", "rect": [i, 0, i + 1, 1], "households": 1}
            for i in range(13)
        ]
    }
    assert client.post("/old-grids", json=too_many).status_code == 422

    # 退化矩形 -> 422
    bad_rect = {"items": [{"id": "g1", "rect": [0, 0, 0, 5], "households": 1}]}
    assert client.post("/old-grids", json=bad_rect).status_code == 422

    # 负户数 -> 422
    neg = {"items": [{"id": "g1", "rect": [0, 0, 1, 1], "households": -1}]}
    assert client.post("/old-grids", json=neg).status_code == 422

    # 编号重复 -> 400
    dup = {
        "items": [
            {"id": "g1", "rect": [0, 0, 1, 1], "households": 1},
            {"id": "g1", "rect": [2, 0, 3, 1], "households": 1},
        ]
    }
    r = client.post("/old-grids", json=dup)
    assert r.status_code == 400 and "重复" in r.json()["detail"]

    # 同版内部重叠 -> 400
    overlap = {
        "items": [
            {"id": "d1", "rect": [0, 0, 5, 5]},
            {"id": "d2", "rect": [4, 4, 9, 9]},
        ]
    }
    r = client.post("/new-districts", json=overlap)
    assert r.status_code == 400 and "重叠" in r.json()["detail"]

    # 空提交 -> 400
    assert client.post("/new-districts", json={"items": []}).status_code == 400


def test_twelve_items_boundary_accepted(client):
    # 恰好 12 个互不重叠条目 -> 接受
    payload = {
        "items": [
            {"id": f"d{i}", "rect": [i, 0, i + 1, 1]} for i in range(12)
        ]
    }
    r = client.post("/new-districts", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["item_count"] == 12
    # 对象形式的 rect 同样可用
    payload_obj = {
        "items": [
            {
                "id": f"g{i}",
                "rect": {"x1": i, "y1": 0, "x2": i + 1, "y2": 1},
                "households": i,
            }
            for i in range(12)
        ]
    }
    r = client.post("/old-grids", json=payload_obj)
    assert r.status_code == 201, r.text


def test_generation_requires_both_inputs(client):
    r = client.post("/generations")
    assert r.status_code == 409
    _submit(client, "/old-grids", OLD_V1)
    r = client.post("/generations")
    assert r.status_code == 409 and "新片区" in r.json()["detail"]


def test_happy_path_publish_and_conservation(client):
    old_v = _submit(client, "/old-grids", OLD_V1)
    new_v = _submit(client, "/new-districts", NEW_V1)
    assert old_v["version"] == 1 and new_v["version"] == 2  # 版本号全局自增
    assert old_v["items"][0]["area"] == 100

    # 无结果时 404
    assert client.get("/result").status_code == 404

    r = client.post("/generations")
    assert r.status_code == 202
    gen = poll_generation(client, r.json()["id"])
    assert gen["status"] == "succeeded"
    assert gen["old_version"] == old_v["version"]
    assert gen["new_version"] == new_v["version"]

    result = client.get("/result").json()
    assert result["total_households"] == 18
    assert sum(d["allocated_households"] for d in result["districts"]) == 18
    # 结果内附两份输入版本
    assert result["old_version"]["version"] == old_v["version"]
    assert result["new_version"]["version"] == new_v["version"]

    # 手算分摊：每格在 d1/d2 上等面积（各 50）
    # g1=7 -> 4/3（余数并列，d1 先得）；g2=11 -> 6/5
    by_district = {d["district_id"]: d for d in result["districts"]}
    g1_detail = {x["old_id"]: x["households"] for x in by_district["d1"]["detail"]}
    assert g1_detail == {"g1": 4, "g2": 6}
    g2_detail = {x["old_id"]: x["households"] for x in by_district["d2"]["detail"]}
    assert g2_detail == {"g1": 3, "g2": 5}
    assert by_district["d1"]["allocated_households"] == 10
    assert by_district["d2"]["allocated_households"] == 8

    # 版本列表接口
    versions = client.get("/versions").json()
    assert [v["version"] for v in versions] == [old_v["version"], new_v["version"]]
    assert client.get(f"/versions/{old_v['version']}").json()["kind"] == "old"
    assert client.get("/versions/99").status_code == 404


def test_coverage_failure_keeps_old_published_result(client):
    # 先发布一份成功结果
    _submit(client, "/old-grids", OLD_V1)
    _submit(client, "/new-districts", NEW_V1)
    gen1 = poll_generation(client, client.post("/generations").json()["id"])
    assert gen1["status"] == "succeeded"
    good = client.get("/result").json()

    # 再提交一个无法完整覆盖的片区版本并生成 -> 失败
    _submit(client, "/new-districts", NEW_PARTIAL)
    gen2 = poll_generation(client, client.post("/generations").json()["id"])
    assert gen2["status"] == "failed"
    assert "未被新片区完整覆盖" in gen2["error"]

    # 旧发布结果保持可查且内容不变
    still = client.get("/result").json()
    assert still["generation_id"] == good["generation_id"]
    assert still["total_households"] == good["total_households"]
    assert still["districts"] == good["districts"]

    # 修正后再生成 -> 成功并切换
    _submit(client, "/new-districts", NEW_V1)
    gen3 = poll_generation(client, client.post("/generations").json()["id"])
    assert gen3["status"] == "succeeded"
    assert client.get("/result").json()["generation_id"] == gen3["id"]


def test_snapshot_pinned_at_generation_start_with_interleaved_replace(client):
    """生成期间替换输入：任务必须按创建时快照计算，旧结果在发布前保持可查。"""
    old_v1 = _submit(client, "/old-grids", OLD_V1)
    new_v1 = _submit(client, "/new-districts", NEW_V1)

    import app.db as dbmod

    started = threading.Event()
    release = threading.Event()
    original_load = dbmod._load_snapshot

    def blocked_snapshot(conn, gen_id):
        # 快照读取发生在任务被置为 running 之后；此时通知测试线程去替换输入
        started.set()
        assert release.wait(timeout=5), "blocked worker was never released"
        return original_load(conn, gen_id)

    dbmod._load_snapshot = blocked_snapshot
    try:
        r = client.post("/generations")
        gen_id = r.json()["id"]
        assert r.json()["status"] == "pending"
        assert started.wait(timeout=5), "worker did not start"

        # 任务运行中：两份输入都可替换
        old_v2 = _submit(client, "/old-grids", {
            "items": [
                {"id": "h1", "rect": [0, 0, 10, 10], "households": 100},
            ]
        })
        new_v2 = _submit(client, "/new-districts", {
            "items": [{"id": "e1", "rect": [0, 0, 10, 10]}]
        })
        assert old_v2["version"] > old_v1["version"]
        assert new_v2["version"] > new_v1["version"]
        # 生成完成前尚无任何已发布结果
        assert client.get("/result").status_code == 404
        release.set()

        gen = poll_generation(client, gen_id)
    finally:
        dbmod._load_snapshot = original_load
        release.set()

    assert gen["status"] == "succeeded"
    # 固定的是生成开始时的 v1 快照，而不是后来的 v2
    assert gen["old_version"] == old_v1["version"]
    assert gen["new_version"] == new_v1["version"]
    result = client.get("/result").json()
    assert result["old_version"]["version"] == old_v1["version"]
    assert result["new_version"]["version"] == new_v1["version"]
    assert result["total_households"] == 18

    # 再生成一次才使用新快照
    gen_b = poll_generation(client, client.post("/generations").json()["id"])
    assert gen_b["old_version"] == old_v2["version"]
    assert gen_b["new_version"] == new_v2["version"]
    result_b = client.get("/result").json()
    assert result_b["total_households"] == 100
    assert [d["district_id"] for d in result_b["districts"]] == ["e1"]


def test_two_generations_serialize_and_both_keep_snapshots(client):
    old_v1 = _submit(client, "/old-grids", OLD_V1)
    new_v1 = _submit(client, "/new-districts", NEW_V1)
    id1 = client.post("/generations").json()["id"]
    # 立即替换并再次生成：两个任务各自固定自己的快照
    old_v2 = _submit(client, "/old-grids", {
        "items": [{"id": "h1", "rect": [0, 0, 10, 10], "households": 3}]
    })
    new_v2 = _submit(client, "/new-districts", {
        "items": [{"id": "e1", "rect": [0, 0, 10, 10]}]
    })
    id2 = client.post("/generations").json()["id"]
    g1 = poll_generation(client, id1)
    g2 = poll_generation(client, id2)
    assert g1["status"] == g2["status"] == "succeeded"
    assert (g1["old_version"], g1["new_version"]) == (
        old_v1["version"],
        new_v1["version"],
    )
    assert (g2["old_version"], g2["new_version"]) == (
        old_v2["version"],
        new_v2["version"],
    )
    # 最终当前结果来自后完成发布的 g2
    result = client.get("/result").json()
    assert result["generation_id"] == id2
    assert result["total_households"] == 3


def test_immutable_versions_retain_history(client):
    v1 = _submit(client, "/old-grids", OLD_V1)
    v2 = _submit(client, "/old-grids", {
        "items": [{"id": "only", "rect": [0, 0, 1, 1], "households": 1}]
    })
    assert v2["version"] == v1["version"] + 1
    # 历史版本内容不被后续提交改变
    old = client.get(f"/versions/{v1['version']}").json()
    assert [c["id"] for c in old["items"]] == ["g1", "g2"]
    olds = client.get("/versions", params={"kind": "old"}).json()
    assert len(olds) == 2
