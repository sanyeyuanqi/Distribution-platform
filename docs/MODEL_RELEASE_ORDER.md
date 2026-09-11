# 模型显示顺序

各分类的模板模型选项、已有模板的模型标签、映射候选、上传高级选项及测试模型下拉统一按官方 API 发布日期降序显示。排序在候选与已选项合并后执行，同日发布的模型保留原顺序，按完整 ID 去重，不更改实际模型 ID、已保存配置、RPM/TPM 或映射关系。

日期集中维护在 `frontend/src/model-release-dates.json`，排序逻辑在 `frontend/src/model-release-order.ts`。数据核对日期为 2026-09-10，逐项来源与别名说明见 [来源记录](MODEL_RELEASE_DATE_SOURCES_2026_09_10.json)。主要依据 [OpenAI API 更新日志](https://developers.openai.com/api/docs/changelog)、[Claude API 发布记录](https://platform.claude.com/docs/en/release-notes/overview)和 [Gemini API 更新日志](https://ai.google.dev/gemini-api/docs/changelog)。

使用确切稳定版或预览版 API 首次公开可用日期。OpenAI 使用正式 API 发布记录，区分早先仅在 ChatGPT/Codex 产品内的发布；例如 GPT-5.3 Codex 使用 2026-02-24 的正式 API 发布日期。官方 API 记录优先于模型 ID 中的快照日期，例如 `claude-haiku-4-5-20251001` 的发布日期为 2025-10-15。

没有核实日期的自定义 ID、无法确认目标的动态 `latest` 别名和未找到精确 ID 发布记录的模型稳定置于末尾，界面提示未知数量。不按名称中的版本号、8 位数字或日期后缀猜测发布时间。用户清单中的 `gpt-image-2.5` 与 `gpt-4o-transcribe-2025-03-20` 保留在选项中，因未确认精确 ID 的官方发布日期，暂列未知。

已识别的供应商前缀、Bedrock/Vertex 表达方式及本地推理强度后缀仅继承基础模型日期用于显示排序，不表示远端一定接受此别名。数据录入和编辑表单中的原始顺序不被排序逻辑写回，模型缺口等带业务指标排序的报表保留自身规则。

验证命令：`node --test frontend/tests/model-release-order.test.mjs`。测试覆盖日期优先、别名、未知日期、同日稳定顺序、精确去重及输入不可变。
