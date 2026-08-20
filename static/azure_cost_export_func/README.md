# Azure 每日用量导出 (Timer Function)

本工程含两个每日 **北京时间 09:00**（= UTC 01:00）触发的导出，写入同一存储账户
`xpilotcostexport` 的 `cost-exports` 容器、用不同前缀隔离：

1. **调用次数导出** (`calls/`)：查 Azure Monitor `ModelRequests` 指标，聚合
   `模型 × 项目` 的**真实调用次数**。
2. **单请求明细导出** (`requests/`)：查 Log Analytics 诊断日志，导出**逐条请求**
   的完整元数据(NDJSON) + 按模型/项目的汇总(Markdown)。详见下方"单请求明细导出"章节。

## 为什么用 ModelRequests(而非 AzureOpenAIRequests)

实测两者对 gpt 系列返回**相同数值**，`ModelRequests` 已覆盖 AzureOpenAI + Foundry
全部模型；只用一个指标可避免重复计数。

## 目录结构

采用 Azure Functions **v2 编程模型**(单一 `function_app.py` + 装饰器)。

```
azure_cost_export_func/
├── host.json                       # App Insights 采样关闭(省成本)
├── requirements.txt                # 依赖清单(本地调试 / 重新拉 wheel 用)
├── local.settings.json             # 本地调试配置(勿提交真实密钥)
├── function_app.py                 # v2 入口: 两个 @app.timer_trigger 编排 + 写 blob
├── shared/
│   ├── __init__.py
│   ├── calls_report.py             # 调用次数: 查指标+聚合+渲染 JSON/MD(可独立本地跑)
│   └── requests_report.py          # 单请求明细: 查诊断日志+聚合+渲染 NDJSON/MD(可独立本地跑)
├── _pack_pkg.py                    # 部署: 打自包含 zip(代码 + .python_packages, 正斜杠)
├── _unpack.py                      # 部署: 解压 Linux wheel 到 .python_packages
└── _upload_blob.py                 # 部署: 512KB 分块上传 zip 到 Blob(账户密钥认证)
```

> 两个 Timer(`daily_calls_export`、`daily_requests_export`)均为 `0 0 1 * * *`
> (UTC 01:00 = 北京时间 09:00)，在 `function_app.py` 里用装饰器注册。

## Blob 落地路径

复用现有存储账户 `xpilotcostexport` 的 `cost-exports` 容器，用独立前缀 `calls/`
隔离，不影响 Azure 原生成本导出：

```
cost-exports/calls/2026/08/calls-2026-08-12-CST.json
cost-exports/calls/2026/08/calls-2026-08-12-CST.md
```

> 文件名日期为**北京自然日**：`calls-2026-08-12-CST` 覆盖北京 08-12 00:00~24:00
> (= UTC 08-11 16:00Z ~ 08-12 16:00Z)。指标底层按 UTC 存储，查询窗口按北京日界平移。

## 配置(App Settings / local.settings.json)

| 变量 | 说明 | 默认 |
|---|---|---|
| `COST_SUBSCRIPTION_ID` | 订阅 ID | a6dfdf96-... |
| `COST_RESOURCE_GROUP` | 资源组 | x-pilot |
| `COST_STORAGE_ACCOUNT` | 存储账户 | xpilotcostexport |
| `COST_BLOB_CONTAINER` | 容器 | cost-exports |
| `COST_BLOB_PREFIX` | 调用次数 blob 前缀 | calls |
| `COST_REQUESTS_PREFIX` | 单请求明细 blob 前缀 | requests |
| `COST_LAW_ID` | Log Analytics workspace 的 customerId/GUID(单请求明细用) | fb0b738e-... |
| `COST_COGNITIVE_ACCOUNTS` | 逗号分隔的**真实资源名**(注意 `-resource` 后缀) | 见 local.settings.json |
| `COST_EXPORT_DATE` | (可选)手动补跑指定**北京**日期 `YYYY-MM-DD`，留空=昨天(两个导出共用) | 空 |

> 资源名规律：`x-pilot`(无后缀) + `x-pilot-N-resource`。报告展示时自动去掉 `-resource`。

## 本地调试

