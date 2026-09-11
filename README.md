# KeyAcross 多平台上 Key 管理系统

前后端分离的渠道分发与结算系统：React + TypeScript + FastAPI + PostgreSQL + Redis。业务接口由 FastAPI 独立提供；React 通过会话 Cookie 与 CSRF Token 调用接口；后台分发和同步由独立 Worker 执行。当前范围见 [保留功能与数据库结构](docs/CORE_FUNCTIONS.md)。原始需求和账号材料仅保留在本地，不随代码发布。

## 已实现

- **RBAC 和固定归属**：仅超管可以创建账号，创建时选择管理员或普通用户，无需选择所属管理员；新账号直接归属创建它的超管。历史管理员与其直属用户的归属和管理权限保持不变，管理员不能创建账号。创建后角色与归属不可修改。用户、渠道、任务、密钥、统计、结算及审计均在服务端验证范围。
- **独立登录**：Argon2 密码哈希、Redis 会话、闲置/最长有效期、退出、失败限频、密码重置/停用后会话失效、CSRF 校验。
- **站点与固定接入规则**：令牌加密、只读连接验证、能力展示、启用与采集分开、手输站点名确认归档。七类渠道及凭据解析规则由系统内置，不再提供“分类与格式”管理入口。Azure 分为 GPT 和 Claude，上传资源名及密钥后自动生成官方地址；历史渠道与密钥保留原解析定义。
- **密钥上传与分发**：单条/批量、先绑定备注/代理再去重、行号与冲突预览、现有 Key 补分发、稳定分组标签、逐站点子任务、提交幂等、失败重试、取消、未知结果核实。
- **分发模板**：超管配置目标站点、接入类型、名称、创建后渠道状态、接收模型、目标分组和默认备注。凭据规则、官方地址和其他渠道默认参数由系统生成。上传支持单密钥 / 批量、JSON 凭据、模型范围、RPM 保护、号况及逐行备注和代理；密钥及代理加密保存，远端参数回读验证。使用方式见 [模板与简化上传](docs/UPLOAD_TEMPLATES.md)。
- **渠道管理**：标签分组与列表详情查询、站点筛选与详情、完整 Key 受控读取、密钥版本、备注/模型修改、远端启停、补分发、归档及取消归档、明确确认远端删除。
- **消耗与结算**：按授权范围汇总各站点渠道消耗，保留精确金额、远端目标去重及缺失数据状态；历史事实支持幂等导入、分类/趋势/覆盖统计和 CSV 导出。在用户管理设置分类汇率、选择分组结算，保存订单及明细中的当时汇率、结算消耗和金额；首次结算后使用逐远端目标基线计算增量，防止重复结算。0% 可生成零金额订单，历史订单不可改写。用户记录弹窗和结算历史共用订单数据；不提供独立付款登记或转账。详见 [结算订单](docs/SETTLEMENT_ORDERS.md)。
- **运营页面**：控制台、公告中英文及已读版本、模型覆盖缺口、任务、审计、用户资料、中文/英文切换。
- **功能权限**：管理员不开放公告中心、任务中心和操作审计；普通用户保留公告及本人任务，操作审计仅超级管理员可访问。管理员仍可上传密钥，渠道同步统一由后台定时执行。
- **系统设置**：任务中心和操作审计统一从“系统设置”进入，按原有权限显示；旧页面地址仍可自动跳转。
- **运行交付**：数据库迁移、Docker Compose、初始化、加密备份/恢复工具和自动化测试。

## 首次启动

安装 Docker Desktop（Linux 容器模式）、Python 3.12+。完整容器部署不需要本机 Node；本地前端开发使用 Node 22+。

```powershell
python scripts/setup.py
docker compose up -d --build
```

初始化程序要求自行设置首个超管账号和 6–256 位密码，并生成独立随机密钥。账号创建、重置密码和个人修改密码使用相同的长度要求。没有固定默认密码。当前工作目录首次开发运行已生成私有 `.env`，不会重复覆盖；初始账号和密码位于其中的 `BOOTSTRAP_USERNAME`、`BOOTSTRAP_PASSWORD`。

