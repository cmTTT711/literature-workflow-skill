---
name: literature-search-download
description: 在 Windows 上按主题检索学术文献，自动下载主 PDF 与补充材料，核验、归档并分类。首次使用引导配置自己的学校、学术 API 与机构访问；支持中断续跑。
---

# 检索到下载

使用随包的统一入口 `scripts/workflow.ps1`。保留 literature_gain 的检索、去重、筛选、共享库、清单路径、核验、渠道和分类逻辑，自动将它生成的正文和附件清单交给内置 ScanPDF。无需用户手动转交 CSV。

## 首次使用

先阅读 [学校与首次配置](references/first-use.md)，按步骤引导用户。一次说明当前一步需要的信息，不要把全部配置问题一次抛给用户。先检查本地状态，已有有效配置直接复用。

1. Windows x64、Python 3.11 x64、本机 Google Chrome。先确认安装和数据磁盘，有非系统盘时优先选择；运行 `scripts/install.ps1 -Python <python.exe> -DataHome <所选磁盘的短数据目录>`，大型内核与会话随数据目录存放。首次安装从 PyPI 与官方浏览器源获取固定依赖和内核；Python 和 Chrome 需本机安装。
2. 运行 `scripts/workflow.ps1 guide`。引导选择检索源，再按 [API 配置](references/api-keys.md) 逐项配置启用源的凭据。密钥在本机输入或编辑本机 credentials.env，聊天里仅报告是否配置。
3. 用 `setup` 保存用户自己的学校全名、真实联系邮箱、校园网/CARSI/官方机构入口、短成果目录和检索源。不要默认使用任何学校，也不要复制开发者的账号、会话或配置。
4. `doctor` 检查依赖；`browsers` 检查固定内核。选择学校有权访问的样本验证。需要学校认证时用 `login --doi ... --wait-for-confirmation` 打开独立会话；本人登录后运行 `confirm-login REQUEST_ID`，等待原进程结束。账号密码和验证码只在本机浏览器输入。
5. 首次检索先设小预算，检查主件及附件结果后再按用户要求扩展。

## 正常运行

读 [运行与恢复](references/workflow.md)。主题、年份、筛选条件和检索预算沿用原 SearchProfile。例如：

```powershell
& '<skill目录>\scripts\workflow.ps1' run --topic 'magnetoelectric coupling' --year-from 2023 --year-to 2026 --max-results 100 --providers openalex,scopus --label magnetoelectric
```

也可 `run --profile <profile.json>`，使用 [示例](assets/search-profile.example.json)。默认包括附件，用户明确不要附件时才加 `--no-si`。预算是每个来源的上限，不代表保证下载数量。分类规则可通过 `--classification-rules` 传入原项目规则 JSON。

保存输出的任务编号与结果目录。`status --job ID` 查看，`resume --job ID` 增量恢复，确需再次访问已失败的下载项时加 `--retry-failed`；`--offline` 只做本地回填发布。不得为刷新进度反复新建检索或无限重开验证页面。

正式成果统一保存在所选工作区的 `results`；工作区 `项目清单.md` 是正式项目入口。用 `--label` 提供简洁、易懂的项目主题名，文件夹沿用“主题_年份_创建时间_标识”，正文、附件、分类和清单使用同一布局。调试或验收必须先配置独立的 `--home` 与非正式工作区，不把测试项目或临时样本混入正式 results。详见[结果与恢复](references/workflow.md)。

## 必须保留的行为

- 正文使用原 `agent_required.csv` 的 `target_path`，附件使用原 `attachment_required.csv` 的 `target_dir`，由原项目导入、核验并发布到 automatic/agent/all 和分类目录。不要重新命名接收路径或把 SI 当正文。
- 正文新下载须通过 ScanPDF 的 DOI/标题核验，随后通过原库核验；错误件留在隔离区，不手工篡改状态充数。附件记录来源和哈希。
- 附件 `zero` / “未发现链接”保持待确认，不能推定“这篇没有 SI”；部分成功保持部分状态。`FINISHED_WITH_PENDING` 表示仍有未解决项，应如实报告原因。
- 本文工作流默认尽力获取附件。用户只关注正文时，先交付已核验正文；附件未完成单独说明，不为补齐附件反复重试或延迟正文交付。
- 复用已验证的出版社路径：Elsevier 使用包内原 CloakBrowser 146 常驻会话与响应捕获；RSC/PNAS/JPSJ/AIAA 使用固定 Camoufox 152；其他按内置路由。不依赖外部 InstSci，不全局切换内核、不自动升级固定内核。
- 浏览器按出版社复用上下文、每篇独立页面。遇到网页验证，只处理当前可见挑战并有限重试；学校认证由本人完成。会话按学校与内核隔离。
- 保持 agent 通信和校园网分流；不要关全局代理、改 VPN 或复制别人的凭据。遇访问失败先查保存的通路日志与历史证据，区分会话、权限、限流和未捕获 PDF。

已有配置、文件和成功结果增量复用。不要修改用户原 literature_gain / ScanPDF 项目。只给出实际得到和核验过的数量，不承诺任何学校或全部出版社都能访问。