```powershell
# 1. 安装依赖
python -m pip install -r requirements.txt

# 2. az 登录(本地用 AzureCliCredential)
az login

# 3. 直接跑核心逻辑(打印报告，不写 blob)
$env:PYTHONIOENCODING="utf-8"
python -m shared.calls_report 2026-08-12      # 指定日期
python -m shared.calls_report                 # 默认昨天(北京)

# 4. 完整 Function 本地运行(需 Azure Functions Core Tools)
func start
```

## 部署到 Azure

### 1. 创建 Function App(Python, 消费计划, 复用现有存储账户)

```bash
az functionapp create \
  --resource-group x-pilot \
  --name xpilot-cost-export \
  --storage-account xpilotcostexport \
  --consumption-plan-location eastus \
  --runtime python --runtime-version 3.11 \
  --functions-version 4 \
  --os-type Linux \
  --disable-app-insights true
```

### 2. 开启系统分配的托管身份

```bash
az functionapp identity assign \
  --resource-group x-pilot --name xpilot-cost-export
# 记下输出的 principalId
```

### 3. 授最小 RBAC 角色(关键 — 需管理员执行)

```bash
PRINCIPAL_ID=<上一步的 principalId>
SUB=a6dfdf96-3081-4996-bd76-7e07d8ea63b0

# 读 Monitor 指标(作用域=资源组) —— 调用次数导出用
az role assignment create --assignee $PRINCIPAL_ID \
  --role "Monitoring Reader" \
  --scope /subscriptions/$SUB/resourceGroups/x-pilot

# 读 Log Analytics 日志(作用域=workspace) —— 单请求明细导出用
az role assignment create --assignee $PRINCIPAL_ID \
  --role "Log Analytics Reader" \
  --scope /subscriptions/$SUB/resourceGroups/x-pilot/providers/Microsoft.OperationalInsights/workspaces/xpilot-diag-law

# 写 Blob(作用域=存储账户)
az role assignment create --assignee $PRINCIPAL_ID \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/$SUB/resourceGroups/x-pilot/providers/Microsoft.Storage/storageAccounts/xpilotcostexport
```

> RBAC 生效有几分钟延迟。列 blob 曾报 403(AuthorizationPermissionMismatch)正是
> 因为缺 `Storage Blob Data Contributor` 数据面角色。
> `Monitoring Reader` 已隐含读 workspace 日志的权限，但单独加 `Log Analytics Reader`
> 更明确、作用域更小。

### 4. 配置 App Settings

```bash
az functionapp config appsettings set \
  --resource-group x-pilot --name xpilot-cost-export \
  --settings \
    COST_SUBSCRIPTION_ID=a6dfdf96-3081-4996-bd76-7e07d8ea63b0 \
    COST_RESOURCE_GROUP=x-pilot \
    COST_STORAGE_ACCOUNT=xpilotcostexport \
    COST_BLOB_CONTAINER=cost-exports \
    COST_BLOB_PREFIX=calls \
    COST_REQUESTS_PREFIX=requests \
    COST_LAW_ID=fb0b738e-52df-468f-8d82-741df02cdce2 \
    COST_COGNITIVE_ACCOUNTS="x-pilot,x-pilot-2-resource,x-pilot-3-resource,x-pilot-4-resource,x-pilot-5-resource,x-pilot-6-resource,x-pilot-7-resource,x-pilot-8-resource,x-pilot-10-practice-resource"

# v2 worker indexing 需要
az functionapp config appsettings set -g x-pilot -n xpilot-cost-export \
  --settings AzureWebJobsFeatureFlags=EnableWorkerIndexing
```

### 5. 发布代码(run-from-package —— 当前采用方式)

本 app 运行在 **Linux 消费计划**。实测 `config-zip`/`zipdeploy` 的远程构建链路
(Azure Files + Oryx)在受限网络下**不稳定**:上传常被中途 RST，即使偶尔返回
HTTP 200 也不激活新触发器(Kudu `deployments` 为空、`function show` 读到旧 cron)。
因此改用 **run-from-package**:把代码 + **预装好的 Linux 依赖**打成一个自包含 zip，
上传到 Blob，用 SAS URL 指给 `WEBSITE_RUN_FROM_PACKAGE`。host 直接从只读包挂载
运行，**不做远程构建**，改一次 cron 即可秒级生效，且彻底摆脱上传链路的不确定性。

