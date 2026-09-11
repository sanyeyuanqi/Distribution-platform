# SpaceX Hub 适配

核对日期：2026-09-10。适配器标识：`spacex-hub-v1`，限定站点 `https://api.token-spacex.com`。站点隐藏发行版本，界面显示已核对的构建信息，不冒充某个 New API 发行版。

## 账号与接入

使用系统令牌和 `/api/user/self` 返回的顶层 `id`，不是 `admin_hub.id`、供应商编号或旧账号编号。供应商主账号与用户子账号共用 Hub 管理接口，但上传需要用户子账号及明确的 `channel_write` 权限。标准 `/api/channel/` 的 403 不代表 Hub 令牌无效，也不会触发猜测其他写入接口。

身份验证检查角色、账号状态、所属供应商、可见下游站点和公开构建。本地只支持单一下游站点；分发模板必须通过能力检查后才能启用站点分发。

## 接口合同

| 功能 | 已核对接口 |
| --- | --- |
| 身份、公开状态 | `GET /api/user/self`、`GET /api/status` |
| 可见站点 | `GET /api/admin-hub/usage-logs/sites` |
| 平台类型 | `GET /api/admin-hub/channels/platform-types` |
| 分组、发布设置 | `GET /api/admin-hub/sites/:id/groups`、`GET /api/admin-hub/site-publish-fields/:id` |
| 渠道列表、详情 | `GET /api/admin-hub/channels`、`GET /api/admin-hub/channels/:id` |
| 创建 | `POST /api/admin-hub/channels` |
| 发布结果查询 | `POST /api/admin-hub/channels/publish-status`，只读批量查询 |
| 启停 | `POST /api/admin-hub/channels/:id/status`，指定下游 `site_id` |
| 测试、用量 | `POST /api/admin-hub/channels/:id/test`、`GET /api/admin-hub/channels/:id/realtime-used-quota` |

创建请求使用 `platform_channel_type`、`type`、`model_series`、`key`、模型数组、`siteIds` 等 Hub 字段，不发送标准 New API 的 `mode/channel` 包装。创建后异步发布，必须核实下游映射及发布状态；未知结果保留待核实，不重复创建或自动强制重新发布。

每条远端渠道只支持一个 Key。批量上传逐条建立分发，不伪装成多 Key 容器。分区身份与原始条目顺序关联，不随重新验证或令牌刷新变化。

## 明确边界

- 发布初始状态来自下游 `site_defaults.create_status`。模板状态必须与其一致；不会悄悄把要求停用的模板改为启用。
- 远端 GCP Claude 的类型为 42，与本地 Vertex Claude 的类型 41 不同，本轮拒绝该服务；不能借 Gemini 的 JSON 格式能力绕过检查。
- 未核对的认证方式和模板字段在提交前提示不支持，不静默丢弃配置。
- 不支持原地替换 Key、删除 Hub 渠道、跨多个下游映射的写操作。未发现的接口不按标准 New API 猜测。
- Hub 详情中的远端状态是缓存。归档仍要求可靠的远端停用证据，不能仅凭缓存放行。
- 原始用量与结算事实分离。缺少已核实换算规则时不凭固定比例生成美元消耗。

## 公开构建证据

匿名获取以下公开资源，运行时核对脚本入口、大小及 SHA-256。构建或账号权限改变时停止旧任务写入，要求重新验证。

| 资源 | SHA-256 |
| --- | --- |
| `/static/js/index.c3891c5ce3.js` | `48b7afdb3aa44c7bbe274231b774564ed155f68d57be9b8be832c315bca1fe87` |
| `/static/js/async/5457.0c75bd0410.js` | `d5e9482af05b5e94990e193d37b5f6ee91e3f519d3d9cc3af6524406afa0bc63` |
| `/static/js/async/2438.57a4fda8d6.js` | `0d094ab57be112830b4b7ee986b21cc8f806fefab337ec810c3c119a24127946` |

入口脚本包含 Hub 路由与分块对应关系，5457 包含 API 方法，2438 包含渠道表单、发布状态及账号操作条件。公开客户端用于核对接口合同，不代表已审计远端服务端实现。

验证使用 MockTransport 测试创建、状态查询、权限和不确定结果；真实站点仅核验身份、构建、元数据与读取。未上传测试密钥或触发计费推理。
