# Third-party references

源码包不捆绑 node_modules。Vue、Vue Flow、GSAP 等依赖由 npm 按锁文件安装，分别适用各包自带的许可证；项目 MIT 许可证不覆盖这些依赖。

## 2026-11-11 星空生成进度与行内参考图

星空效果参考 [TheodorStorm/starfield](https://github.com/TheodorStorm/starfield) 的轻量 Canvas 星空纵深表现，其 [MIT 许可证](https://github.com/TheodorStorm/starfield/blob/main/LICENSE) 已核实。共享帧调度、粒子精灵、进度显示和生命周期控制均由本项目自行实现，没有复制上游实质源码或加入运行时依赖。行内参考图采用本项目 Vue DOM 编辑器与 Teleport 预览；仅参考用户提供的 LibTV 交互截图，不复制其私有源码或媒体。

生成百分比优先使用真实 Provider 进度；服务商未返回百分比时，仅在 UI 显示明确标注的阶段与时间估算，不写入后台任务、SSE 或资产历史。估算不代表预计完成时间，只有实际成功事件显示 100%。

## 2026-11-10 上传资产与生成历史交互

参考 [Vue Flow Guide](https://vueflow.dev/guide/) 中的 Custom Nodes 与 NodeToolbar 组织方式：节点继续参与现有画布，局部操作与媒体内容分开呈现；参考 [shadcn/vue Carousel](https://www.shadcn-vue.com/docs/components/carousel) 的媒体集合、当前项和切换交互。本轮历史使用本仓库自行编写的 Vue/CSS 叠层与列表，不复制上述组件源码，不安装 Carousel／Embla、registry 或新增 UI 依赖。既有 Vue Flow 依赖不变，媒体和 Prompt 来自用户资产及项目结果。

## 2026-11-10 节点编辑交互参考

紧凑 Prompt composer 同时参考 [shadcn/vue Prompt Input](https://www.shadcn-vue.com/docs/ai-elements/prompt-input) 的附件条／文本区／底部工具栏结构；其仓库为 [MIT](https://github.com/unovue/shadcn-vue/blob/dev/LICENSE)。本项目采用自有 Vue/CSS 实现，没有导入 registry、Tailwind、React 或富文本运行时。

本轮参考 [wubuku/liblib-tv](https://github.com/wubuku/liblib-tv) 的单节点下方编辑卡、单输入／输出端口，以及 [santos-1664/libtv-studio](https://github.com/santos-1664/libtv-studio) 的项目组织交互；两者为非官方 MIT 项目。实现为本仓库自行编写的 Vue 组件，没有引入 React 运行时、复制上述源码、Logo 或媒体资产。官方 [libtv-skills](https://github.com/libtv-labs/libtv-skills) 是 Agent/API 工具，未作为前端代码集成。浮层采用当前 Vue Flow 坐标和受控 Teleport；没有新增 UI 运行时依赖。

本项目没有直接复制下列仓库的 Python/ComfyUI 实现或完整提示词模板；仅提炼输入结构、术语体系和版本化方法。若后续引入实质性源码或模板原文，应把对应 MIT 许可证和版权声明加入 `NOTICE`。

| Reference | Fixed revision | Applied idea |
| --- | --- | --- |
| [VigoZhao/AI-Visual-Prompt-Cookbook](https://github.com/VigoZhao/AI-Visual-Prompt-Cookbook) | `d5e971797f956def11344eb036a18b80b23044ad` | versioned schema, variables, style anchors, avoid rules and validation |
| [zhangchenchen/awesome_sora2_prompt](https://github.com/zhangchenchen/awesome_sora2_prompt) | `9e797e75506d384f96c75e94527b06a001bcf1e4` | subject, action, environment, framing and aesthetics vocabulary |
| [dagthomas/comfyui_dagthomas](https://github.com/dagthomas/comfyui_dagthomas) | `469fdd62f4c78bfd37dfc7b900ecf69537dffa3f` | ordered multi-frame input, even sampling and model reuse |

生产 Prompt 的主要内容来源仍是本仓库 `Prompts/` 中的本地镜头规范。

## Cinema Style Library（2026-10-02）

本轮术语、配方与问答为自行编写的中文内容，不导入外部模板原文或图片。来源与许可边界见 `Prompts/cinema_style_library/sources.v1.json`。参考包括 MIT 授权的 photographic-styles-and-wildcards-for-Krea-2、cinematic、awesome-ai-visual-realism-prompts，以及 ASC、Kodak 的专业视觉说明。

上游 commit 的只读查询未成功，来源 revision 明确标为 `unverified`，不伪造固定 SHA；本库自身以源 JSON 哈希锁定版本。未来复制/翻译实质内容之前仍需固定上游版本、追溯原作许可，并保留版权声明。MidJourney 参考库许可未核实、Fooocus 为 GPL，只做分类研究。Met CC0 仅限其明确 OA 的作品与数据，Wikidata CC0 仅限指定结构数据；网站其余文字不视为自由授权内容。本轮没有自动下载第三方媒体。
