# AWS Bedrock 每日用量自动化

为子账号 **502225588666** 的 Amazon Bedrock 搭的「每日成本 + 调用量 + 逐条明细(真实 token) + 调用者审计」三合一自动化报告。仿照同仓 `static/azure_cost_export_func/`，复用 daily 调度 + FastAPI 在线预览范式，落盘在本目录 `_data/` 下，零污染其它业务。

## 一、数据链路(真实验证)

```
本机弱权限用户 BedrockAPIKey-58cx (主账号 703621193912，仅 Bedrock 调用 + ce:GetCostAndUsage)
        │  sts.assume_role
        ▼
子账号 502225588666 OrganizationAccountAccessRole (AdministratorAccess)
        ├── Cost Explorer GetCostAndUsage   → 金额(真实成本/实付/Credit 三口径 + 模型级)
        ├── CloudWatch Logs Insights        → 调用次数 + 四类 token + identity.arn
        └── CloudTrail LookupEvents         → 调用者 IP(Cursor 中转) + 活跃时段
```

- **用量/花费实际发生在子账号**，主账号几乎无用量。所有查询都靠 AssumeRole 到子账号 Admin。
- 凭据走本机 `~/.aws` 默认链(与现有 aws CLI 一致)，不承载任何明文密钥。

## 二、三个关键结论(踩过的坑)

1. **Credit 会把实付显示为 0**。CE 的 `UnblendedCost`(现金口径)被 Credit 抵成 0；必须同时看：
   - **真实用量成本**：过滤 `RECORD_TYPE=Usage` 后的 `UnblendedCost`(Credit 抵扣前，真实消耗)。
   - **实付**：不过滤的 `UnblendedCost`(Credit 抵扣后)。
   - **Credit 抵扣**：`RECORD_TYPE=Credit`(负数)。
   - 实测 8/28：真实成本 **$669.62** / 实付 **$0** / Credit **−$669.62**。报告三行并列，绝不被 0 误导。

2. **绝大部分输入 token 走 prompt cache**。invocation logging 的 `input.inputTokenCount` 常常极小(如 2)，真正的量在 `cacheReadInputTokenCount`(如 16M)。报告把 token 拆成 **输入/缓存读/缓存写/输出** 四类，否则严重低估用量。

3. **拿不到真人 IP**。invocation logging 无 IP 字段；CloudTrail 的 `sourceIPAddress` 全是 **Cursor 云端中转服务器的 AWS 网段 IP**，非真人。区分"谁"的可靠维度是 **IAM `identity.arn`**(cursor-bedrock-user vs intern-bedrock)。报告 IP 列如实标注"Cursor 中转 IP"。

## 三、前置状态(已完成)

- ✅ Bedrock model invocation logging 已开到 CloudWatch Logs 组 `/bedrock/model-invocations`(保留期见控制台)，写入角色 `BedrockInvocationLoggingRole`。
- ✅ 本机可 AssumeRole 到子账号 `OrganizationAccountAccessRole`。
- ✅ 主账号 `BedrockAPIKey-58cx` 有 `ce:GetCostAndUsage`。
- ⚠ **logging 只对开启后调用生效**：早于日志组创建时间的日子(如 8/28 及更早)Logs 查询会报 `MalformedQueryException`(窗口超保留期)，代码已优雅降级为"该日无用量"，此时报告仍能从 CE 出成本、从 CloudTrail 出调用者/IP/时段。

## 四、目录结构

```
static/aws_cost_export_func/
├── aws_session.py        # AssumeRole 到子账号，派发 boto3 client(带缓存/自动刷新)
├── cost_fetch.py         # CE 成本：三口径 + 模型级 + RECORD_TYPE 拆分 → _data/daily_cost/<date>.json
├── logs_fetch.py         # CloudWatch Logs：caller×model 聚合四类 token + 逐条 NDJSON
├── trail_fetch.py        # CloudTrail：caller 聚合 IP + 活跃时段
├── daily_pipeline.py     # 编排 CE+Logs+CloudTrail → generate → 落盘(含锁/幂等/多worker)
├── shared/
│   └── usage_report.py   # 纯离线聚合器：三 JSON → md + ECharts HTML + json
├── _data/                # 唯一数据根(下述)
└── README.md
```

`_data/` 布局：

```
_data/
├── daily_cost/<date>.json                     # CE 成本
├── src_cache/<date>/logs_usage.json           # Logs 聚合
├── src_cache/<date>/invocations.ndjson        # Logs 逐条明细
├── src_cache/<date>/trail.json                # CloudTrail 审计
└── reports/<date>/aws-usage-<date>-CST.{md,html,json}   # 最终产物
```

## 五、命令行用法(可独立跑，便于排查)