复现脚本已留在本目录:`_pack_pkg.py`(打包)、`_unpack.py`(解压 wheel)、
`_upload_blob.py`(分块上传)。完整流程:

```powershell
# (a) 拉 Linux/py3.11 wheel(纯 Python + 原生扩展如 cryptography 的 manylinux 版)
pip download --only-binary=:all: `
  --platform manylinux2014_x86_64 --python-version 3.11 --implementation cp --abi cp311 `
  azure-functions "azure-identity>=1.17" "azure-mgmt-monitor>=6.0" `
  "azure-monitor-query>=1.4" "azure-storage-blob>=12.20" -d _wheels

# (b) 解压 wheel 到 Functions 期望的依赖路径(不能 pip install: Windows 上会拒装 Linux wheel)
python _unpack.py    # → .python_packages/lib/site-packages/

# (c) 打自包含 zip(function_app.py + host.json + shared/ + .python_packages/，正斜杠 arcname)
python _pack_pkg.py  # → deploy_pkg.zip (~7MB)

# (d) 上传到 Blob(用账户密钥,不要用 --auth-mode login: 当前账户缺 Blob 数据面写权限)。
#     大文件单次上传易被网络掐断,用 512KB 分块(见 _upload_blob.py)
az storage account keys list --account-name xpilotcostexport -g x-pilot --query "[0].value" -o tsv > _key.txt
python _upload_blob.py

# (e) 生成只读 SAS 并指给 WEBSITE_RUN_FROM_PACKAGE(消费计划用 SAS URL,不能用 =1)
$sas = az storage blob generate-sas --account-name xpilotcostexport --account-key (Get-Content _key.txt -Raw).Trim() `
  --container-name deploy --name app-package.zip --permissions r `
  --expiry (Get-Date).ToUniversalTime().AddYears(3).ToString("yyyy-MM-ddTHH:mmZ") --https-only -o tsv
$url = "https://xpilotcostexport.blob.core.windows.net/deploy/app-package.zip?$sas"
az functionapp config appsettings set -g x-pilot -n xpilot-cost-export --settings "WEBSITE_RUN_FROM_PACKAGE=$url"
# run-from-package 模式不需要远程构建
az functionapp config appsettings delete -g x-pilot -n xpilot-cost-export --setting-names SCM_DO_BUILD_DURING_DEPLOYMENT

# (f) 重启并验证两个 cron
az functionapp restart -g x-pilot -n xpilot-cost-export
az functionapp function show -g x-pilot -n xpilot-cost-export `
  --function-name daily_calls_export --query "config.bindings[0].schedule" -o tsv   # 应为 0 0 1 * * *
```

> 更新代码/改 cron 后:只需重跑 (c)(d)，因 `WEBSITE_RUN_FROM_PACKAGE` 指向的
> blob 名不变(`app-package.zip`，overwrite)，重启即加载新包。重传前若上一次
> 上传残留过未提交的块，可能报 `InvalidBlobOrBlock`，先 `az storage blob delete`
> 掉 `app-package.zip` 再传。

> 打包踩坑记录(**任何打包工具都适用**):Windows 的 `Compress-Archive` 与 .NET
> `ZipFile.CreateFromDirectory` 会把 zip **子目录**条目写成反斜杠(如
> `shared\calls_report.py`)。Linux 上解压后 `shared` 不再是目录，而是名为
> `shared\calls_report.py` 的字面文件，导致 `from shared import ...` 报
> `ModuleNotFoundError`，host 索引到 **0 个函数**。必须用 Python `zipfile`
> 且 `arcname=os.path.relpath(...).replace(os.sep, "/")`(见 `_pack_pkg.py`)。

## 手动补跑历史某天

临时设 `COST_EXPORT_DATE`，触发一次后清空即可：

```bash
az functionapp config appsettings set -g x-pilot -n xpilot-cost-export \
  --settings COST_EXPORT_DATE=2026-08-10
# 手动触发或等待下次定时；完成后删除该设置
az functionapp config appsettings delete -g x-pilot -n xpilot-cost-export \
  --setting-names COST_EXPORT_DATE
```

## 成本

### 调用次数导出 (calls/)

