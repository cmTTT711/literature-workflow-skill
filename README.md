# Literature Workflow · 文献检索与下载 Skill

Windows 上从主题检索到正文下载、核验归档和分类的统一 skill。支持补充材料尽力获取、机构访问与断点续跑。完整复用 literature_gain 和内置 ScanPDF 的检索、清单与出版社通路；外部 InstSci 插件不是依赖。

## 下载和安装

从 [Releases](https://github.com/cmTTT711/literature-workflow-skill/releases/latest) 下载：

- `literature-search-download-0.1.1-win-x64.zip`：通用本机 agent skill。
- `literature-workflow-codex-0.1.1-win-x64.zip`：Codex 插件目录包。
- 对应 `.sha256` 文件用于核对下载完整性。

需要 Windows x64、Python **3.11 x64**、本机 Google Chrome，以及首次安装时能访问 PyPI/GitHub/浏览器官方源的网络。解压到有写权限、路径较短的目录，优先选择非系统盘。

```powershell
& 'D:\Skills\literature-search-download\scripts\install.ps1' -Python 'C:\Path\To\Python311\python.exe' -DataHome 'D:\LiteratureData'
```

包内提供集成组件源码 wheel；第三方 Python 依赖和固定浏览器由安装脚本从官方源获取。**不含浏览器二进制、Python 安装程序、Chrome、API key、学校账号或登录态**。CloakBrowser 二进制禁止随包再分发；首次官方安装不等于自动升级，固定版本保持不变。

普通 agent 将完整 skill 文件夹放入其支持的 skill 目录，并读取 SKILL.md。agent 需要本机文件、PowerShell/Python 和可见浏览器执行能力。Codex 目录包使用根插件描述；仓库另提供 `.agents/plugins/marketplace.json` 相对路径市场入口。各平台的注册方式以平台支持为准，不把上传文件当成已自动注册插件。

## 第一次使用

让 agent 读取 [首次配置](skill/literature-search-download/references/first-use.md)，逐步配置你自己的：

1. 学校官方全名、真实联系邮箱、校园网/CARSI/学校官方机构入口。
2. 检索来源和所需 API key；可以先配置 OpenAlex，再添加 Scopus、Semantic Scholar、Lens 或 Web of Science。
3. 成果保存目录；用自己学校有权访问的小样本完成首次浏览器认证和下载验证。

凭据仅在本机输入或编辑本机 credentials.env。换电脑后需要重新完成本人认证。学校账号、验证码和 API key 不应发到聊天或提交 Git。

## 使用流程

```powershell
& '<skill目录>\scripts\workflow.ps1' run --topic 'quantum materials' --year-from 2024 --year-to 2026 --max-results 20 --providers openalex --label '量子材料'
& '<skill目录>\scripts\workflow.ps1' resume --job '<任务编号>'
```

原多源检索与自动下载 → 自动读取正文/附件清单 → 原出版社浏览器取件 → 核验 → 回填共享库 → 发布与分类。正文优先交付，附件未取得保留待办。网页验证、订阅权限、API 限流仍可能需要本人处理，程序不会保证所有论文都能获得。

正式项目统一保存在所选工作区 results，入口为项目清单.md；每个项目都包含项目说明、pdfs、attachment、classifications 和 agent_tasks。测试/验收使用独立配置与工作区，不混入正式项目。

## 隐私与开源来源

此仓库是独立、无历史记录的公开发布副本，不包含开发者配置、学校身份、凭据、浏览器 profile/cookies、聊天记录、论文、下载台账或本机迁移记录。公开学校名称表属于上游通用目录，不代表预置了任何人的学校配置。

本项目并非全部从零开发。主要来源、修改范围、版本和许可证请阅读 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；完整依赖清单见 [THIRD_PARTY.json](THIRD_PARTY.json)。原始集成代码使用 AGPL-3.0-or-later，上游 Apache/MIT/MPL 等许可按组件保留。

## 从源码验证

使用 Python 3.11 建立独立环境。先安装 vendor/literature-gain、vendor/scanpdf 和本项目，再执行各自的测试目录。`tools/build_public.py` 从公开源码构建三个 wheel 和两个发布包；不扫描或复制任何个人数据目录。
