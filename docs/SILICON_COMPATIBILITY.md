# Silicon 卖家接口兼容检查

更新日期：2026-09-10。Silicon 按卖家接口契约处理上传，版本号只用于诊断和识别旧版协议。新版本或隐藏版本在关键接口兼容时可以继续使用，无需逐个添加版本白名单。接入文档中的 `type: 1` 是示例，不是唯一支持的类型。

## 卖家接口契约

| 契约 | 创建、编辑设置字段 | 代理 | 其他配置 |
| --- | --- | --- | --- |
| `v1.0.0-rc.25-fix-22-multiseller-2` | `settings` 对象，`setting` JSON 字符串 | 未核实，不发布支持能力 | 按原接入文档的字段处理 |
| `silicon-seller-compact-v1`，运行时核对接口 | 读取 `settings` JSON 字符串，写入时转换成对象 | 顶层 `proxy` 字段 | 卖家字段白名单；不发送管理员专属的 `setting`、自动禁用、状态码映射、覆盖参数等字段 |

新版本的设置对象只发送 `azure_responses_version`、`vertex_key_type`、`aws_key_type`、`openrouter_enterprise`。自定义自动禁用、状态码映射以及 RPM 设置会在提交前被明确拒绝。默认值不表示卖家接口已经设置了这些未开放的功能。

七个业务分类对应渠道类型 `1、3、14、20、24、33、41`；OpenCode 使用 Anthropic 类型 14 和独立服务地址，Google 包含 Gemini 与 Vertex。AWS 的 IAM、API Key 及混合输入按每条凭据生成对应 `aws_key_type`。这些类型不再被错误限制为 OpenAI。

## 实际核验与证据

- `/api/status` 返回 `fix-38`，卖家身份编号与令牌一致，账号角色为卖家；管理员渠道权限没有授权。身份、列表权限和模型分组接口已再次只读核对。
- `/api/seller/channel/` 返回明确的创建、启停及路由修改权限。该账号实际已有 AWS 类型 33 的渠道。
- `/api/seller/channel/meta` 提供模型建议和可用分组。`fix-36`、`fix-38` 的公开表单允许添加自定义模型，提交时直接发送模型名；这两版按模型名称语法和长度校验，旧 `fix-22-multiseller-2` 仍保留模型白名单校验。不拿模型总数推断渠道类型支持。
- 目标渠道分组使用逗号分隔的多个名称，每个分组都须出现在卖家返回的可用分组中；创建和修改模型时都会重新核对全部分组。
- 公开 [卖家表单代码](https://silicon-ovhish.haoyi666.com/static/js/async/6697.331b797c0d.js) 的模块 8655 定义卖家范围与写入白名单；设置转换函数将 JSON 字符串解析并过滤为对象。公开 [渠道类型目录](https://silicon-ovhish.haoyi666.com/static/js/index.33f8462c64.js) 的模块 29164 定义类型，卖家表单仅隐藏 58。
- `fix-38` 当前 [首页脚本](https://silicon-ovhish.haoyi666.com/static/js/index.87f8a35a5e.js) 引用的卖家脚本 `6697.331b797c0d.js` 与 `fix-36` 字节相同，SHA-256 为 `3c0168265cff7734cfb16156401aa6ad8d47deddd953188f9a17b66aba694ee6`。接口模块 47551、权限页 1220、请求编码及消耗 schema 均相同；首页共享测试模块 23612 也逐字相同。公开文件、完整校验值与源码片段保存在 `.local/reference/silicon-fix38/contract-evidence.json` 和 `public-assets.json`。
- 实际卖家详情返回顶层 `proxy` 和字符串 `settings`，不包含管理员的 `setting`、`auto_ban`、`status_code_mapping`。回读按当前卖家可写字段检查，不要求不存在的管理员字段；代理缺失或改变仍会被识别为不一致。

原版本参考工作区的《Silicon卖家 API 接入文档.md》。本次只执行读取，未向真实站点创建、编辑、删除渠道、读取远端密钥明文或调用模型推理。兼容测试使用模拟请求覆盖已核对版本的字段、权限、设置转换、代理回读及版本变化；公开客户端代码不等同于真实写入验收。

## 运行时校验和启用开关

验证站点以及首次查询/核实时读取 `/api/user/self`、`/api/status`、`/api/seller/channel/` 和 `/api/seller/channel/meta`。核对身份、明确的布尔权限、分页结构、渠道核心字段、模型和分组结构。未知版本、版本隐藏或新增可选字段不影响通过检查。

创建/编辑/删除/启停以及模型测试在发出请求前再次核对接口与权限；模型和分组也按本次返回重新检查。仅版本号变化继续执行；协议形状、身份或权限变化则停止，并返回具体接口和字段信息。已发出的请求仍保留原有结果回读、唯一名称核实和禁止盲目重试机制。模型测试不作为兼容性探测自动执行。

Silicon 卖家分支与 [官方 NewAPI](NEWAPI_COMPATIBILITY.md)、Colin 管理分支分别选择协议，不会因为卖家没有管理员权限而改用管理员接口。已有旧 `fix-22-multiseller-2` 协议继续保留；跨越旧/当前两种写入结构时需重新准备操作。

新验证结果保存 `protocol_contract` 和 `compatibility_check=interfaces`。美元换算使用接口已确认的 `quota_per_unit` 和契约标识；没有有效比例时保留原始额度。历史快照保留各自版本、比例和时间，不回算历史金额。同步不能吞掉接口、身份或权限错误并标为成功。

2026-09-10 的 `fix-41` 公开接口模块、创建/编辑编码和设置转换与 `fix-38` 一致，仅增加可选搜索参数 `exclude_model`。证据保存在 `.local/reference/silicon-interface-check/REVIEW.md` 和 `contract-comparison.json`。这次核查没有向真实站点创建、编辑、删除渠道或调用模型推理。

模板修订号用于本地配置历史，不是站点版本。模板开关、站点分发开关分别保留；重新验证不会自动开启分发。