```bash
cd static/aws_cost_export_func

# 单模块自检
python -m aws_session                    # 验证 AssumeRole + 各服务 client
python -m cost_fetch 2026-08-28          # 只拉成本
python -m logs_fetch 2026-08-28          # 只拉用量(Logs)
python -m trail_fetch 2026-08-28         # 只拉审计(CloudTrail，整天数据可能耗时 1-2min)

# 一键流水线(默认昨天；--force 忽略已存在重跑)
python -m daily_pipeline 2026-08-28 --force

# 仅离线渲染(已有三份 JSON 时)
python -m shared.usage_report --date 2026-08-28 \
  --cost _data/daily_cost/2026-08-28.json \
  --logs _data/src_cache/2026-08-28/logs_usage.json \
  --trail _data/src_cache/2026-08-28/trail.json \
  --out-dir _data/reports/2026-08-28 --json
```

## 六、FastAPI 集成(在线预览 + 每日调度)

由 `api/aws_usage_report_api.py` 提供，前缀 `/api`：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/aws-usage/run` | 手动触发/补跑(body: `{date?, wait?}`)，默认后台执行；写端点挂 `AWS_USAGE_REPORT_API_KEY` |
| GET | `/api/aws-usage/` | 在线预览首页(报告列表) |
| GET | `/api/aws-usage/reports` | JSON 列出已生成报告 |
| GET | `/api/aws-usage/report/{date}/html` | 302 跳转到 /static 下自包含 ECharts HTML |
| GET | `/api/aws-usage/report/{date}/md` | 在线渲染 Markdown |
| GET | `/api/aws-usage/status` | 调度器状态 / 下次触发 / 最近一次运行 |

调度：`lifespan_resources` 内起 asyncio 每日循环，北京 `AWS_USAGE_REPORT_SCHEDULE_HHMM`(默认 09:30) 到点跑昨日报告。多 worker 下靠 `<date>.lock` 互斥 + 幂等跳过。

## 七、配置(utils/settings.py 的 `AWS_USAGE_REPORT_*`)

| 配置项 | 默认 | 说明 |
|---|---|---|
| `AWS_USAGE_REPORT_API_KEY` | 空 | 写端点 x-api-key；留空=不鉴权 |
| `AWS_USAGE_REPORT_LINKED_ACCOUNT` | `502225588666` | 子账号 ID |
| `AWS_USAGE_REPORT_ASSUME_ROLE_ARN` | 子账号 Admin 角色 | AssumeRole 目标 |
| `AWS_USAGE_REPORT_REGION` | `us-east-1` | 默认 region(CE 固定 us-east-1) |
| `AWS_USAGE_REPORT_REGIONS` | `us-east-1` | Logs/CloudTrail 查询 region(逗号分隔) |
| `AWS_USAGE_REPORT_LOG_GROUP` | `/bedrock/model-invocations` | invocation logging 日志组 |
| `AWS_USAGE_REPORT_GRANULARITY` | `DAILY` | CE 粒度；`HOURLY` 需 Payer 开 opt-in |
| `AWS_USAGE_REPORT_ENABLE_SCHEDULER` | `True` | 每日调度开关 |
| `AWS_USAGE_REPORT_SCHEDULE_HHMM` | `09:30` | 触发时刻(北京) |
| `AWS_USAGE_REPORT_SKIP_IF_EXISTS` | `True` | 当天已生成则跳过(省 CE 查询费) |

依赖：`boto3>=1.34`(见根 `requirements.txt`)。

## 八、时间口径

- Logs/CloudTrail 按**北京自然日**窗口 `[date 00:00 CST, +24h)` 查询，NDJSON 双写 UTC + 北京时间。
- CE 成本默认 `DAILY`(UTC 日界)。因 HOURLY 需 Payer 账号 opt-in(实测未开)，与北京日最多 8h 边界偏移，报告 `window_utc.basis` 字段已注明。开 HOURLY opt-in 后把 `AWS_USAGE_REPORT_GRANULARITY=HOURLY` 即精确对齐北京日。

## 九、成本(查询本身的开销)

- CE `GetCostAndUsage`：每次 $0.01，每天约 3 次查询 ≈ $0.9/月。
- Logs Insights：按扫描量计费，单日增量小，极低。
- CloudTrail LookupEvents：免费(管理事件历史查询)。
- invocation logging → CloudWatch Logs：摄入+存储按量，量级小。

## 十、验收(已真实跑通)

- ✅ `aws_session` AssumeRole 到子账号 Admin 成功，CE/Logs/CloudTrail client 均可建。
- ✅ 8/28 成本三口径：真实 $669.62 / 实付 $0 / Credit −$669.62，与 CE 控制台 Usage 金额一致。
- ✅ 8/29 用量：154 次调用、四类 token(缓存读占 90%+)，按 `identity.arn` 拆出 cursor-bedrock-user。
- ✅ CloudTrail 区分 cursor-bedrock-user 与 intern-bedrock，含活跃时段与 IP，IP 明确标注 Cursor 中转。
- ✅ Logs 缺失日(保留期外)自动降级：仍从 CE 出成本、从 CloudTrail 出调用者维度。
- ✅ 7 个 `/api/aws-usage/*` 端点在 main.py 注册成功。
