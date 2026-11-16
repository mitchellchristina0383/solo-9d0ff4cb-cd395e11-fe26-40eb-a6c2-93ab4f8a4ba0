"""运行期配置。"""
from __future__ import annotations

import os
from pathlib import Path

# SQLite 数据库文件位置；容器内默认 /data/app.db，本地默认项目根 .data/app.db
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / ".data" / "app.db"
DB_PATH = Path(os.environ.get("APP_DB_PATH", str(DEFAULT_DB_PATH)))

MAX_ITEMS_PER_SUBMISSION = 12

# 生成任务工作线程数固定为 1：
# 保证生成按提交顺序串行发布，且同一时刻只有一个生成事务在写库。
WORKER_THREADS = 1
