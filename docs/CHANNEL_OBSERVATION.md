# 渠道模型测试与远端消耗快照

2026-09-09 更新：渠道详情的“测试”已改为本地保存凭据直连上游，见 [当前测试与同步流程](CHANNEL_MONITORING.md)。下文保留远端适配器的协议核对记录；应用测试任务不再调用 `adapter.test_channel`，`adapter.usage` 继续用于“同步”。

2026-09-11 更新：当前 [结算订单](SETTLEMENT_ORDERS.md) 使用身份匹配、完整且可换算为 USD 的远端观测，并保存逐目标基线。下文 `settlement_verified=false` 表示这些观测不是独立的 `UsageFact` 核实事实，不再表示当前订单一律不能使用它们。旧累计采样接口和表已退休，渠道观测继续保留。 后续同步策略改为仅后台定时执行，页面刷新和结算不触发远端采集；下文保留协议核对历史，当前调度规则见 [后台自动同步](AUTOMATIC_SYNC.md)。

核对日期：2026-09-08。连接验证和普通同步不调用模型；只有用户提交的指定模型测试任务会请求推理。

| 平台 | 指定模型测试 | 权限 | 消耗读取 |
| --- | --- | --- | --- |
| Silicon `v1.0.0-rc.25-fix-36`、`v1.0.0-rc.25-fix-38` | `GET /api/seller/channel/test/{id}?model=名称` | 明确 `can_write=true`，卖家详情限定归属 | 卖家渠道详情 `used_quota` 等白名单字段 |
| Silicon `v1.0.0-rc.25-fix-22-multiseller-2` | 旧文档只列测试路径，尚未核实指定模型参数，不开放 | — | 尚未核实消耗字段，不开放 |
| Colin `v1.0.0-rc.32-colin` | `GET /api/channel/test/{id}?model=名称` | 明确 `operate=true`，并复核 `created_by` 或管理全部渠道权限 | 管理渠道详情白名单字段 |
| 官方 New API 已支持的 27 个版本 | `GET /api/channel/test/{id}?model=名称` | 老版本 AdminAuth；RBAC 版本明确 `ChannelOperate` | 管理渠道详情白名单字段 |

## 请求与结果

测试接口虽然使用 GET，实际会调用渠道上游模型并更新远端测试时间，可能产生请求消耗。调用前重新检查版本、权限、渠道归属、远端 ID/名称/类型，以及模型是否仍在远端渠道的模型列表中。任务的最后一次校验与持久化发送标记在实际测试 GET 发出前执行。

官方测试响应使用顶层布尔 `success` 和以秒计的 `time`。已核对的 Silicon/Colin UI 同时兼容 `data.response_time` 毫秒。适配器仅返回明确成功/失败、有限非负延迟和固定中文说明；不返回远端错误正文、凭据、代理地址或推理内容。超时、网络错误、5xx、无效 JSON 或缺少明确成功标记均为未知结果，禁止自动重新测试。测试成功不自动启用渠道。

渠道消耗只读取现有详情和站点公开状态，不调用 `update_balance`、额度重置或任何会调用上游的余额刷新接口。快照保留 `used_quota`、`balance`、`balance_updated_at`；2026-09-09 起新增美元换算，`used_amount = used_quota / quota_per_unit`，单位 `used_amount_unit=USD`，比例来自受支持版本的 `/api/status`。站点人民币/自定义币种展示设置不会改变这个美元公式。跨平台不推断余额币种；缺失/非法字段为 `null`，真实零值保留。快照明确 `settlement_verified=false`，不作为结算事实，不开放 `stats` 能力。

## 代码与核对依据

- 公共方法：`adapter.test_channel(remote_id, model, expected=身份快照)`、`adapter.usage(remote_id, expected=身份快照)`。
- 列表同步复用纯白名单解析器 `app.adapters.channel_observation.extract_usage(remote)`。已存在列表记录可以提供观察值，但不存在的字段不会被补成零。
- [Silicon fix36 / fix38 公开渠道代码](https://silicon-ovhish.haoyi666.com/static/js/async/6697.331b797c0d.js)：卖家接口模块定义测试路径和参数；卖家 scope 设置 `testChannel` 与 `canTest=canWrite`；详情 schema 定义 `used_quota`、`balance`、`balance_updated_time`。[fix38 共享测试处理代码](https://silicon-ovhish.haoyi666.com/static/js/index.87f8a35a5e.js)传入 `model` 并解析上述两种时间结构，其模块 23612 与 fix36 字节相同，核对记录见 [Silicon 兼容说明](SILICON_COMPATIBILITY.md)。
- [Colin 公开渠道 API 包装](https://api.tcp.red/static/js/index.15d0a1d9f9.js)定义管理测试路径，[测试表单处理代码](https://api.tcp.red/static/js/async/3800.7c30100dc1.js)传入所选模型并读取测试结果；[渠道列表](https://api.tcp.red/static/js/async/3425.639564c3ce.js)读取 `used_quota` 等字段。源码已保存在 `.local/tcp-red-inspect`。
- 官方兼容清单中的 `v0.13.2`、`v1.0.0-rc.11` 至 `rc.35` 以及 `rc.19-i18nfix.2` 的 `controller/channel-test.go` 已逐版本核对：均读取 `c.Query("model")`，返回顶层 `success/time`。示例：[v0.13.2](https://github.com/QuantumNous/new-api/blob/v0.13.2/controller/channel-test.go)、[rc.35](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/controller/channel-test.go)。路由与权限来自对应版本 `router/api-router.go` 或 [channel-router.go](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/router/channel-router.go)，消耗字段来自 [model/channel.go](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/model/channel.go)。

离线契约测试位于 `backend/tests/test_channel_observation_adapters.py`，仅使用模拟 HTTP，覆盖全部已支持官方版本、当前卖家版本、所选模型、权限/身份/版本变化、发送前取消、未知响应防重发以及消耗字段过滤。实现过程中未执行真实模型测试或远端修改。
