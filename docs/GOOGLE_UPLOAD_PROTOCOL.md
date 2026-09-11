# Google 三种上传服务

核对日期：2026-09-08。以下结论来自官方文档及 NewAPI `v0.13.2`、`v1.0.0-rc.1` 至 `rc.35` 的 Vertex 源码；未读取业务凭据、未调用推理接口。服务划分与真实账号的模型授权、计费状态分别判断。

| 上传服务 | NewAPI 渠道类型 | 凭据 | 渠道认证设置 |
| --- | --- | --- | --- |
| AI Studio · Gemini | 24 | Google AI Studio API Key | type 24 原生 Gemini 协议 |
| Vertex AI · Gemini | 41 | 服务账号 JSON 或 Vertex Express API Key | `settings.vertex_key_type` 分别为 `json` / `api_key` |
| Vertex AI · Claude | 41 | 服务账号 JSON 或 Vertex API Key；API Key 还需目标能力支持 | `settings.vertex_key_type` 分别为 `json` / `api_key` |

Google 文档明确支持 Gemini 使用 Google Cloud API Key 或应用默认凭据；Express API Key 请求省略项目和地区路径。它与 AI Studio 服务入口分别处理，不能仅凭 `AIza` 前缀识别服务。[Google API Key](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/api-keys)、[Express 请求地址](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/express-mode/overview)。

Claude 入口接受两种凭据，API Key 按目标站点的 `vertex_claude_api_key` 能力判断。NewAPI `v0.13.2` 和 `rc.1` 的 API Key 分支固定使用 `publishers/google`，即便模型名是 Claude；`rc.2` 首次加入单独的 Anthropic publisher 路径，逐版本确认持续至 `rc.35`。本项目管理接口清单从 `rc.11` 开始，因此只有已核对的 `rc.11` 至 `rc.35`（含 `rc.19-i18nfix.2`）声明支持；旧稳定版不支持。未知版本和无法核对 relay 实现的 Silicon、Colin 分支保持 `unknown`，不能从类型 41、模型列表或版本号大小推断支持。[旧版实现](https://github.com/QuantumNous/new-api/blob/v0.13.2/relay/channel/vertex/adaptor.go)、[rc.2 实现](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.2/relay/channel/vertex/adaptor.go)、[rc.35 实现](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/vertex/adaptor.go)。

Google 现区分普通 API Key 与绑定服务账号的 authorization key：后者可作为服务账号认证访问 Vertex。密钥字符串本身不能证明账号绑定、IAM 权限或 Claude 模型开通；协议能力通过不代表实际推理已验证。Google Claude 指南仍提供 OAuth Bearer 示例。[Google API Key 类型](https://docs.cloud.google.com/docs/authentication/api-keys)、[Google Claude REST](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/use-claude)。

## JSON、项目与地址

NewAPI 从 JSON 的 `project_id`、`client_email`、`private_key` 读取项目和签名材料，以 RSA 私钥签署 JWT，再换取 OAuth token；本项目继续保存完整 JSON，不能替换成只含 API Key 的文本。获取 token 的主机固定在 NewAPI 实现中，JSON 的自定义 `token_uri` 不决定请求地址。[认证实现](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/vertex/service_account.go)。

新模板不需要 Base URL。type 41 的 `base_url` 留空，让 NewAPI 使用官方地址，并固定 `other` 为 `{"default":"global"}`：

- JSON / Gemini：`https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/global/publishers/google/models/{model}:generateContent`。
- JSON / Claude：同样的项目路径，publisher 为 `anthropic`，动作为 `rawPredict`；流式为 `streamRawPredict`。
- Express API Key / Gemini：`https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent?key=...`。
- 支持版本的 API Key / Claude：`https://aiplatform.googleapis.com/v1/publishers/anthropic/models/{model}:rawPredict?key=...`。

旧版空 `other` 不会自动补 global；rc.35 的 URL builder 会补。显式设置默认地区可兼容两者。地区配置是纯地区字符串或按用户原始模型名索引的 JSON 对象；不要给所有历史模型、所有账号的预留吞吐量宣称 global 可用。当前 Claude 文档推荐 global，但预留吞吐量需要区域端点；新增的 `us` / `eu` 多区域域名也不能直接当普通地区塞给这两版 URL 构造器。[地区选择代码](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/vertex/relay-vertex.go)、[URL 构造](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/vertex/url_builder.go)、[Anthropic 地区与模型 ID](https://platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai)。

## 模型及格式隔离

type 41 根据上游模型名的 `claude` 前缀选择 Claude 协议，所以仅有 `remote_type=41` 不能表达两种业务用途。新定义采用独立 `vertex_gemini` / `vertex_claude` 服务标识，再在每条分发任务中推导原生 `vertex_json` / `vertex_api_key` 凭据设置。同批可以保存在一个本地渠道中；分发时 JSON 与 API Key 须分区，因为 NewAPI 的 `vertex_key_type` 是远端渠道级设置，不是密钥列表中每项独立设置。

原生 JSON 多密钥创建用 `mode=multi_to_single`、`multi_key_mode=random` 或 `polling`，`channel.key` 是 JSON 对象数组的序列化字符串；各 JSON 的项目可不同，请求时读取当前选中凭据的 `project_id`，OAuth 缓存也按密钥索引隔离。地区、服务和模型配置仍共用。官方 rc.35 及 Silicon fix36/fix38 前端禁止 Vertex API Key 的非 single 模式，但官方 controller 没有这个禁令：API Key 模式直接按换行拆分，runtime 每次选择一个 key。故不能把前端限制描述为 NewAPI 运行时不支持；是否每 Key 独立分发应按已核对的站点协议策略决定。[创建解析](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/controller/channel.go)、[运行时选 Key](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/model/channel.go)、[前端限制](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/web/src/features/channels/lib/channel-form.ts)。

Claude 的九个既定上传名称中，仅 `claude-haiku-4-5-20251001` 需要转换为 `claude-haiku-4-5@20251001`；其余八个官方 Vertex 名称与上传名一致。只对模板与上传模型的有效交集添加此映射。Gemini 不复制 Claude 映射；其它 Google 模型的服务可用性仍由实际模型与协议能力决定，不能把 AI Studio 完整目录当 Vertex 支持清单。

旧 `vertex_json` 定义只有在模型全属于 Gemini 或全属于 Claude 时才能无歧义派生服务；混合、空或未知列表保留为 `vertex_legacy`。旧 `vertex_api_key` 即使模型全是 Claude，也保留为 legacy，不静默升级为可用 Claude 服务。历史格式和已冻结任务不原地改写。
