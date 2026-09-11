# AWS 上传格式与分发约定

本项目在 AWS 分类下提供两个上传入口：**Bedrock 密钥**和 **Claude 代理 (api.aws)**。它们共享分类展示，但使用不同的上游协议。站点模板决定接收目标、模型和默认设置；用户不选择分发站点。

## Bedrock 密钥

原有 AK / SK、API Key 两个选择项合并成一个入口。每行可填写 AK/SK 或一个 Bedrock API Key，Region 可以省略，也可显式指定：

```text
AKIAIOSFODNN7EXAMPLE|SecretAccessKey
BedrockAPIKey
AccessKeyID|SecretAccessKey|us-east-1
APIKey|eu-west-1
```

同一批次允许混合这两种格式，并保存为一个本地渠道。后台按认证方式建立远端分区：AK/SK 一组、API Key 一组，分别创建 NewAPI `type=33` 渠道，均关联同一个本地渠道。同组有多把密钥时创建真正的多密钥容器，采用随机分流；不同逐条备注或代理会继续分区，避免覆盖用户配置。远端渠道的操作与消耗记录按分发 ID 独立管理。

官方 NewAPI 的已核对 27 个版本（`v0.13.2`、`v1.0.0-rc.11` 至 `rc.35`、`rc.19-i18nfix.2`）存在显式 `api_key` 请求分支错误：URL 中地区与模型位置颠倒，Authorization 又携带了含地区的完整字符串。新分发改走其 AWS SDK 路径，即远端 `aws_key_type=ak_sk`，SDK 根据当前选中凭据的段数自动选择两段 Bearer 或三段 SigV4。API Key 仍保留独立的本地认证分区，不与 AK/SK 混装。此适配仅用于精确核对的官方版本，Silicon、Colin 保留其原接入约定；未知版本不推断兼容。任务和分发记录冻结这一兼容选择，旧任务及旧渠道不自动切换。依据：[显式分支](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/aws/adaptor.go)、[SDK 凭据解析](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/aws/relay-aws.go)。

地区补全顺序：显式 Region → 官方短期 Bedrock API Key 的签名地区 → 后台默认 `us-east-1`。页面明确显示默认地区；这属于本地自动补全，不进行候选地区枚举或模型推理探测。不同地区应在末尾写 `|Region`。带显式地区的短期 Key 必须与自身签名地区一致。

省略 Region 的 AK/SK 通过 `AKIA` 加 16 位大写字母或数字识别，避免把 `APIKey|错误地区` 误判为 AK/SK。`ASIA` 临时 IAM 凭据还需要 SessionToken，当前 NewAPI AK/SK 协议没有传递该字段，不能仅补地区后使用；请改用 AWS 生成的短期 Bedrock API Key。多段、空字段或无效 Region 仍会报错。已有独立 `aws_ak_sk`、`aws_api_key` 旧格式继续要求完整地区，历史 ID 和任务不变。

官方短期 Key 以 `bedrock-api-key-` 开头，其 Base64 内是固定 Bedrock 主机的签名 URL。后台只读取经过结构检查的 `X-Amz-Credential` 签名地区，不请求该 URL、不验证密钥权限，也不返回解码内容。无法解码的已知短期格式会报错；长期 `ABSK…` Key 不猜测地区。实现依据：[AWS 官方短期密钥生成器](https://github.com/aws/aws-bedrock-token-generator-python/blob/main/aws_bedrock_token_generator/token_generator.py)。本系统保留规范化后单条凭据最多 4096 字符的限制。

备注和代理先按原始行绑定，再补全地区并根据规范化凭据去重：省略地区的输入与对应显式默认地区视为相同凭据；相同凭据但备注或代理不同仍报告冲突。任务冻结完整 NewAPI 凭据，重试无需重新补全地区。

这一转换遵循已检查的 [NewAPI v0.13.2 AWS 适配器](https://github.com/QuantumNous/new-api/blob/v0.13.2/relay/channel/aws/adaptor.go) 和 [v1.0.0-rc.35 AWS 适配器](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/aws/adaptor.go) 的输入要求。代码兼容性检查不能代替真实账号的地域、模型授权和余额验证。

## Claude 代理 (api.aws)

此入口接收提供 Anthropic Messages 兼容接口的 `api.aws` 代理凭据。当前上传页面采用以下输入方式：

- 密钥区：每行一个 API Key，支持单个和批量上传。
- API 地址：单独填写一个真实的 HTTPS 地址，**本批次所有密钥共用该地址**。

地址只接受 `https://<实际主机>.api.aws` 主机地址。允许去除首尾空白、统一主机大小写和去除一个末尾斜杠。禁止端口、路径、用户名密码、查询参数、锚点、非 `api.aws` 域名，以及 `xxx`、`example`、`test`、`tenant` 等占位主机名。密钥栏不接受把地址拼在密钥后面的混合输入。

后台创建 NewAPI `type=14` 渠道，密钥写入 `key`，规范化后的代理地址写入 `base_url`。NewAPI Claude 适配器请求 `base_url + /v1/messages`，使用 `x-api-key` 鉴权，并在没有版本头时设置 `anthropic-version: 2023-06-01`。[v0.13.2 Claude 适配器](https://github.com/QuantumNous/new-api/blob/v0.13.2/relay/channel/claude/adaptor.go)、[v1.0.0-rc.35 Claude 适配器](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.35/relay/channel/claude/adaptor.go)

GYS 的公开上传页面及本地留存的前端资源 `.local/gys-reference/index-Cv64ctGY.js` 是此业务输入方式的参考：其 `aws_a` 分类接收 `key` 和 `base_url`；单条分别填写，批量可以逐行提供地址。本项目当前使用单批共享地址的界面，不声称支持 GYS 所有输入形式。公开上传契约也不能证明任意 `api.aws` 主机均兼容该推理协议。[GYS 上传页面](https://gys.oljuxj.xyz/upload)

## 与 AWS 官方专用接口的区别

`api.aws` 域名后缀不能单独决定协议。以下服务不能按上述普通代理地址直接分发，目前的代理地址校验会拒绝已知官方专用主机：

| 服务 | 官方接口要求 | 与普通代理的区别 |
| --- | --- | --- |
| Amazon Bedrock Mantle | `https://bedrock-mantle.{region}.api.aws/anthropic/v1/messages` | Messages 路径包含 `/anthropic`；不能直接拼接普通代理的 `/v1/messages`。 |
| Claude Platform on AWS | `https://aws-external-anthropic.{region}.api.aws/v1/messages` | 除 API Key 鉴权外，还要求对应地域的 `anthropic-workspace-id` 工作区头。 |

上述差异来自 [AWS Messages API 文档](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html) 和 [Claude Platform on AWS 文档](https://platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws)。支持这些服务需要独立处理其接口路径、工作区和权限要求，不能把它们等同于截图中的租户代理。

格式校验与分发预检查不会产生真实模型调用。实际接收还取决于站点模板是否启用、目标站点是否支持相应渠道协议，以及真实代理是否提供所约定的接口。
