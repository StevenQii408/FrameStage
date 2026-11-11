# 评测与重放

工程回归、保存响应重放、人工真实素材验收分别统计。前两者不能代替第三者。默认不调用模型；付费工具必须显式使用其 --allow-paid 或 --live 开关。真实媒体与完整响应保存在仓库外。

## 当前规则与旧标注

- storyboard-eval.v2 保留来源真实 PTS，不设来源 4.8／5 秒上限；event_groups 使用原序来源镜头号标注真实事件归属。
- storyboard-eval.v1 保留原短镜头规则用于旧基线，不静默放宽旧标注。
- manifest.v2.schema.json 与 manifest.schema.json 是两版结构定义。运行时校验来源、顺序、有限时间和完整归属。
- generation_shots 时间属于参考素材，生成规格的半秒时间轴独立保存，不能改写来源证据时间。

人工标注覆盖人物、物体、前中后景、初始状态／动作／结束状态、重要特写、场次、画风和帧证据；未知与不可观察项显式标注。画风近邻、同事件切角与真实事件变化都应有对照样本。

## 无网络工具

在 backend 目录运行：

```bash
python -m evals.storyboard_reliability inventory
python -m evals.delivery_audit /outside/storyboard.json --output /outside/delivery-report.json
python -m evals.segmentation --manifest /outside/manifest.json --clip-id clip-example --result-json /outside/storyboard.json
python -m evals.event_agent /outside/event-result.json --gold /outside/event-gold.json
python -m app.style_library check
```

分别报告来源边界、场次、特写、时间覆盖与事件配对指标，事件数减少不直接等于质量提升。引用存在与语义正确分别评分。

prepare_gold 只生成素材哈希和待标注清单，不自动生成标签：

```bash
python -m evals.prepare_gold --media-dir /outside/authorized-media --files example.mp4 --output /outside/draft.json
```

## 真实调用与人工审阅

```bash
python -m evals.script_director --source-job YOUR_SCRIPT_JOB_ID --allow-paid --output /outside/script-report.json
```

该命令只读提取已有剧本，在临时数据库验证，不重写来源任务。完整原文和模型响应不能直接提交 Git。其他工具参数以模块 --help 为准：evals.flash_baseline、evals.perception_review、evals.style_direct_gold、evals.style_direct_review。

人工评审绑定报告哈希，区分结构通过、局部修复、保守兜底与语义通过。目前仍缺足量独立授权真实素材的人工验收，不能宣称达到设计中的准确率目标。

## 离线重放

evals.replay_perception、evals.replay_style_direct、evals.replay_style_pipeline 与 app.recompile_event_run 使用已保存结果排错。模板／Schema／策略变化时保留原版结果和输入哈希，不静默把旧任务标成新模型验证成功。

本轮验证见根目录 docs/RELEASE_VALIDATION.md。旧命令与单次排错记录保留于本地 docs/history，默认源码包不携带历史排错资料。