- Functions 消费计划:每天 2 次触发 → 远在每月 100 万次免费额度内 = **$0**
- Blob 存储/写操作:每天几 KB → **< $0.01/月**
- Monitor 平台指标查询:**免费**
- Application Insights:已启用(用于查看 worker/host 日志)，采样已关；每天日志量极小，
  基本在免费额度内。如需极致省成本可删除 `xpilot-cost-ai` 并移除相关 App Settings。

### 单请求明细导出 (requests/) 的额外成本

- Log Analytics **数据摄入**按 GB 计费(Analytics 计划约 $2.3/GB，随区域/计划浮动)。
  这是**主要成本来源**，与请求量成正比；请求量大时请关注月度摄入量。
- workspace 保留 30 天在**免费保留窗**内(前 31 天不额外收保留费)。
- NDJSON 明细 blob 体积随请求量增长，仍属**极低**存储成本。

> 若只需统计量、不需逐条明细，用 calls/ 导出即可(≈$0/月)；requests/ 是为
> "单请求完整明细"付的可控代价。

---

# 单请求明细导出 (诊断日志 + Log Analytics)

`daily_requests_export` 每天**北京时间 09:00**(= UTC 01:00)查 Log Analytics 诊断日志，导出**昨日
(北京自然日)逐条请求**的完整元数据到 Blob。

> **时间口径**：文件名日期为北京自然日，覆盖北京 00:00~24:00(= UTC 前一日 16:00Z ~ 当日 16:00Z)。
> NDJSON 每行同时给出 `TimeGenerated`(原始 UTC)与 `TimeBeijing`(北京时间)两个时间字段。

## 能拿到什么 / 拿不到什么

Cognitive Services 诊断日志 `RequestResponse` 类别**不含 prompt/completion 正文**
(Azure OpenAI 隐私设计默认不落 body)。能拿到的是**逐条请求的元数据**:

- 精确时间戳 `TimeGenerated`、操作 `OperationName`(如 `create-response` / `chatcompletions_create`)
- 部署名 `modelDeploymentName` / 模型 `modelName` / 版本 `modelVersion`
- HTTP 状态 `ResultSignature`、延迟 `DurationMs`、是否流式 `streamType`
- **请求/响应体字节量** `requestLength` / `responseLength`(实测 100% 存在，见下)
- 调用方 IP `CallerIPAddress`(**Azure 端已脱敏**，见下)

**字节量维度(requestLength / responseLength)**:HTTP 请求体、响应体的**字节数**(非字符、非 token)。
逐条明细里是原始字节，汇总里按模型/项目/流式类型聚合并转 KB/MB/GB。用途:在拿不到
token 的情况下，用字节量**间接反映每次请求的大小 / 成本量级**。
⚠️ **流式(Streaming)响应的 `responseLength` 会显著虚高**:它统计整个 SSE 流的累计字节
(含 `data:` 分块框架、重复 JSON 结构、`[DONE]`)，不等于有效内容大小；非流式则接近纯响应体。
汇总里专门按 `streamType` 分桶展示，避免误读。请求体 `requestLength` 无此问题。

**IP 只到 /24 网段**:`CallerIPAddress` 最后一段被 Azure **服务端脱敏**成 `*`(如
`119.45.167.***`)，这是平台级 PII 策略，非本项目代码截断、不可通过诊断设置关闭。
经 Azure MCP 直查原始表验证:返回值本身即带 `*`。要完整客户端 IP 只能应用侧自行记录。

**Token 用量当前拿不到**:`promptTokens` / `generatedTokens` 依赖 `AzureOpenAIRequestUsage`
用量事件，但本工程流量绝大多数走 `create-response`(responses API / Foundry / 第三方模型)，
**这些路径不产生用量事件**(该类别仅对 Azure OpenAI 原生 chat completions 有效)，
故 workspace 里无 token 数据、明细中 token 恒为 0。按模型/天的 **token 总量**请以**计费 CSV**为准。

真正带正文、带精确 token 的"账单级记录"平台层拿不到，只能在**应用侧**自行埋点。

## 数据流

```
9 个 Cognitive 账户
  │ 诊断设置 diag-to-law (RequestResponse + AzureOpenAIRequestUsage)
  ▼
Log Analytics workspace: xpilot-diag-law (eastus, 保留30天)
  │ KQL 查昨日 (LogsQueryClient)
  ▼
daily_requests_export (Timer 北京 09:00 = UTC 01:00)
  │
  ▼
Blob: cost-exports/requests/{YYYY}/{MM}/requests-{date}-CST.ndjson  (逐条明细)
                              requests-{date}-CST.md                (汇总)
```

