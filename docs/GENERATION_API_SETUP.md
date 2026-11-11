# 图片与视频生成 API 配置

本阶段使用 **千问 Image 3.0 Pro（`qwen-image-3.0-pro`）** 和 **万相 3.0（`wan3.0-video`）**。生成服务与现有 `qwen3.8-flash` 分镜分析独立配置，不改变分析模型。以下步骤只保存、检查本地配置；不会自动提交生成任务。

## 1. 密钥与区域

图像、视频和 Flash 可以复用同一个百炼工作区的 API Key，前提是模型、Key、工作区端点属于同一区域，且该账号已获得对应模型权限和可用额度。能调用 Flash 不等于已经验证生图、生视频权限。

默认读取已有私有 `backend/.env` 中的 `STORYBOARD_BAILIAN_API_KEY`（或 `DASHSCOPE_API_KEY`），不需要把 Key 再发到聊天或放入前端。可选的 `STORYBOARD_IMAGE_API_KEY` 和 `STORYBOARD_VIDEO_API_KEY` 用于分别配置生成密钥。

生成接口使用原生 `/api/v1` 基础地址。已有的 `/compatible-mode/v1` 地址会在本地转换；例如：

```text
https://your-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
→ https://your-workspace.cn-beijing.maas.aliyuncs.com/api/v1
```

不要将 `/chat/completions`、`/tasks/{id}` 或某个生成接口完整路径写进生成基础地址。API Key 不应出现在 URL、命令行参数、Git 提交或浏览器环境变量中。

## 2. 先检查已有配置

从项目根目录执行：

```bash
cd backend
.venv312/bin/python -m app.generation_setup --status
```

输出仅包含模型 ID、区域基础地址、密钥是否存在和调度限制，不打印密钥。`live_check: not-performed` 表示没有验证云端鉴权和模型权限。`enabled: false` 是首次安装的默认状态，不会因为已有 Flash Key 而自动开放付费生成。

## 3. 保存或更新生成配置

```bash
.venv312/bin/python -m app.generation_setup --configure
```

工具会分别询问生图、视频的基础地址与可选密钥。Key 隐藏输入；已有百炼 Key 时留空即可复用。其他配置会保留，文件按权限 `600` 原子保存。若使用 `STORYBOARD_ENV_FILE`，则保存到该变量指定的私有配置文件。

同名环境变量优先于私有文件；模型专用 Key 优先于共享百炼 Key。排查时应同时查看是否存在旧的 `STORYBOARD_IMAGE_*`、`STORYBOARD_VIDEO_*` 或共享百炼环境变量。检查状态命令显示密钥来源，但不会打印密钥内容。

准备开始实际生成时，在私有 `.env` 中设置：

```dotenv
STORYBOARD_GENERATION_ENABLED=1
STORYBOARD_IMAGE_MODEL=qwen-image-3.0-pro
STORYBOARD_VIDEO_MODEL=wan3.0-video
```

然后重启后端：

```bash
.venv312/bin/python -m app.start_cloud
```

显式开启生成之后，仍需由用户在节点上提交生成任务。工具不会自动补跑历史受阻任务，不会因重新启动后端而重新提交已有云端任务。

## 4. SSE、轮询与真实进度

本项目有两条独立链路：

```text
阿里云生成任务 → 后端异步状态查询 → 持久化任务与事件
                                         ↓
                              项目级 SSE → 画布生成节点
```

千问 Image 3.0 Pro 的官方接口提供同步非流式调用和异步任务调用，不能仅添加 `stream: true` 就变成图片 SSE。这里选择异步调用，先保存云端任务 ID，再按任务状态查询。万相视频也走异步任务。前端使用统一的项目级 SSE 接收多个节点的更新，避免每个节点单独维持一条浏览器事件连接。

默认状态查询间隔为 15 秒，并由调度器错峰。服务商只返回排队／生成／成功／失败时，界面展示真实阶段和已用时，不模拟连续百分比；实际产物下载、解码和合同校验通过后才显示完成。

查询网络出错不会自动重新发起付费生成。已获取云端任务 ID 时应继续查询原任务；提交请求发生超时且无法确认是否受理时，费用与远端状态属于未知，不能将它当作“未收费”或盲目重提。

在画布中取消任务，只能保证停止本地交付。服务商对运行中任务可能不支持取消，任务也可能继续产生费用。后端单独保留远端并发名额，低频核对已取消或本地失败的云端任务，直到收到明确终态才释放，不会因为前端显示“已取消”就超额提交。

若提交超时且没有拿到远端 ID，系统不能自动找到该次请求。该名额进入未知状态，需要先在服务商控制台核查。确认任务已停止后，可通过受本机会话和项目隔离保护的 `POST /api/generations/{job_id}/confirm-cloud-stopped`，提交 `{"confirmed_remote_stopped":true}` 显式释放；只允许本地已结束的任务，操作会保存审计事件。这项确认不会将费用改为已退款，也不会重新发起生成。

## 5. 并发与排队

这些是本地调度上限，不等于账号已经获得的云端额度：

