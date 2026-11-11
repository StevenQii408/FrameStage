# Cinema Style Library

本目录是电影视觉风格知识的维护源：视觉术语、完整配方、区别与问答、来源记录分开保存。

运行时仅使用编译后的少量候选，不将整库注入模型，不启用 Embedding。观察事实、视觉参考锚点和生成约束互不替代。

## 文件与维护

- `terms/*.json`：术语定义、同义备选条件、反向线索、全局/镜头/知识作用域。
- `recipes/*.json`：具体配方、跨维度鉴别组、近似风格区别、精简及详细描述。
- `knowledge/*.json`：区别、适用条件和问答，为未来知识库提供结构。
- `sources.v1.json`：来源、授权、审核日期与未解决的版本记录。
- `taxonomy.v1.json`：分类边界与覆盖目标。
- `schemas/library.schema.json`：从Pydantic生成的正式JSON Schema。
- `CATALOG.md`：生成的配方与问答总览，供审阅，不直接注入模型。

JSON是唯一维护源，生成文件不要手改。正式运行从后端包内 `app/style_direct/cinema_library.v1.json` 读取，部署不依赖仓库的Prompts目录。

## 离线构建与检查

```bash
cd <repo-root>/backend
python -m app.style_library build
python -m app.style_library check
python -m app.style_library search '剪纸' --section recipes
python -m app.style_library search '胶片' --section knowledge
```

以上命令不访问网络、不读取API Key、不调用Embedding或Flash。修改源文件后重新build并重启后端；check会检查关系并比较编译哈希。将来问答检索返回条目和来源，不自行修改镜头或Prompt。

## 新任务识别规则

1. Flash记录带图片编号的视觉事实；人物内容和摄影信息仍在独立字段。
2. 按媒介筛选，在同一维度的单条事实内匹配完整视觉关系。positive_cues外层为AND，组内同义备选为OR。
3. 至多提供6个紧凑候选，包含区别性条件而非整段生成模板；重复同义词不重复计分。
4. 非完整组合的候选明确标为review，不自动选择；具体名称需要feature_support逐组引用真实证据。
5. 反向线索、否定语境、相近配方同时成立或证据不足时保留具体描述，不硬套名称。
6. 只以被观察支持的特征生成60–100字标准版，详细观察单独保存，不复制整套配方来填补缺口。

保持Qwen3.8-Flash、原有两阶段和最多一次补证据预算；不新增固定请求，不恢复Embedding。旧24个锚点ID继续兼容读取，旧任务不静默重算。

## 版权与来源

本次中文条目自行编写，外部仓库只用于分类和描述结构研究。MIT源已核实，但上游SHA的只读查询因网络未成功，来源记录明确写“unverified”，不声称已固定commit，也未导入实质原文。未来复制或翻译实质内容前必须先锁定commit、追溯上游许可并保留相应版权/许可，第三方图像另行审核。

MJ许可未核实、Fooocus GPL、ASC/Kodak文章均不做原文搬运。Met仅明确OA的作品与数据可按其CC0政策使用，Wikidata的CC0只涵盖指定结构数据。没有自动下载影视截图或参考图。

## 验收边界

本地Schema、检索、否定、内容隔离、预算和兼容测试证明结构和规则有效，不等同真实视觉准确率。真实Flash盲测需要自有/授权素材和独立人工标注；不能拿库内描述生成的测试句宣称模型已达到影视理解准确率。
