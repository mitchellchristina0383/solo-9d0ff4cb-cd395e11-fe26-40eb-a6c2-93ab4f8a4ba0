# 城市统计片区重划分摊服务

把旧网格（带整数户数的整数矩形）按矩形重叠面积，均匀估算分摊到新的统计片区，
用于跨年对比。FastAPI + SQLite 实现，支持整版版本提交、异步生成、快照隔离与原子发布。

## 业务规则

- 矩形为整数轴对齐矩形 `[x1, y1, x2, y2]`，要求 `x1 < x2`、`y1 < y2`，面积 `(x2-x1)*(y2-y1)`。
- 每次**整版提交**旧网格或新片区，每版最多 **12** 个条目；同版条目编号唯一、矩形**内部不得重叠**（边界相接允许）。
- 生成结果时，**任一旧格未被新片区完整覆盖则拒绝**该次生成；片区可以延伸到旧网格之外。
- 通过覆盖校验后，**逐格**按重叠面积分摊该旧格的整数户数：
  - 先按面积比例取整；
  - 余数用**最大余数法**分配，余数相同时按**片区编号升序**（可解析为整数的编号按数值升序，否则按字符串升序）。
- 分摊结果**总户数守恒**，并带守恒护栏；输出片区汇总及两份输入版本。
- 生成在后台串行执行。生成任务在创建的同一事务内**固定当时最新的 old/new 版本快照**，
  生成期间仍可替换任一输入（产生新版本），不影响进行中的任务。
- 当前结果在单事务内原子切换；新结果**完整发布前**，`/result` 始终返回上一份已发布结果；
  生成失败只标记任务，**不改变**已发布结果。

## 目录结构

```
app/
  config.py       运行配置（DB 路径、每版上限）
  models.py       Pydantic 请求/响应模型
  allocation.py   纯函数：几何、覆盖校验、最大余数法分摊、汇总
  db.py           SQLite 持久化、版本快照、后台生成与原子发布
  main.py         FastAPI 路由与单线程后台工作池
tests/            pytest 单元 + 集成测试（19 个用例）
Dockerfile
docker-compose.yml
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

- 服务：<http://localhost:8000>
- 交互式文档（可直接演示提交与生成）：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- SQLite 数据持久化在命名卷 `grid_data`（容器内 `/data/app.db`）。

## 本地运行（无 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可通过环境变量指定数据库：APP_DB_PATH=/path/to/app.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/old-grids` | 整版提交旧网格（编号、矩形、户数），产生不可变版本 |
| POST | `/new-districts` | 整版提交新片区（编号、矩形），产生不可变版本 |
| GET | `/versions?kind=old\|new` | 列出输入版本（历史版本保留） |
| GET | `/versions/{id}` | 查询单个版本 |
| POST | `/generations` | 开始生成（202），同步固定版本快照，后台计算 |
| GET | `/generations` | 列出全部生成任务 |
| GET | `/generations/{id}` | 查询任务状态：pending/running/succeeded/failed |
| GET | `/result` | 当前已发布结果（片区汇总 + 两份输入版本），无结果时 404 |
| GET | `/health` | 健康检查 |

## /docs 演示流程

1. `POST /old-grids` 提交旧网格；
2. `POST /new-districts` 提交新片区；
3. `POST /generations` 触发生成，记录返回的任务 `id`；
4. 轮询 `GET /generations/{id}` 直到 `succeeded`（覆盖不足时为 `failed`，`error` 给出原因）；
5. `GET /result` 查看片区汇总、逐格分摊明细与两份输入版本。

### 请求示例

```json
// POST /old-grids
{
  "items": [
    {"id": "g1", "rect": [0, 0, 10, 10], "households": 7},
    {"id": "g2", "rect": [0, 10, 10, 20], "households": 11}
  ]
}
```

```json
// POST /new-districts（rect 也可写成 {"x1":..,"y1":..,"x2":..,"y2":..} 对象）
{
  "items": [
    {"id": "d1", "rect": [0, 0, 5, 20]},
    {"id": "d2", "rect": [5, 0, 10, 20]}
  ]
}
```

上例两个旧格在 d1/d2 上均为等面积（各 50%）：
`g1=7` 拆成 `4/3`、`g2=11` 拆成 `6/5`（余数并列时编号小的 d1 先得 +1），
片区汇总 `d1=10`、`d2=8`，总计 18，与输入总户数一致。

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -v
```

覆盖要点：矩形几何与重叠、整版数量/编号/内部重叠校验、覆盖不足拒绝、
最大余数法与编号并列规则、零户数与多格守恒、片区延伸/零分摊片区、
快照固定与生成期间交错替换、失败不影响旧发布结果、串行任务各自快照、版本不可变。
