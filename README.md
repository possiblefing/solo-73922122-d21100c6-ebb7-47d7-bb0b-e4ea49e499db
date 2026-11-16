# 站点目录归一系统 StationHub

面向生态数据工程师的跨机构站点目录归一服务：导入三家机构的站点 CSV，以「同站 / 异站」
判定为依据维护传递分组，每组在同一来源中至多包含一个站点，并以带版本的**统一号**追踪
每次组成员变化。FastAPI + PostgreSQL，单容器化 Compose 一键启动。

## 一键启动

```bash
docker compose up --build
# 启动后：
#   网页界面      http://localhost:8000
#   OpenAPI 文档  http://localhost:8000/docs
#   健康检查      http://localhost:8000/health
```

`demo` 服务会在 API 就绪后自动导入 `demo/stations_demo.csv`。也可以手动跑完整业务剧本：

```bash
./demo/walkthrough.sh http://localhost:8000
```

## 业务规则与实现映射

| 需求 | 实现方式 |
| --- | --- |
| 导入来源、源站号、名称 CSV，每源至多 30 站 | `app/importer.py`，累计计数校验，超限整次回滚 |
| 原始记录不改 | 已存在 `(来源,源站号)` 仅核对，名称不一致直接 409，绝不 UPDATE |
| 初始每站独立成组 | 导入事务内为每个新站建组并签发首个统一号 |
| 同站关系传递成组 | 有效同站边构成无向图，BFS 求连通分量即当前组（`app/service.py`） |
| 每组同源最多一站 | 合并前与重算时双重校验，冲突 409 并列出涉事站点 |
| 异站约束不得被后续合并绕过 | 跨组合并前枚举两组间全部有效异站判定，存在即整次回滚并返回依据 |
| 冲突列出依据 | 409 响应体 `detail.conflicts` 含异站依据 id、站对、说明、时间 |
| 可撤销一条同站依据，按剩余关系重分组 | 依据行保留并置 `revoked`，删除受影响旧组后按剩余边重算 |
| 仍有效的异站判断保留 | 异站表不可撤销，重分组只依赖同站边，异站约束继续拦截后续合并 |
| 成员改变即签发新统一号 | 成员集合变化的组一律作废旧组、建新组、签发新号；成员未变不换号 |
| 旧号固定签发时成员 | `unified_numbers` + `number_members` 为只增快照表，永不改写 |
| 查旧号返回成员现属完整组，拆开列出全部分支 | `GET /api/numbers/{号}` 返回快照 + 全部当前分支，不静默选一组 |
| 并发共同守规则、不丢依据 | 站点行锁 + 组成员行锁（按 id 排序防死锁），单事务提交，死锁整事务重试 |
| 慢组重算不阻塞无关组 | 行锁只覆盖受影响组的成员，互不相交的组可并行确认 |
| 查询只见完整提交状态 | READ COMMITTED + 全部写操作单事务，失败整次回滚 |
| 重启保留关系及旧号含义 | 全部状态在 PostgreSQL，无内存态；启动仅 `create_all` 补表 |

## 数据模型

```
sources ──< stations ──< group_members >── current_groups ──< unified_numbers
                │                            (当前物化组)          │
                ├──< same_evidences (可撤销，撤销行保留)            └──< number_members (签发快照，只增)
                └──< diff_evidences (永久有效)
```

- **same_evidences**：站对规范化为 `a_id < b_id`，每对唯一；撤销后重新确认复用同一行。
- **diff_evidences**：永久有效；同组内判异站会被拒绝并返回当前同站传递链，提示先撤销哪条依据。
- **current_groups / group_members**：当前组划分的物化结果，每个站点任一时刻恰属于一个组。
- **unified_numbers**：号段 `U-000001` 起，序号取自表主键，保证全局唯一、严格递增。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/import` | multipart 上传 CSV（`source,station_code,name`，表头支持中文） |
| POST | `/api/evidences/same` | 确认两站同站 `{station_a_id, station_b_id, note?}` |
| POST | `/api/evidences/diff` | 判两站异站 |
| POST | `/api/evidences/same/revoke` | 撤销一条同站依据并重分组 `{evidence_id, note?}` |
| GET | `/api/groups` `/api/groups/{id}` | 当前组与成员 |
| GET | `/api/stations` `?source_code=` | 站点及当前号 |
| GET | `/api/evidences?include_revoked=true` | 全部同站/异站依据 |
| GET | `/api/numbers/{number}` | 按号查询：固定快照 + 现属全部分支 |

所有错误返回 `409/400/404`，响应体 `detail.message` 说明原因，`detail.conflicts` 列出依据。

## 本地测试

```bash
pip install -r requirements-dev.txt
# 默认连 postgresql+psycopg2://postgres@127.0.0.1:5433/stationhub
# 可用 TEST_DATABASE_URL 覆盖；连不上 PG 时自动回退 SQLite（并发用例自动跳过）
pytest -q
```

测试覆盖（23 个用例）：导入上限与不可变性、传递合并、同源/异站冲突、撤销重分组、
旧号快照与拆组分支、失败原子性、真实 PostgreSQL 多线程并发（互不相交组并行、
异站与合并竞速、撤销与无关合并竞速）。

## 目录结构

```
app/
  config.py            配置（DATABASE_URL、每源上限）
  db.py                引擎/会话，请求级事务提交回滚
  models.py            ORM 模型与约束
  schemas.py           Pydantic 出入参
  errors.py            领域异常
  importer.py          CSV 导入与初始成组
  service.py           同站/异站/撤销/重分组/统一号签发（核心规则）
  queries.py           只读查询与旧号分支展开
  retry.py             死锁整事务重试
  routers.py main.py   HTTP 路由与应用入口
  web/index.html       内嵌网页（无前端构建依赖）
demo/                  演示 CSV 与 walkthrough 剧本
tests/                 pytest 套件（含 PG 并发测试）
```
