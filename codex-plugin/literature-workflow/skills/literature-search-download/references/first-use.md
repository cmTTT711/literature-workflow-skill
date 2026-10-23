# 第一次在自己的电脑使用

## 1. 安装

准备 Windows x64、Python **3.11 x64**、Google Chrome。包内携带检索与下载集成组件；首次安装从 PyPI 和浏览器官方源获取固定依赖及内核，不需要安装外部 InstSci。

先选择安装磁盘和数据磁盘，有可用非系统盘时优先使用。解压到有写权限的目录，例如 `D:\Skills\literature-search-download`。在 PowerShell 中运行：

```powershell
& 'D:\Skills\literature-search-download\scripts\install.ps1' -Python 'C:\路径\python.exe' -DataHome 'D:\LiteratureData'
```

安装创建 skill 自己的 `.venv`。`-DataHome` 指定配置、密钥、登录态和大型浏览器内核的目录，建议放在非系统盘的短路径；安装脚本会为该 skill 记住这个位置。省略时使用用户目录的 `.literature-workflow`。也可用环境变量 `LITERATURE_WORKFLOW_HOME` 或入口的 `--home` 指向自己的独立目录。程序不会修改系统代理。安装位置、运行数据目录与正文成果目录分别配置，均应检查实际所在磁盘。

如果 PowerShell 阻止本机脚本，可在一次进程内运行：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File <install.ps1路径> -Python <python.exe路径>`。这不会永久修改系统执行策略。

## 2. 选择检索源与配置 API

让用户先选择实际使用的来源。可先用 OpenAlex，再逐项加入 Semantic Scholar、Scopus、Lens 或 Web of Science。按 [API 指南](api-keys.md) 配置选择的服务；未使用的服务可以留空。学校能登录网页不一定代表该校有数据库 API 授权。

## 3. 填学校信息

确认四件事：学校官方全名、本人可联系的真实邮箱、访问方式、成果目录。

- 在学校网络且出版社能识别机构：`--access campus`。
- 使用 CARSI 联邦认证：`--access carsi`，选择自己的学校并登录。
- 使用学校指定机构认证入口：`--access institution --institution-url 'https://学校官方入口'`。各校 WebVPN 不一定兼容，先拿本校样本验证。

```powershell
& '<skill目录>\scripts\workflow.ps1' setup --school '自己的学校全名' --email '自己的真实邮箱' --access campus --workspace 'D:\Literature' --providers openalex,semantic_scholar
& '<skill目录>\scripts\workflow.ps1' doctor
```

成果目录使用较短路径，避免原项目深层原始检索记录触及 Windows 路径长度限制。学校信息可再次 setup 修改；不同学校的登录会话分开保存。

## 4. 网络与首次机构认证

保持 Codex、GitHub 等正常走用户已有代理，学术站点走学校授权网络。程序不会关闭代理。先在学校网络访问样本文章，确认机构名/全文授权。

需要登录时：

```powershell
& '<skill目录>\scripts\workflow.ps1' login --doi '学校订阅的样本 DOI' --wait-for-confirmation
# 上一进程保持运行；本人在打开的浏览器完成认证后，另一个终端执行：
& '<skill目录>\scripts\workflow.ps1' confirm-login '<输出的 REQUEST_ID>'
```

等待原登录进程保存会话再下载。浏览器验证可能需要本人操作；学校密码、短信验证码不写进配置、不发给 agent。更换电脑须在新机重新认证，安装包不含开发者登录态。

## 5. 小样本验收后正式检索

先选择已知学校有权访问的主题/文章，限制每源 1–3 条，默认同时尝试正文与附件。检查结果目录、正文核验、附件台账、未完成原因。若能读文章但抓不到 PDF，先看浏览器捕获日志，不能直接判没权限。

正式操作只需告诉 agent 主题、年份、筛选条件、数量预算和保存目录。程序自动传递原项目清单，不再人工移动文件。