打开 [管理界面](http://localhost:8080)；开发时访问 [本地 API 文档](http://127.0.0.1:8000/docs)。容器默认只反向代理 `/api`，不公开 `/docs`。接口健康检查为 `/api/health`。

初次进入后：超管创建管理员与普通用户 → 配置并验证站点 → 配置分发模板的模型及参数 → 超级管理员、管理员或普通用户上传密钥 → 等待后台同步后查看渠道数据 → 在用户管理设置分类汇率 → 选择符合条件的分组，预览并记录结算 → 查看用户结算记录或结算历史。

同步默认约每 5 分钟调度，已有自动任务未完成时不重叠创建。页面“刷新”只读取本地数据；结算不会先触发同步，使用最近成功保存且完整有效的快照，缺失数据不当作零。范围、失败重试和周期说明见 [后台自动同步](docs/AUTOMATIC_SYNC.md)。

默认仅绑定 `127.0.0.1:8080`。正式服务器应配置 HTTPS 反向代理、准确的 `CORS_ORIGINS`，并将 `COOKIE_SECURE=true`。PostgreSQL 和 Redis 默认不对外开放。`.env`、附件、备份不应提交代码仓库。以 `distribute` 为容器前缀的部署方式见 [Docker 生产部署](docs/PRODUCTION_DEPLOYMENT.md)。

## 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
docker compose -f compose.yaml -f compose.dev.yaml up -d postgres redis
cd backend
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m app.bootstrap
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端运行 Worker：在 `backend` 目录执行 `..\.venv\Scripts\python.exe -m app.worker`。再在 `frontend` 目录执行 `npm ci`、`npm run dev`。Vite 将 `/api` 代理至 FastAPI，页面默认位于 `http://localhost:5173`。

PostgreSQL 开发端口 `55432`，Redis 开发端口 `56379`，仅绑定本机；请使用 `.env` 内生成的连接串。本项目不使用 SQLite 替代生产数据库；SQLite 仅用于一部分快速单元测试。

## 接入边界：必须按实际材料配置

V2 明确指出接口材料不完整，以下状态在页面和服务端均有约束：

| 功能 | 当前状态 |
|---|---|
| Silicon 卖家身份、模型/路由组、创建/查询/修改/启停/删除 | 已按本地文档实现适配及模拟契约测试 |
| TCP Red / Colin 供应商身份、渠道分组、模型、渠道管理 | 独立 `tcp-red-v1` 适配器；身份/权限/列表/模型/分组已实测读取，写操作有契约测试；见 [接入说明](docs/TCP_RED.md) |
| NewAPI 官方近期版本的渠道管理与密钥分发 | 已核对 2026-06-08 至 2026-09-08 的全部 26 个发布标签，并保留 v0.13.2；自动选择版本对应的权限、启停接口及类型能力，逐版本模拟契约测试通过；见 [兼容清单](docs/NEWAPI_COMPATIBILITY.md) |
| OpenAI API Key v1，远端 type=1 | 有明确凭据与字段映射，可选已验证站点和明确模型后使用 |
| AWS / Anthropic / Azure / Google / OpenRouter / OpenCode | 使用内置凭据规则和管理员配置的接收模板。Bedrock 支持同批 AK/SK 与 API 密钥；AWS Claude 代理使用密钥和实际 api.aws 地址；Azure GPT / Claude 从资源名生成官方地址。上传入口按已启用模板和站点能力开放；见 [模板说明](docs/UPLOAD_TEMPLATES.md) |
| 远端自动测试并启用 | Silicon 文档缺少契约；Colin 接口已识别但尚未接入持久任务流程，均标记未支持；可停用创建后人工验证并显式启用 |
| 卖家消耗采集 / TPM / 上游 RPM 保护 / 代理字段 | 未定义的接口标记未支持，不虚构金额、覆盖率或已生效保护 |
| 统计事实导入 | 超管可通过 `/api/usage/import` 导入有来源编号、归属、单位换算版本及核实依据的事实；非实时采集替代品 |
| 当前结算 | 使用已同步且完整、身份匹配的 USD 渠道消耗及分类百分比计算 USDT 金额；记录完成结算，不发起转账 |
| 结算基线 | 逐远端目标证据保留在订单明细 JSON；重复、计数回退和换算变化由后端校验。旧 `/api/usage/samples` 已退休 |
| 历史订单 | 保存当时的汇率、消耗、金额和收款方；没有修改、删除或旧账单付款/调整接口 |

新增站点不会自动复制历史 Key；使用渠道补分发或明确的任务入口。站点被归档后，新写任务停止，历史数据保留。未经确认的请求结果必须先核实，不自动重复创建。

## 测试与验收

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest
```

真实 PostgreSQL 验证（使用独立测试数据库，不操作业务数据库）：

```powershell
docker compose exec -T postgres createdb -U keyacross keyacross_test
.\.venv\Scripts\python.exe scripts/test_postgres.py
cd frontend
npm run build
```

早期版本的验收项与外部依赖见 [历史验收记录](docs/ACCEPTANCE.md)；其中旧账务流程不再代表当前接口。当前功能和空表迁移保护见 [功能范围](docs/CORE_FUNCTIONS.md)。生产部署前还需要实际卖家权限/数据契约联调；20 站点、5,000 渠道、20 人并发是需求目标，尚未进行容量验收，不视为已通过压测。

## 备份与恢复

```powershell
.\.venv\Scripts\python.exe scripts/backup.py
.\.venv\Scripts\python.exe scripts/restore.py .local/backups/某个备份.kaenc
```

备份包含 PostgreSQL 自定义格式转储，并兼容保留旧私有附件目录，整体加密；主密钥不在备份内。必须将原 `ENCRYPTION_KEY` 单独安全保存。恢复创建新的 `keyacross_restore_*` 数据库和独立附件目录，不覆盖正在运行的数据库。验证账号归属、凭据解密、结算订单及任务记录后，再自行切换连接配置。升级旧备份前检查其迁移版本；退休的 9 张旧表中任意一张非空时，新迁移会拒绝删除。

建议每日备份并定期恢复演练。恢复后的 Redis 不需要保留登录会话；用户重新登录即可。Worker 依照 PostgreSQL 任务状态恢复，未知创建结果仍只查询核实。

## 代码导航

`frontend/src` 为 React 页面；`backend/app/routers` 为独立 API；`app/auth.py` 为 RBAC 与会话；`app/channel_service.py`、`app/worker.py` 为异步业务；`app/adapters` 为平台适配契约及工厂；`app/settlement_orders.py` 为当前结算订单，`app/billing.py` 保留共同汇率/授权工具；`backend/migrations` 为数据库版本；`backend/tests` 为自动化验证。

服务端不读取项目内的账号密码表，也不会自动把该表导入系统。原需求文档保留其设计内容，并标注为历史资料。
