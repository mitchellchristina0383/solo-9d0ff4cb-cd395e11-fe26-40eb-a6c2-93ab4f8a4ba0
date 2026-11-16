"""pytest 共享夹具：把数据库指向临时文件，提供隔离的 TestClient。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 在导入应用前指定测试数据库路径
_TMPDIR = tempfile.mkdtemp(prefix="gridtest_")
os.environ["APP_DB_PATH"] = str(Path(_TMPDIR) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.config import DB_PATH  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # 每个用例使用全新的数据库：清掉同目录下所有 SQLite 侧车文件（含 WAL/SHM）
    db_dir = Path(DB_PATH).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    for p in db_dir.glob(Path(DB_PATH).name + "*"):
        p.unlink()
    db.init_db()
    with TestClient(app) as c:
        yield c


def poll_generation(client: TestClient, gen_id: int) -> dict:
    """轮询任务直到进入终态。"""
    for _ in range(200):
        r = client.get(f"/generations/{gen_id}")
        body = r.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        import time

        time.sleep(0.02)
    raise AssertionError("生成任务未在预期时间内结束")
