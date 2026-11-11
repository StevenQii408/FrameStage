# FrameStage

FrameStage 是本机运行的视频创作工作台。AI 辅助分镜可以从参考视频提取镜头，也可以根据文字剧本规划分镜。你可以在项目画布上编排连续事件，编辑首帧图片和 Seedance 视频提示词，预览生成的素材。

**当前为单用户开发预览版。**视觉和剧本分析默认使用百炼北京区域 `qwen3.8-flash`；本地负责媒体处理、证据工具和数据保存。图片／视频生成已提供千问 Image 3.0 Pro 与 Wan 3.0 官方接口适配器，默认关闭；配置和显式启用后才能提交真实生成。配置步骤见 [生成 API 指南](docs/GENERATION_API_SETUP.md)。

## 主要功能

- **参考视频解析：**PySceneDetect 提取物理切点，FFmpeg 抽取带时间戳的帧；静态事实、动态动作及画风分别分析。长来源镜头用内部窗口取证，不再按 4.8／5 秒强制切碎。
- **事件优先编排：**受控 Agent 根据相邻镜头和只读工具证据判断连续事件，保留来源镜头与切点；生成片段提供独立的完整事件 Prompt。原片时间与建议生成时间分别保存，生成时长按半秒向上取整。
- **剧本导演规划：**区分人物设定、动作、对白与声音；人物介绍不独立成镜，对白由本地按原文 ID 无损提取。摄影补全与原文事实分开记录。
- **画风分析：**根据可见证据与本地精选知识库输出具体画风描述，保留不确定性及可选创作配方；默认流程不依赖 Embedding，也不自动升级模型。
- **项目画布：**项目首页、独立画布、事件叠图、来源展开、镜头列表、时间轴、全局画风、Prompt 编辑、类型化连线和撤回。详情按需打开，不占用常驻右侧栏。
- **媒体与生成底座：**原片播放器标注真实切点；资产节点双击原位放大，视频支持截取首帧／当前帧／尾帧。生成任务具备 SQLite 队列、输入快照、幂等键、项目级 SSE、重启续查、取消和历史版本规则；云端状态未知时不会自动重复提交付费请求。

## 快速开始

需要 Python 3.12+、Node.js 22，以及 PATH 中可运行的 `ffmpeg`／`ffprobe`。发行版 CI 使用 Python 3.12／3.13。首次运行先获取源码；已有本地副本可直接使用原目录。

```bash
git clone https://github.com/StevenQii408/FrameStage.git
cd FrameStage
```

以下命令从仓库根目录执行。

安装 FFmpeg，例如 macOS 使用 `brew install ffmpeg`，Ubuntu 使用 `sudo apt-get install ffmpeg`。

**终端 1：安装并启动后端。**

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
python -m app.cloud_setup
python -m app.start_cloud
```

`app.cloud_setup` 只需首次运行：隐藏输入 API Key，保存至 Git 忽略的私有 `backend/.env`。以后进入后端虚拟环境后运行 `python -m app.start_cloud` 即可。不要把 Key 写进前端或提交到仓库。

**终端 2：安装并启动前端。**

```bash
cd front
npm ci
npm run dev
```

打开 **http://127.0.0.1:5174/**，创建项目后双击空白画布添加输入节点。后端默认地址为 `http://127.0.0.1:8000`；端口占用时应先关闭旧服务，前端不会自动漂移到其他端口。

**没有 API Key 时可使用 Mock 验证交互。**在已激活后端虚拟环境的终端运行以下命令，替代云端启动命令：

```bash
STORYBOARD_PROVIDER=mock python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Mock 是显式开发替身，结果不代表模型解析质量。本机会话保护仍开启；网页自动取得会话令牌。CLI、非默认端口及安全边界见 [SECURITY.md](SECURITY.md)。

## 实现与数据边界

```text
视频 → 真实切点／来源镜头 → 分窗视觉观察 → 事件 Agent → 生成片段与 Prompt
剧本 → 类型化原文与对白 ID → 导演窗口规划 → 分镜、事件与 Prompt
                         ↓
              SQLite／持久事件／帧证据与版本
                         ↓
             项目画布／镜头列表／双时间轴
```

- 后端：FastAPI、Pydantic、SQLite、FFmpeg、PySceneDetect、HTTPX；本机串行 Worker，云端只使用已配置的 Flash。
- 前端：Vue 3、TypeScript、Pinia、Vue Flow 和按场次／事件分支计算的画布布局。播放器、详情与编辑器按需加载；本地图片 Blob 使用 IndexedDB。
- Agent：有限状态流程、工具白名单、调用预算、证据与来源校验、局部纠错、检查点及失败保留。模型输出先校验，再提交业务状态。
- 任务结果 JSON **不是完整项目备份**：不包含浏览器本地 Blob，也不等同全部原视频和生成资产归档。
- 旧任务保留原始策略和模板版本，不因代码更新静默重算。删除画布节点、取消任务、项目回收与物理素材清理是不同操作。
- 视频上传限制、生成模型能力和时长参数由后端再校验。来源镜头没有固定 5 秒上限；生成服务商的时长能力是另一层约束。Wan 3.0 生成节点支持 2–30 秒整数、480P／720P／1080P；图片按画幅显示合法像素尺寸。

## 开发与验证

参见 [CONTRIBUTING.md](CONTRIBUTING.md) 安装哈希锁定的开发依赖。常规检查不需要 API Key，也不运行付费验收：

```bash
cd backend
python -m pytest -q -p no:cacheprovider
python -m app.style_library check
cd ../front
npm run typecheck
npm test
npm run test:layout
npm run build
```

结构、状态机和 Mock 回归通过，并不等于真实画风或剧情准确率达标。真实质量评测仍需自有／授权素材与独立人工标注，不能用模型预测自动充当真值。参见 [评测说明](backend/evals/README.md)。

## 文档与许可

- [后端安装、接口与运行约束](backend/README.md)
- [前端交互与浏览器回归](front/README.md)
- [提示词规范与画风知识库](Prompts/cinema_style_library/README.md)
- [安全与部署边界](SECURITY.md)、[贡献指南](CONTRIBUTING.md)、[变更记录](CHANGELOG.md)
- [发布验证](docs/RELEASE_VALIDATION.md)：检查提交、源码包和干净安装。

项目自行编写的代码、文档及画风知识内容使用 [MIT License](LICENSE)。第三方代码、品牌名称、模型服务、用户上传素材和演示媒体各自适用其许可；本项目许可证不授予这些素材的再分发权。来源参考保留在知识库及历史记录中。默认发布包排除私有数据、付费响应报告和尚未核实许可的展示 PNG。
