# TCP Red / Colin 平台接入

适配器标识：`tcp-red-v1`。目标站点：`https://api.tcp.red`。

2026-09-07 实测版本为 `v1.0.0-rc.32-colin`。该平台供应商角色为 `5`，使用 `/api/channel` 系列接口。它与 Silicon 的 `/api/seller/channel` 接口、权限结构及设置字段类型不同，因此使用独立适配器，不能只替换域名。

## 配置

在站点管理中选择 **TCP Red / Colin Supplier API**，填写站点根地址、供应商用户编号、访问令牌，保留默认渠道分组 `default`，然后验证连接。原有站点可直接编辑适配器，不需要重新保存令牌。

验证会检查令牌身份、账号状态、明确的渠道权限、平台版本、渠道列表、模型列表及渠道分组列表。成功后可启用分发。站点默认分组保存在独立 `routing_group` 字段中；新增任务冻结该分组，执行前如组配置改变则停止旧创建任务。

这里需要读取 **`GET /api/group/?scope=channel`**。普通用户的可消费分组与可配置的渠道分组不同，不能用 `/api/user/self/groups` 或不带 scope 的分组列表替代。实际渠道列表包含 `default`。

分发模板支持选择多个渠道分组，按逗号分隔字符串写入同一远端渠道，写入前逐一核对是否仍可用。Colin `v1.0.0-rc.32-colin` 的已核对渠道表单允许自定义模型，因此模型目录仅作为建议；新增模型仍需满足非空、无逗号或换行、单个名称不超过 255 个 UTF-8 字节、每模板最多 200 个名称的规则。允许配置模型不等于上游密钥已具备该模型的调用权限。

## 请求契约

鉴权头沿用 `Authorization: Bearer <访问令牌>` 和 `New-Api-User: <供应商用户编号>`。凭据不写入日志、任务快照、诊断或文档。

| 操作 | 接口 | 关键字段 |
|---|---|---|
| 身份、权限 | `GET /api/user/self` | `data.id`、`status`、`permissions.admin_permissions.channel` |
| 版本 | `GET /api/status` | `data.version` |
| 渠道列表 | `GET /api/channel/` | `p`、`page_size`；返回 `data.items/total` |
| 稳定名称搜索 | `GET /api/channel/search` | `keyword`、分页参数；本地严格匹配名称 |
| 详情 | `GET /api/channel/{id}` | 渠道对象，包括 `created_by` |
| 模型 | `GET /api/channel/models` | `data` 为模型对象数组 |
| 渠道分组 | `GET /api/group/?scope=channel` | `data` 为字符串数组 |
| 新建 | `POST /api/channel/` | 使用带尾斜杠的规范路由，避免无尾斜杠请求返回 HTTP 307；`{mode: "single", channel: {...}}` |
| 编辑 | `PUT /api/channel/` | 有尾斜杠；扁平 `{id, ...字段}` |
| 启停 | `POST /api/channel/{id}/status` | `status: 1` 启用，`status: 2` 停用 |
| 删除 | `DELETE /api/channel/{id}` | 单渠道操作 |

分发已支持公开表单定义的固定渠道类型及凭据格式，包括 OpenAI、Azure、Anthropic、Gemini、AWS、Vertex、Codex 等；完整格式与设置见 [NEWAPI_COMPATIBILITY.md](NEWAPI_COMPATIBILITY.md)。新建默认 `status=2`，模板可选择启用（同时要求 `operate=true`）；`models` 和 `group` 为逗号字符串；`setting`、`settings` 都是 JSON **字符串**。Silicon 的 `settings` 对象类型不适用于该平台。

新建不指定 `created_by`，由远端根据供应商账号确定归属。模板中明确配置的优先级/权重仅在具有路由权限时写入。成功响应不一定包含渠道 ID；缺少 ID 时按同一个稳定名称读取并核对模板配置，不能自动重复新增。编辑保留已读取的类型、路由优先级、权重及设置；不回传掩码密钥、状态或创建者，仅允许本地工作流中的备注、模型和明确的新密钥更新。

平台备注上限为 255 字符。超过上限会明确拒绝，不自动截断。

## 权限与安全

- 读取需要明确的 `channel.read=true`。
- 新增、敏感编辑及删除要求 `write=true` 且 `sensitive_write=true`。
- 启停要求 `operate=true`。
- 现有渠道写入前，还要求 `created_by` 与令牌身份相同，或明确拥有 `sensitive_write_all=true`。
- 不把角色数值或字符串 `"true"` 当成权限授权，不请求远端明文密钥接口。
- 本地 RBAC、账号/渠道归属、站点启用状态、凭据版本、取消状态、Redis 租约及写前复查继续生效。
- 仍使用 DNS 检查、目标 IP 固定、TLS 校验、响应大小限制，且不自动跟随重定向。
- 验证失败可显示请求方法、固定接口路径及 HTTP 状态，不保存远端原始错误正文。

Colin 的读取接口可能将 `base_url` 返回为 `***`，这表示字段已隐藏，不是实际接口地址。只有原任务已收到创建成功响应并持久保存后，才允许在核实同一远端渠道身份及其他配置时，将此字段记录为“未核对”；后端保留可见性标记，不能宣称已完整核实。正常脱敏不作为渠道状态说明显示，页面仍展示真实渠道状态和实际操作错误。创建前发现的同名渠道、真实地址差异及其他配置差异仍不能自动接受。隐藏地址不能原样回传到编辑接口。

## 验证范围

接口路径、请求包装、设置字段及权限归属依据平台实际提供的公开前端代码：

- `/static/js/index.15d0a1d9f9.js` 的渠道请求包装。
- `/static/js/async/3800.7c30100dc1.js` 的渠道表单序列化。
- `/static/js/async/5151.c047ef84ff.js` 的权限与创建者检查。
- `/static/js/async/3425.639564c3ce.js` 的渠道页面调用。

已使用用户授权的访问令牌实测身份、权限、列表、模型、渠道分组读取。新增、编辑、启停、删除通过离线契约及任务回归验证；本次接入未向真实平台提交占位密钥或创建测试渠道。

公开表单 `3800.7c30100dc1.js` 定义代理 `setting.proxy`、RPM 保护 `setting.rate_limit_enabled/rpm_limit` 以及 AWS/Vertex 等格式设置，因此这些字段已按固定契约开放。RPM 能力为 `supported`，运行效果为 `not_measured`，没有对真实平台做限流压测。号况中的余额、RPM、TPM、预付、KD 仅作为本地申报信息，不映射成远端余额或自动限流；远端测试后启用、消耗采集与独立 TPM 保护仍未开放。每次写入前重新核对精确平台版本，变化时停止写入并要求重新核对契约。
