# 2026-09-08 模型配置更新

已更新咸鱼和 Colin 的现有分发模板，保留原有模型及自定义映射，把最新公开模型放在列表前面。完整 JSON 见 [MODEL_CATALOG_2026_09_08.json](MODEL_CATALOG_2026_09_08.json)。这份文件是本次配置记录，不会在启动时覆盖管理员的后续编辑。

| 模板 | 更新前 | 更新后 | 新增 |
| --- | ---: | ---: | ---: |
| Anthropic | 39 | 43 | 4 |
| OpenAI | 158 | 177 | 19 |
| Azure OpenAI | 158 | 172 | 14 |
| Google / Gemini | 30 | 40 | 10 |
| OpenRouter / Claude | 11 | 15 | 4 |
| OpenCode / Claude | 7 | 11 | 4 |
| AWS / Bedrock type33 | 17 | 17 | 0 |

## 名称与来源

- Claude：新增 `claude-fable-5-1`、`claude-fable-5`、`claude-opus-5`、`claude-sonnet-5`。依据 [Anthropic 模型目录](https://platform.claude.com/docs/en/models/overview)和[生命周期说明](https://platform.claude.com/docs/en/about-claude/model-deprecations)。没有为新模型生成未经证实的 `-thinking`、`-max` 等名称。
- OpenAI：新增 GPT-6 Astra、GPT-5.6 Sol/Terra/Luna、GPT-5.5/Pro、GPT-5.4 Mini/Nano、GPT Image 2、GPT Transcribe、Chat Latest，以及目录中的 Daybreak/Cyber 和官方快照名。依据 [OpenAI 模型目录](https://developers.openai.com/api/docs/models/all)和各模型的 Snapshots 项。保留原有 158 个条目，补入 19 个；每个密钥实际开放的模型仍由 OpenAI 账号决定。
- Azure：独立核对 [Microsoft Foundry 模型目录](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure)，使用 `gpt-chat-latest`、`gpt-5.3-chat` 等 Azure 名称。文档里的模型版本日期没有拼接成部署名。账号自定义部署名需按实际部署配置映射；现有 Azure 模板仍需填写其 API 地址。
- Gemini：新增 3.8/3.7/3.6/3.5 Flash、3.5 Flash Lite、3.1 Flash Lite、3.1 Flash Lite Image、3.1 Flash TTS Preview、Robotics ER 2/1.6 Preview。依据 [Google 模型目录](https://ai.google.dev/gemini-api/docs/models)（页面更新时间 2026-09-04）。TTS 使用原生 generateContent；type24 的 OpenAI 兼容 audio/speech 转换未实现。
- OpenRouter：依据[公开模型接口](https://openrouter.ai/api/v1/models)，新增 Claude 名称并映射到 `anthropic/…`；Fable 5.1 对应 `anthropic/claude-fable-5.1`。没有加入 batch 专用或 `~` 滚动别名。
- OpenCode：依据[公开模型接口](https://opencode.ai/zen/v1/models)，新增四个 Claude 名称，保留原有日期别名映射。

## 协议边界

添加模型名不等于补齐上游协议。此次按现有渠道接口处理以下差异：

- AWS 最新 Claude 使用 Bedrock Mantle 的 `/anthropic/v1/messages`，不能直接加入仍使用 InvokeModel/Converse 的 NewAPI type33 模板。现有 17 个条目保留。新模型还需适配真实 Region、Mantle 地址、认证方式和 `anthropic.…` 模型映射；当前 AWS Claude 代理入口只接收既有租户代理契约，不能直接当成已完成的 Mantle 接入。参考 [Anthropic Bedrock 文档](https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock)与 [NewAPI rc35 AWS 实现](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/aws/adaptor.go)。
- Gemini Omni 1.1 Flash、3.5 Transcribe 使用 Interactions；3.5 Transcribe Live、3.5 Live Translate Preview、3.1 Flash Live Preview 使用实时接口。已研究的 [NewAPI rc35 Gemini 实现](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/gemini/adaptor.go)没有这些接口转换，因此没有把这五个名字加入现有 type24 模板。
- GPT Live Transcribe 使用 `/v1/realtime/transcription_sessions`；已研究的 [NewAPI rc35 路由](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/router/relay-router.go)没有此入口，因此未新增。GPT Transcribe 可使用现有 `/v1/audio/transcriptions`。
- GPT OSS 是开放权重模型；没有作为 OpenAI 官方托管 API 模型补入。已退役且原模板不存在的 Codex Mini 也没有新增。

## 分组与验证

目标渠道分组支持搜索和多选，最多 32 组、合计 160 个字符。同一渠道加入所有选中分组，不会按分组重复创建渠道。旧单分组字符串仍然有效；启用、入队及远端写入均逐组校验权限。

Silicon fix36 和 Colin rc32 的官方渠道表单支持填写目录外的自定义模型；已修正本地错误的模型白名单限制，保留名称格式、长度和权限校验。旧 Silicon 合约仍使用已验证的白名单。

本次使用本地管理 API 保存配置并回读检查。相关后端测试 626 项通过，前端构建、模拟浏览器及部署后页面检查通过。实际页面核对了全部 14 份模板的模型数量、JSON 原值、多选后取消与重新打开；检查前后模板数据一致，页面无错误。没有向远端创建测试渠道，也没有使用用户密钥执行推理。保存模型时，站点和模板开关、现有分组保持原值。
