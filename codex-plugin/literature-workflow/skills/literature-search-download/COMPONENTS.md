# 组件与固定版本

- `literature-workflow 0.1.1`：统一配置、任务记录、自动 CSV 交接、回填及恢复。
- `paper-acquisition 0.1.0`：literature_gain 工作区源码快照；仅增加可选新运行目录参数以在检索前持久化恢复信息，更新 agent 指引为本 skill。原检索、筛选、下载、核验、库和分类逻辑保留。
- `scanpdf-portable 0.1.0`：复用上一版 ScanSci 下载路径和 Elsevier 必要的内部实现，不依赖外部 InstSci 包。修复 Windows 状态文件短暂占用导致心跳线程退出、心跳过期时误起第二个浏览器，以及 SI 候选误收期刊卷期导航；出版社正文取件流程与固定内核保留。
- Camoufox SDK 0.5.6，固定内核 152.0.4-beta.30；CloakBrowser SDK 0.5.10，固定 Chromium 146.0.7680.177.5；其他 Chromium 路径使用本机 Chrome。
- Python 依赖完整约束见 constraints-windows.txt；首次安装通过 PyPI 获取，面向 Windows x64 Python 3.11。

上游许可保存在 licenses，wheel 自身包含源码及许可。浏览器二进制不随本公开包分发，由使用者从官方源取得。程序不会自动升级固定内核。

分发包仅含本项目可分发的集成程序和文档，不含开发者配置、API 凭据、学校会话、全文或聊天记录。每个文件的大小与 SHA-256 见 FILES.sha256.json。
