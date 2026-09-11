# NewAPI 近期版本兼容范围

隐藏版本号的三野 / 连界 API 使用独立的[已核对构建适配](SSSVIP_COMPATIBILITY.md)，不归入下列官方发行版清单。

核对日期：**2026-09-08**。本次将“最近几个月”落为 **2026-06-08 至 2026-09-08** 的三个月窗口，覆盖该窗口内官方发布的全部 **26 个标签**，并继续兼容此前已支持的稳定版 **v0.13.2**，共 27 个明确版本。

版本来自 [QuantumNous/new-api 官方发布列表](https://github.com/QuantumNous/new-api/releases)，包括 `v1.0.0-rc.11` 至 `v1.0.0-rc.35` 和 `v1.0.0-rc.19-i18nfix.2`。窗口中没有其他稳定版、alpha 或 nightly 发布标签。GitHub 给这些 RC 发布记录的 `prerelease` 字段是 `false`；本项目保留原始标记，不把它改写成稳定版结论。

## 按实际协议分组

| 已核对版本 | 管理员权限 | 渠道启停接口 | 渠道类型能力 |
| --- | --- | --- | --- |
| v0.13.2；rc.11–15 | 已启用账号，角色 10 或 100，核对令牌身份 | `PUT /api/channel/`，只传 `id`、`status` | 按每个标签的渠道常量与本项目已实现格式取交集 |
| rc.16–21；rc.19-i18nfix.2 | 除管理员身份外，必须返回明确的 `permissions.admin_permissions.channel` | `POST /api/channel/:id/status` | 不发布 59、60 类型能力 |
| rc.22 | 同上，独立的 read / write / operate / sensitive_write 权限 | 同上 | 新增已实现类型 59（Sub2API） |
| rc.23–35 | 同上 | 同上 | 新增已实现类型 60（NewAPI） |

`rc.16` 是独立渠道权限和状态路由的分界。不会因某个新版本未返回权限信息而回退为旧版角色授权。创建和编辑需要对应的敏感写入及普通写入权限；创建启用渠道还检查 operate 权限。`rc.22` 起管理认证内部改为统一访问令牌解析，但仍接受管理 PAT 的 Bearer 请求；适配器持续比对 `/api/user/self` 的账号 ID。

可核对的边界源码：[rc.15 管理路由](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.15/router/api-router.go)、[rc.16 渠道权限路由](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.16/router/channel-router.go)、[rc.16 身份权限响应](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.16/controller/user.go)、[rc.22 认证](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.22/middleware/auth.go)、[rc.22 类型定义](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.22/constant/channel.go)、[rc.23 类型定义](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.23/constant/channel.go)。

## 已检查的共同管理契约

- 使用管理账号 PAT，读取状态、令牌所属身份、渠道列表、详情、模型建议目录和分组。
- `POST /api/channel/` 创建请求为 `{"mode":"single","channel":{...}}`。批次中的凭据由本系统逐条分发。
- `models`、`group` 使用逗号分隔字符串；`settings` 和 `setting` 是 JSON 字符串。AWS 的 `aws_key_type`、Vertex 的 `vertex_key_type` 及代理字段保留各自含义。
- `PUT /api/channel/` 编辑；`DELETE /api/channel/:id` 删除；状态操作按上表选择路由。
- 模型名支持自定义，单个模型名不超过 255 字节；模板的分组必须存在。模型目录不会被误作拒绝所有自定义模型的白名单。
- 创建响应不一定返回渠道 ID。后台通过稳定名称读取确认；结果不明确时不会重复提交创建请求。
- 写入前复查平台版本。版本改变、权限不足、响应结构改变或回读设置不一致时停止对应操作。

核对包括每个发布标签的管理路由、认证中间件、渠道控制器、身份控制器、分组及模型控制器、渠道数据结构、类型定义、额外设置定义与渠道权限定义，共 **285 份源码文件**。文件 SHA-256 和发布时间保存在随应用发布的 [版本清单](../backend/app/adapters/newapi_releases.json)，运行时使用 [兼容契约读取器](../backend/app/adapters/newapi_compatibility.py)。该清单来自逐标签源码核对，不是按版本字符串范围自动放行。

## 边界与验证

覆盖的是本项目所需的**渠道管理与密钥分发协议**，并不表示实现 NewAPI 的所有模型推理接口、实验插件或管理功能。58、61 等没有本地格式实现的类型不会因为远端声明存在就发布为可用能力。账号权限、目标模型授权、代理协议和余额仍需由真实站点满足。

未列出的旧版、未来版、带自定义后缀的构建、fork 和 nightly 不自动启用。它们需要新增源码核对记录和测试后进入清单。运行时版本号只能用于选择已核对契约，不能证明远端二进制确实来自该官方标签，因此仍保留身份、权限、响应结构和回读校验。

适配器回归测试使用 MockTransport，逐个清单版本检查创建、读取确认、编辑、删除和正确的启停路由，并检查权限分界、渠道类型边界、未知版本及操作中版本变化。测试不会调用真实 NewAPI 管理写接口或执行模型推理。

## 已核对发布标签

以下时间为 GitHub 发布记录的 UTC 日期。v0.13.2 位于三个月窗口之外，是保留的既有稳定版兼容项。

| 发布标签 | 发布日期（UTC） | 权限契约 |
| --- | --- | --- |
| [v0.13.2](https://github.com/QuantumNous/new-api/releases/tag/v0.13.2) | 2026-04-27 | role_admin |
| [v1.0.0-rc.11](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.11) | 2026-06-13 | role_admin |
| [v1.0.0-rc.12](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.12) | 2026-06-18 | role_admin |
| [v1.0.0-rc.13](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.13) | 2026-06-19 | role_admin |
| [v1.0.0-rc.14](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.14) | 2026-06-20 | role_admin |
| [v1.0.0-rc.15](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.15) | 2026-06-24 | role_admin |
| [v1.0.0-rc.16](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.16) | 2026-07-03 | channel_rbac |
| [v1.0.0-rc.17](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.17) | 2026-07-06 | channel_rbac |
| [v1.0.0-rc.18](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.18) | 2026-07-06 | channel_rbac |
| [v1.0.0-rc.19](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.19) | 2026-07-07 | channel_rbac |
| [v1.0.0-rc.19-i18nfix.2](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.19-i18nfix.2) | 2026-07-07 | channel_rbac |
| [v1.0.0-rc.20](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.20) | 2026-07-07 | channel_rbac |
| [v1.0.0-rc.21](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.21) | 2026-07-11 | channel_rbac |
| [v1.0.0-rc.22](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.22) | 2026-07-26 | channel_rbac |
| [v1.0.0-rc.23](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.23) | 2026-08-01 | channel_rbac |
| [v1.0.0-rc.24](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.24) | 2026-08-07 | channel_rbac |
| [v1.0.0-rc.25](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.25) | 2026-08-18 | channel_rbac |
| [v1.0.0-rc.26](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.26) | 2026-08-26 | channel_rbac |
| [v1.0.0-rc.27](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.27) | 2026-08-29 | channel_rbac |
| [v1.0.0-rc.28](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.28) | 2026-08-30 | channel_rbac |
| [v1.0.0-rc.29](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.29) | 2026-08-30 | channel_rbac |
| [v1.0.0-rc.30](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.30) | 2026-08-31 | channel_rbac |
| [v1.0.0-rc.31](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.31) | 2026-09-03 | channel_rbac |
| [v1.0.0-rc.32](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.32) | 2026-09-04 | channel_rbac |
| [v1.0.0-rc.33](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.33) | 2026-09-05 | channel_rbac |
| [v1.0.0-rc.34](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.34) | 2026-09-06 | channel_rbac |
| [v1.0.0-rc.35](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.35) | 2026-09-07 | channel_rbac |
