# Prompt Profiles

这里保存生产 Prompt 的稳定版本元数据。模板只读取结构化镜头合同，不让视觉模型自由发明下游格式；Profile 内容变化必须提升版本并通过 `tests/test_prompt_profiles.py`。

本地 `Prompts/` 是规范与写作参考，生产渲染器位于 `app/services/prompt_service.py`。外部仓库只提供结构和术语参考，不会自动更新此目录。

新任务使用 `first-frame-v2` / `seedance-video-v2`；v1 定义仅用于兼容，旧任务不重算。中文交付说明随已有合同 JSON 保存，不新增必填端点或数据库列。

`style-rules.v1.json` 固定已审核的观察指导和来源摘要。后续风格文档按 `Prompts/STYLE_DOCUMENTS.md` 接入：审核、提升版本、添加回归，不自动扫描或执行 Markdown。实际摄影观察与毫米焦段、声音和时间线等生成建议分开存储。