| 配置 | 默认值 | 含义 |
| --- | ---: | --- |
| `STORYBOARD_GENERATION_MAX_INFLIGHT` | 16 | 图片与视频合计的云端在途任务数，可配置至 32 |
| `STORYBOARD_IMAGE_MAX_INFLIGHT` | 10 | 图片在途任务上限，同时受总上限限制 |
| `STORYBOARD_VIDEO_MAX_INFLIGHT` | 16 | 视频在途任务上限，同时受总上限和账号配额限制 |
| `STORYBOARD_GENERATION_MAX_PENDING` | 100 | 本地等待队列上限 |
| `STORYBOARD_GENERATION_IO_WORKERS` | 4 | 提交／查询短请求的工作线程数 |
| `STORYBOARD_GENERATION_DOWNLOAD_WORKERS` | 2 | 结果下载并发 |
| `STORYBOARD_GENERATION_IMAGE_FINALIZE_WORKERS` | 2 | 图片解码、标准化并发 |
| `STORYBOARD_GENERATION_VIDEO_FINALIZE_WORKERS` | 2 | 视频本地校验并发 |
| `STORYBOARD_IMAGE_SUBMIT_RPM` | 5 | 图片提交速率，每分钟请求数 |
| `STORYBOARD_VIDEO_SUBMIT_RPM` | 5 | 视频提交速率的保守初始值 |
| `STORYBOARD_GENERATION_POLL_INTERVAL_SECONDS` | 15 | 单任务查询间隔 |
| `STORYBOARD_GENERATION_TIMEOUT_SECONDS` | 1800 | 单任务总时间预算，秒 |
| `STORYBOARD_GENERATION_REQUEST_TIMEOUT_SECONDS` | 60 | 单次网络请求时间预算，秒 |

Qwen Image 3.0 Pro 官方限流文档列出的默认图片提交频率为 5 次／分钟、异步并发为 10。提高本地上限不会绕过云端限制；应先以账号控制台实际额度为准，出现 429 时执行退避。生成任务数量与提交速率是两种限制，16 个在途名额不代表允许瞬间提交 16 次请求。

## 6. 输出与安全边界

- 图片按画幅显示后端提供的像素尺寸，覆盖合法的512至2048短边预设；组合必须满足512²–2048²总像素限制。旧2160p仅用于历史读取，不作为千问新任务选项。
- Wan 3.0 视频支持 **480P、720P、1080P、2–30秒整数**，以及21:9、16:9、9:16、1:1、4:3和3:4画幅。模型名称与选项取自后端能力目录，不由前端猜测。
- 视频的分辨率是服务商档位，实际宽高始终由本地解码并原样展示；存在官方计量信息时，档位、画幅与解码像素规模一起校验，允许有限编码对齐误差。此容差是本地交付策略，不宣称是官方像素桶映射；没有官方计量时保留严格校验，不裁切或拉伸结果来通过检查。
- 生成中卡片显示星海潮汐及较小的数字百分比：双层波面、明亮星云和星尘随进度自底向上涨。真实服务商百分比优先；仅返回阶段时显示明确标注的“预计 xx%”，基于阶段与时间保守估算，生成阶段最高95%，保存产物阶段最高99%，仅实际成功显示100%。估算仅用于 UI，不写入后台任务、SSE 或资产历史，也不承诺完成时间。旧生成视频通过惰性缓存的JPEG首帧在节点完整显示，播放时才加载原视频。
- Prompt 中的 `@图片N` 显示为带1:1缩略图的行内标签，悬停完整预览；提交内容仍为纯文本引用，不含HTML或图片数据。
- 参考图按所选顺序上传，Prompt 中的图片引用应与这份输入快照一致。
- 媒体下载不携带模型 API Key；结果先写临时文件并验证，完成后才发布为本地资产。
- 当前 HTTP 调用均有整体截止时间并独立关闭连接；跨任务的持久 HTTP 连接池尚未接入。并发由短请求和下载线程上限控制，DNS 结果短期缓存。
- 配置命令不包含真实生成连通性测试；接入后的首次小额生成应单独执行并核对结果、耗时、失败状态和费用。

## 7. 失败反馈

失败通过原项目 SSE 下发公开错误码、中文说明和可重试标记；卡片停止进度并保留之前的成功产物。以下原因只根据结构化服务商代码判断，未知原因显示通用失败，不根据图片、等待时长或任意报错文字猜测。

| 卡片原因 | 公开错误码 | 处理方式 |
| --- | --- | --- |
| 上游算力不足 | `provider_capacity_exhausted` | 等待后手动重试 |
| 请求频率或并发受限 | `rate_limited` | 等待后重试，必要时降低提交频率 |
| 余额不足或欠费 | `provider_balance_insufficient` | 检查账户账单后创建新任务 |
| 额度或预算用尽 | `provider_quota_exceeded` | 检查配额／预算后创建新任务 |
| 密钥或调用权限问题 | `provider_authentication_failed` / `provider_access_denied` | 检查区域、密钥与模型开通状态 |
| 内容审核未通过 | `content_rejected` | 修改 Prompt 或参考图后提交 |
| 网络连接失败／超时 | `provider_network_error` / `provider_timeout` | 先核查原云端任务，避免重复生成 |
| 结果规格不符／媒体不可读 | `output_contract_mismatch` / `invalid_image` / `invalid_video_output` | 不发布不合格产物，保留旧版本 |

服务商只返回通用错误时保持 `provider_failed`，不伪造具体原因。官方错误码中的 `Throttling.ResourceExhausted` 指服务繁忙，而裸 `ResourceExhausted` 属于请求限额，不能混为同一种原因。状态查询与下载仅在原任务上有界重试，付费提交不会自动重复；网络或超时等不确定提交继续保留云端名额，直到核实终态。

## 官方资料

- [Qwen Image 生成与编辑 API](https://help.aliyun.com/en/model-studio/qwen-image-generation-and-editing-api-reference)
- [Wan 3.0 视频生成 API](https://help.aliyun.com/en/model-studio/wan3-video-generation-api-reference)
- [百炼模型限流说明](https://help.aliyun.com/zh/model-studio/rate-limit)
- [百炼错误码](https://help.aliyun.com/en/model-studio/error-code)

接口核对日期：2026-11-11。模型权限、账号配额与端点以对应区域控制台和官方文档为准。
