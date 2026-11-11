# FrameStage Backend

FastAPI + SQLite + 单任务 Worker。FFmpeg／PySceneDetect 在本机处理媒体，百炼北京区域 `qwen3.8-flash` 执行视觉与文字推理；没有本地 MLX 推理入口，也没有自动升级模型。

## 安装与启动

发行版使用 Python 3.12+，需要 PATH 中的 `ffmpeg`、`ffprobe`。从仓库根目录执行：

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
python -m app.cloud_setup
python -m app.start_cloud
```

配置向导只需首次运行，私有 Key/Host 保存在 Git 忽略的 `backend/.env`，读取不依赖终端当前目录。已有 `.venv312` 安装可以继续使用该环境中的 `python -m app.start_cloud`。升级依赖时按锁文件安装，不复用已退役的 Python 3.9 依赖集。

默认监听 `127.0.0.1:8000`。前端固定使用 `127.0.0.1:5174`，在另一个终端按根 README 启动。只确认配置是否齐全的启动健康检查不会证明云端鉴权成功；`python -m app.cloud_setup --check` 是显式低额度联网验证，会调用 API。

无 Key 开发流程须明确使用 Mock：

```bash
STORYBOARD_PROVIDER=mock python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`app.start_cloud` 固定使用云端 Flash；不把 Mock 或旧 MLX 环境变量当作生产默认。Mock 的示例内容不代表解析质量。

## 当前处理链路

**视频：**上传与容量检查 → 原片元数据与真实切点 → 来源镜头及帧 → 受限窗口感知 → 视觉合同／两类 Prompt → 连续事件 Agent → 生成片段与完整事件 Prompt。

物理来源镜头没有固定 4.8／5 秒上限。长来源在内部 SourceSpan 中分窗取证，不因此增加画布来源卡或伪造切点。真实原片 PTS、建议表演时间、生成时间分别保存；生成时长按半秒向上取整，不改原片时间。

感知先做静态和动态分析，字段由 Pydantic 定义及局部 wire 协议约束。安全归一化只修已知结构差异，不为裸字符串补造帧证据；必要时对失败字段纠错一次，与补证据共享追加请求预算。动态无法成立时保留有效静态观察并标记动态待复核。

事件 Agent 根据已保存的事实、邻镜与受限工具证据组织连续事件和动作阶段。来源覆盖、顺序、时间、帧身份及可用事实经过校验后才提交。重要特写仍可保留在事件内部，多个微动作不机械均分成四段。单动作自然概括，多阶段才给必要时序。

**剧本：**原文 → 人物／场次／动作／对白／声音的类型化片段 → 导演窗口规划 → 原文与连续性校验 → 分镜及事件片段。

人物介绍作为共享设定，不独立成镜；对白通过本地 ID 无损提取，不由模型重新撰写或计算字符范围。摄影与站位等创作补全独立标注。剧本时间是建议表演时间，不是实测视频时间；纯剧本没有伪造的帧证据、图片或音轨。

**画风：**默认 `qwen-direct-v3`，基于代表帧／可见特征与精选知识库做受控观察及转译；存在歧义才按预算取证。不依赖 Embedding，不把人物服装、道具或局部特效自动写成全局风格。用户选择库内配方是明确的创作修改，不改写原片观察。历史向量／V1 结果保留兼容读取。

## 运行边界与取消

常用配置见 `.env.example`，生产配置保存在私有 `.env`：

| 配置 | 默认值／意义 |
| --- | --- |
| `STORYBOARD_PROVIDER` | `bailian`；`mock` 仅显式开发使用 |
| `STORYBOARD_CLOUD_MODEL` | `qwen3.8-flash` |
| `STORYBOARD_MAX_VIDEO_SIZE_MB` | 2048 MB |
| `STORYBOARD_MAX_VIDEO_DURATION_SECONDS` | 600 秒 |
| `STORYBOARD_JOB_MAX_MODEL_CALLS` | 424；整任务模型请求次数上限，包含重试尝试 |
| `STORYBOARD_JOB_TIMEOUT_SECONDS` | 3600；整任务执行期限，阶段仍有更小预算 |
| `STORYBOARD_MAX_PENDING_JOBS` | 8；运行中与排队中的分析任务合计上限，满额返回 HTTP 429 |
| `STORYBOARD_CLOUD_CONNECT_TIMEOUT_SECONDS` | 8；网络握手上限 |
| `STORYBOARD_CLOUD_READ_TIMEOUT_SECONDS` | 75；读取空闲上限，不能替代总执行期限 |
| `STORYBOARD_CLOUD_TRUST_ENV` | `0`；环境代理仅按需显式开启 |

