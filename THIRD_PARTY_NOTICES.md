# 开源来源、修改和许可证

本项目是在既有检索与下载系统上做的集成，不是从零编写下载引擎，也不是上游项目的官方版本。

| 来源 | 本项目如何使用 | 许可证与版本说明 |
|---|---|---|
| [literature_gain](https://github.com/cmTTT711/literature_gain) | 复用检索、去重筛选、来源证据、正文/附件 CSV、共享库、身份核验、发布和分类代码 | 此仓库公开的检索组件快照随本项目按 AGPL-3.0-or-later 提供源码 |
| [Rimagination/scansci-pdf](https://github.com/Rimagination/scansci-pdf) | 内嵌出版社策略、浏览器后端、CARSI 和附件发现；经 ScanPDF Portable 包装调用 | 本地 1.17.0 快照标注 Apache-2.0，保留原 Apache 文本；上游后来的 MIT 版本不替换此快照的许可证 |
| [Rimagination/instsci](https://github.com/Rimagination/instsci) | 仅复用内置 Elsevier 常驻会话、查看器下载与响应捕获所需的实现 | 0.1.1 来源快照，MIT；保留原版权与许可证。不要求安装或启用外部 InstSci 插件 |
| [Camoufox](https://github.com/daijro/camoufox) | RSC 等站点的独立浏览器 | SDK 0.5.6（MIT）；固定浏览器 152.0.4-beta.30（MPL-2.0 及 Firefox 组件许可），由用户从官方地址下载 |
| [CloakBrowser](https://github.com/CloakHQ/CloakBrowser) | Elsevier 固定 Chromium 后端 | SDK 0.5.10 为 MIT；146.0.7680.177.5 浏览器另有二进制许可，禁止随包再分发，因此本仓库和 Release 不含此二进制 |
| [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) / [Playwright](https://github.com/microsoft/playwright-python) | 其他出版社的浏览器控制 | Apache-2.0；版本见固定依赖清单 |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) / MuPDF | PDF 解析、身份核验辅助 | AGPL-3.0 或 Artifex 商业许可；本项目使用开源方案，提供全部集成源码，不将其误标为 MIT |
| [pypdf](https://github.com/py-pdf/pypdf)、HTTPX、Requests、Pydantic 等 | PDF 与 HTTP/数据处理 | 各自许可证见 THIRD_PARTY.json 及官方分发元数据 |

完整依赖版本、项目链接和许可证元数据列在 [THIRD_PARTY.json](THIRD_PARTY.json)。该清单由实际依赖 wheel 的官方元数据生成；第三方依赖和浏览器二进制在首次安装时由用户直接从 PyPI 或官方源获取，不通过本仓库重新分发。

## 本项目的修改

- 新增统一配置、逐步学校/API 引导、任务状态和自动 CSV 交接；沿用原 DOI、target_path/target_dir、身份核验、共享库与分类协议。
- 检索前保存任务恢复记录；给原检索工作流增加可选运行目录参数，更新交接说明。
- 为 ScanPDF 增加 Windows 原子状态写入重试、心跳线程容错和存活进程保护；排除附件候选中的 ScienceDirect 卷期导航。
- 固定原出版社后端；按出版社复用上下文，每篇独立页面。未改成依赖外部 InstSci 插件。
- 统一正式项目布局、项目索引和说明；把测试环境与正式成果分开。
- GitHub 公开版改为安装时从官方获取固定依赖与浏览器；删除个人验证经历和本机目录说明。

本项目的原始集成代码按根目录 LICENSE（AGPL-3.0-or-later）提供。独立的 Apache/MIT 等上游文件保留各自原许可证与版权声明，不声明全部代码由本项目独立创作，也不授予上游专有浏览器二进制的再分发权。

## 上游许可与源码

- scansci-pdf：licenses/scansci-pdf-Apache-2.0.txt、licenses/scansci-Apache-2.0.txt（历史嵌入路径保留的许可副本）。
- InstSci 子集：licenses/elsevier-engine-MIT.txt，原版权声明不变。
- Camoufox 源码与 MPL 文本：[官方仓库](https://github.com/daijro/camoufox)，固定浏览器出处在运行时锁文件。
- CloakBrowser：[SDK 许可](https://github.com/CloakHQ/CloakBrowser/blob/main/LICENSE)、[二进制条款](https://github.com/CloakHQ/CloakBrowser/blob/main/BINARY-LICENSE.md)。
- PyMuPDF：[官方源码](https://github.com/pymupdf/PyMuPDF)与[许可说明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)。
