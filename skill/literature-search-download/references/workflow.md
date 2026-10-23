# 运行、结果和恢复

统一入口：`scripts/workflow.ps1`。全局 `--home <配置目录>` 放在子命令之前。

## 输入

`run --topic ... --year-from ... --year-to ... --max-results ... --providers openalex,scopus`，或 `run --profile <JSON>` 使用原 SearchProfile。支持 queries/include_terms/exclude_terms/document_types/language/year_slice_size 等原字段，详细含义沿用 literature_gain；未知字段会拒绝，不能默默忽略。

保存 `任务编号`，这是恢复同一任务的依据。正文与附件默认都开。可传入原项目 `--classification-rules <JSON>`，不要自行发明分类格式。

分类规则是 `{"类别名": ["关键词1", "关键词2"]}`，同类关键词按任一匹配，标题及正文匹配后可进入多个分类。见 [分类示例](../assets/classification-rules.example.json)。

## 自动执行

1. 原 literature_gain 多源检索、证据留存、筛选去重、共享库复用与原自动下载。
2. 自动读取剩余正文清单，内置 ScanPDF 按 CSV 的 target_path 下载和严格核验。
3. 原项目导入核验、共享库发布、automatic/agent/all 和分类目录。
4. 原附件发现/HTTP 下载，自动读取附件清单，内置浏览器下载，按 target_dir 回填，原附件模块导入和发布。
5. 记录总数、来源状态、未完成项和恢复信息。

浏览器接手的是原项目自动路径用尽之后导出的清单。没有另造一套检索/排名/正文库。

## 结果

输出的 result_dir 是用户查看成果的目录；原来的命名、渠道、分类、主件和附件组织保留。workspace 下共享库和运行数据库是后续复用依据，别只搬走部分数据库文件。

正式项目入口为成果工作区的 `项目清单.md`，`projects.json` 保存对应索引。新项目和续跑都会自动更新清单；已归档的任务应移出当前配置的 jobs 与正式 results。

每个成果项目统一使用下列结构：

```text
results/主题_年份_创建时间_标识/
  项目说明.md
  pdfs/{all,automatic,agent}/
  attachment/{all,automatic,agent}/
  classifications/by_publisher/
  agent_tasks/
  run.json
```

附件为空不等于“没有 SI”。保持原 DOI、接收路径、文件哈希和渠道归属；不要为了统一外观重新命名已核验的正文或附件。用户需要调试/验收时，先在独立 `--home` 配置独立成果工作区，再运行测试；避免在正式工作区产生 acceptance、migration-check 等临时任务。

运行目录 `workspace/runs/<任务编号>` 保存原清单、日志与 browser_handoff 证据。浏览器 SI 台账与证据放在接收目录之外，避免把 JSON 误导入附件。内部运行状态在配置目录 jobs，不混入论文成果。

`COMPLETED` 表示当前选择范围没有待处理项；结果可能是 0 篇，需检查检索条件。`FINISHED_WITH_PENDING` 表示检索源、正文、程序下载或 SI 仍有未解决项，退出码 2；不能报告全部成功。附件无链接仍可能待确认。没有 SI 的人工确认必须有明确证据，不得自动将 zero 改成 none。

## 恢复

```powershell
& '<skill目录>\scripts\workflow.ps1' status --job '<任务编号>'
& '<skill目录>\scripts\workflow.ps1' resume --job '<任务编号>'
# 原因已解决后重新尝试失败项：
& '<skill目录>\scripts\workflow.ps1' resume --job '<任务编号>' --retry-failed
# 只做本地导入和发布：
& '<skill目录>\scripts\workflow.ps1' resume --job '<任务编号>' --offline
```

普通恢复跳过已验证的下载和未改变的失败尝试；不会为了更新进度反复打开验证。恢复使用原任务的条件、预算和目录。若用户修改了请求，应新建一个任务，不篡改旧任务状态。

锁定目录表示任务仍运行或上次异常，先检查该进程。不要清掉整个缓存、学校 profile 或 CARSI cookies。Elsevier 常驻会话可用 `broker status` 查看、用 `broker stop` 结束自己配置目录的会话；不要终止其他浏览器。