## 已开通的 Azure 资源(本次已创建)

- workspace:`xpilot-diag-law`，customerId=`fb0b738e-52df-468f-8d82-741df02cdce2`
- 9 个账户各一条诊断设置 `diag-to-law`，类别 `RequestResponse` + `AzureOpenAIRequestUsage`，指向该 workspace。

> **诊断日志无法回填**:只能采到诊断设置**开启时刻之后**的请求。开通当天
> 之前的历史请求拿不到；首个完整自然日(北京)的数据从次日 09:00 起可导出。

## 数据源与落表

诊断日志落在 Log Analytics 的 `AzureDiagnostics` 表(legacy 共享表)，
`properties_s` 动态列里是逐请求的 JSON。核心 KQL(见
[shared/requests_report.py](shared/requests_report.py))用
`union isfuzzy=true` + `column_ifexists(...)` 兼容"表尚无数据"的情况(此时返回 0 行
而非报错)。

## 本地调试

```powershell
az login
$env:PYTHONIOENCODING="utf-8"
python -m shared.requests_report 2026-08-14   # 指定北京日期
python -m shared.requests_report              # 默认昨天(北京)
```

首日日志尚未积累时会打印 0 条，属正常;有实际流量并经过落表延迟(数分钟)后再查即可见数据。

## 交叉校验

用 `ModelRequests` 指标当日总数与本导出的请求条数交叉核对，二者应接近
(诊断日志与指标口径略有差异属正常)。

---

# 每日消耗报告自动化(融入 FastAPI 项目 script_tools)

在**不改动**上面云函数(`function_app.py`)的前提下，把"拉账单 CSV + 复用云函数已导出的
calls/requests → `shared/usage_report.generate()` 生成 md/html/json → 落盘/回传 blob →
在线预览"作为一个标准 router 融入现有 FastAPI 项目，用应用生命周期内的 asyncio 每日
调度替代 systemd/cron。

## 组成(全部落盘限制在 static 子目录，零污染其它业务)

```
static/azure_cost_export_func/
├── shared/usage_report.py     # 复用现有 generate()
├── billing_fetch.py           # [新] refresh_billing.py 改造版(SDK + DefaultAzureCredential，去 az CLI)
├── daily_pipeline.py          # [新] 纯逻辑：拉CSV + 下 calls/requests + generate + 落盘 + 可选回传
└── _data/                     # [新] 唯一数据根(全部产物/缓存都在这)
    ├── daily_csv/<date>.csv
    ├── src_cache/<date>/{calls.json,requests.ndjson}
    └── reports/<date>/usage-<date>-CST.{md,html,json}

api/usage_report_api.py        # [新] router + lifespan 每日调度 + 预览端点
utils/settings.py              # [改] 新增 UsageReportSettings(USAGE_REPORT_* 前缀)
main.py                        # [改] import + _LIFESPAN_MODULES + include_router
```

## 端点(前缀 /api)

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/usage/` | **在线预览首页**：列出所有已生成日期，每行一键跳「图表版 / Markdown 版」(公开，浏览器直接打开) |
| GET | `/api/usage/reports` | 列出已生成报告(倒序) + md 在线预览 / html 直出链接(JSON，公开) |
| GET | `/api/usage/report/{date}/md` | 用 markdown 库在线渲染 md 为 HTML(公开) |
| GET | `/api/usage/report/{date}/html` | 302 跳转到 `/static/.../usage-<date>-CST.html`(自包含 ECharts，公开) |
| GET | `/api/usage/status` | 调度器开关 / 下次触发时间 / 最近一次运行结果(公开) |
| POST | `/api/usage/run` | 手动触发/补跑；body `{date?, upload?, wait?}`，默认后台执行返回 202。**唯一需 x-api-key 的写端点**(配置 `USAGE_REPORT_API_KEY` 时) |

> 鉴权拆分：只读预览端点全部公开，方便浏览器直连；仅写操作 `POST /usage/run` 单独挂
> `x-api-key`(留空则不鉴权)。在线预览入口直接打开 `http://<host>/api/usage/` 即可。

