# Windows 文献检索与下载

这一个 skill 完成：检索 → 原项目自动下载 → 自动接手正文/附件清单 → 核验 → 回填文献库 → 归档分类。保留 literature_gain 的原工作方式，省去人工交接清单。

1. 安装 Python **3.11 x64** 与 Google Chrome。
2. 将整个 `literature-search-download` 文件夹放进你的 agent 支持的 skill 目录。agent 须能读取 SKILL.md、运行本机 PowerShell/Python 并使用可见浏览器；平台自动注册方式因 agent 而异。
3. 让 agent 使用此 skill，先安装依赖，再按 [首次配置](references/first-use.md) 一步一步配置自己的学校、联系邮箱、检索源 API 和保存目录。
4. 先用少量学校有权访问的样本验证。以后告诉 agent 主题、年份与筛选条件，即可自动完成整个流程。

公开包不含浏览器二进制和第三方依赖 wheel；首次安装从 PyPI 与官方源获取固定版本。不含 Python 安装程序、Chrome、API key、学校账号或登录态。新电脑需要自己的授权与首次认证。学校无订阅、网页验证或 API 限流会保留未完成项，不会假报下载成功。

运行中断后用原任务编号继续；详见 [运行与恢复](references/workflow.md)。文件保存在 setup 指定目录，沿用原文献项目的渠道、分类与附件结构。