最多 120 个来源／规划镜头；超出容量明确报错，不静默截断。基础关键帧最长边 768px，优先取来源内 10%／35%／65%／90% 的有序帧，按需工具有独立补帧／裁剪限制。串行策略不等于无限等待，网络、工具与媒体子进程受执行上下文约束。

上述调用与时间预算不是人民币硬账单限制；Token、费用估算、未知用量及请求状态分别记录。分析队列准入统计运行中与等待中的任务，默认合计最多 8 个，不能将单 Worker 理解为无限排队。已经发送的请求可能已经计费。取消要求停止后续请求与发布，不承诺服务商退款。重启中断不会自动重发付费操作，恢复需要显式发起，并按历史版本和检查点判断是否可续跑。

## 本机 API 与认证

写入接口要求本机会话令牌，CLI 不能直接使用无认证的 `curl -X POST`。浏览器从合法 Origin 自动 bootstrap `GET /api/session`，在 `X-FrameForge-Session` 请求头发送内存中的令牌。CLI 使用单独配置的 `STORYBOARD_LOCAL_API_TOKEN`；详情与示例见 [安全说明](../SECURITY.md)。该令牌不等于百炼 Key。

| 路由 | 用途 |
| --- | --- |
| `GET /api/health` | Provider、配置和运行健康，无密钥正文 |
| `GET /api/session` | 受信任浏览器／已认证 CLI 初始化本机会话 |
| `/api/projects` | 项目、版本化画布、回收站 |
| `POST /api/jobs` | 表单创建 `video` 或 `script` 分析任务 |
| `GET /api/jobs/{id}/result`、`/export` | 兼容 `storyboard.v1` 结果／JSON 导出 |
| `GET /api/jobs/{id}/events` | 持久事件、游标及 SSE 进度 |
| `GET/HEAD /api/jobs/{id}/video` | 原片播放，支持 Range |
| `GET /api/jobs/{id}/frames/{frame_id}` | 来源及工具证据帧 |
| `/api/jobs/{id}/shots/{shot_id}/perception` | 显式恢复、复核与修订记录 |
| `/api/jobs/{id}/event-agent/runs` | 有预算的独立事件实验与审计 |
| `/api/style/analyses` | 图片／帧画风分析、版本及诊断 |
| `/api/generations`、`/api/generation-assets` | 生成任务与参考资产底座 |

任务和媒体请求可带 `X-Project-ID` 或 `project_id`。项目隔离用于本机单用户工作台，不是多租户鉴权。API Schema 可在本机 `/docs` 查看；写入仍须携带会话头。

图片／视频生成提供 `qwen-image-3.0-pro` 和 `wan3.0-video` 百炼适配器。默认 `STORYBOARD_GENERATION_ENABLED=0`；未启用的任务保存为 `blocked/awaiting_provider`，不会产生服务商请求。启用后由 SQLite 队列分阶段提交、查询、下载及校验，按项目共用 SSE。已有远端任务 ID 的任务恢复查询；无 ID 且提交结果未知的任务不自动重提。默认在途总上限 16、等待队列 100，查询间隔 15 秒，实际速率还受账号配额限制。完整配置见 [生成 API 指南](../docs/GENERATION_API_SETUP.md)。Wan 3.0 新任务支持 2–30 秒整数和480P／720P／1080P；图片使用按画幅过滤的像素尺寸，均由后端能力目录提供。

## 存储、兼容与维护

数据库和原片／帧存于 `STORYBOARD_DATA_DIR`，默认 `backend/data`。浏览器图片 Blob 另存 IndexedDB；任务结果导出不包含完整项目媒体备份。不要把生产数据和付费原始响应加入 Git。

旧任务继续保留原 Provider 名称、策略、模板、观察和原 Prompt；读取不自动调用模型、不静默更新结果。历史兼容代码不等于仍支持启动本地 MLX。需要复核或重规划时使用显式操作并保留修订历史。

画风源位于 `Prompts/cinema_style_library/`，编译产物打包在应用中。检查与重建均为本地操作，不购买 Embedding 或模型推理：

```bash
python -m app.style_library check
python -m app.style_library build
```

## 验证

开发依赖用 `requirements-dev.lock` 安装，参见 [贡献指南](../CONTRIBUTING.md)。常规测试无需 Key：

```bash
python -m pytest -q -p no:cacheprovider
python -m app.style_library check
```

真实模型验收必须显式选择收费入口，使用外置授权素材与独立人工标注；不能把结构通过率当作画风、动作或事件理解的准确率。参见 [评测说明](evals/README.md)。历史实现记录移至 `docs/history/`，其旧规则不代表当前默认。