> html 之所以能"在线预览"，是因为产物落在 `static/` 下、由 `main.py`
> `app.mount("/static", ...)` 直接静态托管。md 无法被浏览器原生渲染，故由 router 端点
> 服务端渲染成带样式 HTML。

## 配置(App Settings / .env，均带默认值)

| 变量 | 说明 | 默认 |
|---|---|---|
| `USAGE_REPORT_API_KEY` | 端点 x-api-key 守卫，留空=不鉴权 | 空 |
| `USAGE_REPORT_ENABLE_SCHEDULER` | 是否开启每日调度 | True |
| `USAGE_REPORT_SCHEDULE_HHMM` | 每日触发时刻(北京)，晚于云函数 09:00 | 09:10 |
| `USAGE_REPORT_UPLOAD_BLOB` | 生成后是否回传 blob(usage/ 前缀) | True |
| `USAGE_REPORT_SUBSCRIPTION_ID` / `_RESOURCE_GROUP` / `_STORAGE_ACCOUNT` / `_BLOB_CONTAINER` | Azure 资源定位 | 见 settings |
| `USAGE_REPORT_EXPORT_NAME` | Cost Management 导出任务名 | daily-actualcost-export |
| `USAGE_REPORT_CALLS_PREFIX` / `_REQUESTS_PREFIX` / `_OUT_PREFIX` | blob 前缀 | calls/requests/usage |
| `USAGE_REPORT_SRC_SUFFIX` | calls/requests 文件名后缀(线上云函数导出为 -UTC，<date> 即 UTC 自然日) | UTC |
| `USAGE_REPORT_OUT_SUFFIX` | 回传 usage 报告文件名后缀(与入参 date 北京日语义一致) | CST |
| `USAGE_REPORT_DATA_SUBDIR` | 数据根(相对 static) | azure_cost_export_func/_data |
| `USAGE_REPORT_SKIP_IF_CSV_EXISTS` | 当天已生成则跳过(幂等/补跑省时) | False |

## 鉴权：Service Principal + 环境变量(无人值守，不依赖 az login)

拉账单(ARM)与读写 blob(数据面)统一走 `azure.identity.DefaultAzureCredential`。
Linux 服务器上创建服务主体并把凭据写进 `.env` / 环境变量即可：

```bash
# 1) 创建 SP 并授最小 RBAC(需管理员)
SUB=a6dfdf96-3081-4996-bd76-7e07d8ea63b0
az ad sp create-for-rbac --name xpilot-usage-report --skip-assignment
# 记下输出的 appId / password / tenant

APP_ID=<appId>
# 触发按需导出(Cost Management)
az role assignment create --assignee $APP_ID \
  --role "Cost Management Contributor" --scope /subscriptions/$SUB
# 读写 blob(下载 CSV/calls/requests + 上传 usage)
az role assignment create --assignee $APP_ID \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/$SUB/resourceGroups/x-pilot/providers/Microsoft.Storage/storageAccounts/xpilotcostexport
```

```dotenv
# 2) 服务器 .env(DefaultAzureCredential 自动识别这三个变量)
AZURE_CLIENT_ID=<appId>
AZURE_TENANT_ID=<tenant>
AZURE_CLIENT_SECRET=<password>
```

> RBAC 生效有几分钟延迟。本地开发也可不配 SP：`DefaultAzureCredential` 会回退到
> `az login` 的用户身份或 VS Code 凭据。

## 多 worker 注意

`uvicorn --workers N` 会每进程各起一个调度器，到点可能并发触发。`daily_pipeline.run`
用 `reports/<date>/.lock` 原子文件锁做进程级互斥 + "当天已生成则跳过"(需
`USAGE_REPORT_SKIP_IF_CSV_EXISTS=True` 才跳过重算)，避免重复拉账单/重复上传。

## 本地补跑 / 验证

```bash
# 补跑指定日期，不回传 blob，同步等待产物
curl -X POST http://127.0.0.1:2906/api/usage/run \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-08-19","upload":false,"wait":true}'

# 列表 / 预览
curl http://127.0.0.1:2906/api/usage/reports
# 浏览器打开 /api/usage/report/2026-08-19/md 或 /api/usage/report/2026-08-19/html
```

