# 验证源码发布

发布对象是单用户本地工作台的源码。公开的 Git 树和源码 ZIP 应相同；ZIP 另带包含提交号、文件大小与 SHA-256 的 RELEASE_MANIFEST.json。私有配置、数据库、媒体、真实模型响应和展示截图不随源码分发。

1. 在候选提交运行 `python3 scripts/release_source.py --verify-head` 和 `python3 scripts/secret_scan.py --history`。新增的公开源码如未提交，也会阻止打包。
2. 在干净检出中按 [开发环境](../CONTRIBUTING.md) 安装依赖，运行后端 pytest（Python 3.12、3.13）、画风库校验、前端 Vitest、实际画布布局回归和生产构建。浏览器联调按 [前端说明](../front/README.md) 启动独立 Mock 后端，不连个人数据库。
3. 从 backend 构建 wheel。发行包应包含 app、evals 的模块、运行用 JSON 和 MIT 许可证，不包含 tests、媒体、.env 或构建机的私有数据。
4. 从仓库根运行 `python3 scripts/release_source.py --output release-artifacts/frameforge-source.zip`。提交发布前可用 `python3 -m zipfile -t release-artifacts/frameforge-source.zip` 检查 ZIP 完整性。

无 Key 回归验证的是实现、协议与存储行为，不验证真实付费账户权限、供应商可用性或镜头／风格识别准确率。
