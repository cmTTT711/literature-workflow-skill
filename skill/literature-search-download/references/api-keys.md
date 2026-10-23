# 学术 API 配置

核对日期：2026-10-22。额度和申请页面会变化，以各服务官方页面为准。程序无需额外的大模型 API key；agent 本身的服务由使用者自行配置。

| 来源 | 本机变量 | 配置方法 |
|---|---|---|
| OpenAlex | `OPENALEX_API_KEY` | 注册并验证邮箱，在 [Settings / API](https://openalex.org/settings/api) 复制 key。基础匿名请求可用；游标翻页及较大规模使用应配置免费 key，见 [官方认证说明](https://help.openalex.org/api/authentication/)。 |
| Semantic Scholar | `SEMANTIC_SCHOLAR_API_KEY` | 在 [官方 API 页面](https://www.semanticscholar.org/product/api) 申请，收到邮件后本机填写。多数端点允许匿名请求，但共享额度可能限流，配置 key 更稳定。 |
| Scopus | `SCOPUS_API_KEY` | 在 [Elsevier Developer Portal](https://dev.elsevier.com/apikey/create) 创建 API key。需相应机构授权/校园 IP，详见 [官方说明](https://dev.elsevier.com/partner_support.html)。 |
| Scopus 机构 token | `SCOPUS_INSTTOKEN` | 仅在学校/Elsevier 提供时填写；不是所有使用者都有，也不能自行随便生成。 |
| Lens Scholarly | `LENS_API_KEY` | 在 Lens 个人资料的 API & Data 申请 **Scholarly API**，说明机构和用途，批准后创建 token。见 [官方文档](https://docs.api.lens.org/)。 |
| Web of Science | `WOS_API_KEY` | 在 [Clarivate Developer Portal](https://developer.clarivate.com/apis/wos-starter) 注册应用并申请 **WoS Starter API** 计划；本项目使用 Starter 端点。网页订阅与 API 计划需分别核实。 |
| Unpaywall | `UNPAYWALL_EMAIL` | 填本人真实邮箱；没有 key。默认使用 setup 的联系邮箱。见 [官方 API](https://unpaywall.org/products/api)。 |
| 公共联系邮箱 | `PAPER_CONTACT_EMAIL` | 默认来自 setup 的本人邮箱，用于原项目的学术服务联系信息。 |
| OpenAIRE | `OPENAIRE_TOKEN` | 可选补充元数据凭据；未使用可留空。 |

CORE 暂不在本程序启用范围，不要求申请 CORE key。Crossref 元数据无需 key。

## 逐项填写与核对

不要在聊天中粘贴密钥。setup 会在用户配置目录创建 `credentials.env` 空模板，可在本机编辑；也可以在本机交互终端运行：

```powershell
& '<skill目录>\scripts\workflow.ps1' keys --set OPENALEX_API_KEY
& '<skill目录>\scripts\workflow.ps1' keys
```

输入不回显；状态只显示是否已填。已有环境变量优先于凭据文件。文件只接受列出的变量，不支持命令执行或变量插值。不要将 credentials.env、用户配置目录、浏览器 profile 或检索原始数据重新打进分发包。

选择 Scopus、Lens、WoS 却没填对应 key 时，程序会在检索前提示缺失；OpenAlex/S2 无 key 可以试运行，但不保证可完成分页。认证失败、额度不足、429 和网络失败分别记录，不等同于“没有检索结果”。切勿通过反复重试消耗配额；先检查服务状态与自己的访问计划。

检索 API 负责发现和元数据，机构全文访问负责主 PDF/附件。拿到 API key 不代表拿到所有出版社全文权限。
