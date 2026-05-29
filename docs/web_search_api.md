# 统一 Web 搜索 / 抓取接口说明

本文档对应项目中的 `api/web_search.py` 与 `services/web_search/*`，面向集成方、Dify Workflow、`data_server` 采集器以及任何需要"联网搜索 + 正文抓取"能力的内部组件，说明本服务暴露的接口、请求路径、参数、字段映射、缓存与错误模型。

- **Base URL**：`http://<host>:<port>/api`，默认端口参考 `APP_PORT=2906`。
- **本地接口前缀**：`/api/web`
- **实现源码**：
  - `api/web_search.py`
  - `schemas/web_search.py`
  - `services/web_search/{base,tavily_provider,searchapi_google_provider,registry,fetcher,cache}.py`
  - `services/web_search/__init__.py`
  - `api/url_content_fetch.py`（被 `fetcher.py` 复用做正文抓取）
  - `utils/{settings.py, redis_client.py, security.py, responses.py}`

设计目标：

1. **密钥下沉**：Tavily / SearchAPI Google 的 API key 只存在后端，调用方（Dify / 前端 / 内部脚本）不再持有。
2. **提供商中立**：调用方主要写"中性请求字段"；provider 选择与 fallback 由服务端编排。需要提供商特有能力时通过 `provider.tavily / provider.searchapi_google` 逃生通道直通。
3. **三端点单一职责**：仅 SERP / SERP+正文 / 仅抓正文 按耗时与成本拆开，便于按场景选用与配限。
4. **可缓存可降级**：默认走 Redis 缓存；Redis 不可用时静默降级为不缓存，不阻断业务。

---

## 1. 接口总览

| 方法 | 路径 | 说明 | 是否抓正文 | 计费 / 耗时（典型） |
|------|------|------|------------|---------------------|
| `POST` | `/api/web/search` | 仅搜索（SERP） | 否 | 1 Tavily credit 或 1 次 SearchAPI 调用，约 1~2s |
| `POST` | `/api/web/search-and-fetch` | 搜索 + 并发抓取 top_k 条正文 | 是 | 1 + N 次 HTTP，约 5~30s |
| `POST` | `/api/web/fetch` | 仅按 URL 抓取正文，不做搜索 | 是 | N 次 HTTP，约 3~10s |

所有端点统一通过 `utils.responses.create_standard_response` 返回 `{code, message, data, timestamp}` 结构。

---

## 2. 认证与配置

### 2.1 本地接口认证

如果配置了 `WEB_SEARCH_API_KEY`，调用本地接口时必须带请求头：

```http
x-api-key: <WEB_SEARCH_API_KEY>
```

如果 `WEB_SEARCH_API_KEY` 为空（默认），本地接口不校验 `x-api-key`，行为与 `REGION_JOBS_API_KEY / TIANYANCHA_API_KEY` 完全一致。

### 2.2 Provider 远程认证

服务端调用 Tavily / SearchAPI 时使用各自密钥：

- **Tavily**：`Authorization: Bearer <TAVILY_API_KEY>`（不在 body 里塞 `api_key`，对齐官方现行规范）
- **SearchAPI Google**：`Authorization: Bearer <SEARCHAPI_IO_API_KEY>`

任一密钥为空时，对应 provider 的 `is_configured()` 返回 `False`；当请求开启 `include_provider_attempts=true` 时，会在 `data.meta.attempts[*]` 中标记为 `error.code="unconfigured"`，并自动跳过到下一个 provider（仅当 `provider.name=auto` 时）。

### 2.3 相关配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `WEB_SEARCH_API_KEY` | `None` | 本地接口访问密钥；留空表示不启用鉴权 |
| `TAVILY_API_KEY` | `None` | Tavily 开放平台密钥；生产必须填 |
| `SEARCHAPI_IO_API_KEY` | `None` | SearchAPI.io 密钥；生产必须填 |
| `WEB_SEARCH_DEFAULT_PROVIDERS` | `["tavily","searchapi_google"]` | `auto` 模式下的回退链 |
| `WEB_SEARCH_DEFAULT_TOP_K` | `5` | `top_k` 默认值 |
| `WEB_SEARCH_MAX_TOP_K` | `10` | `top_k` 硬上限（取两家 provider 的下界，Google 自 2025-09 起锁 num=10） |
| `WEB_SEARCH_PROVIDER_TIMEOUT_SEC` | `30.0` | 单 provider 调用超时（秒） |
| `WEB_SEARCH_FETCH_HTML_TIMEOUT_SEC` | `15.0` | HTML 抓取单页超时 |
| `WEB_SEARCH_DOC_DOWNLOAD_TIMEOUT_SEC` | `60.0` | PDF / Office 等文档下载超时 |
| `WEB_SEARCH_DEFAULT_CONCURRENCY` | `5` | 单次请求内允许的最大并发抓取条数 |
| `WEB_SEARCH_MAX_CONCURRENCY` | `10` | 并发硬上限 |
| `WEB_SEARCH_DEFAULT_CONTENT_CHARS` | `8000` | 单条正文最大字符数（默认） |
| `WEB_SEARCH_MAX_CONTENT_CHARS` | `50000` | 单条正文最大字符数（硬上限） |
| `WEB_SEARCH_TAVILY_BASE_URL` | `https://api.tavily.com` | Tavily 基地址（联调/灰度可指向 mock） |
| `WEB_SEARCH_SEARCHAPI_BASE_URL` | `https://www.searchapi.io/api/v1/search` | SearchAPI 基地址 |
| `WEB_SEARCH_CACHE_ENABLED` | `True` | 是否启用 Redis 缓存 |
| `WEB_SEARCH_CACHE_TTL_SEARCH_SEC` | `300` | `/web/search` 缓存 TTL（秒） |
| `WEB_SEARCH_CACHE_TTL_FETCH_SEC` | `1800` | `/web/search-and-fetch` 与 `/web/fetch` 缓存 TTL |
| `WEB_SEARCH_REQUEST_TIMEOUT_SEARCH_SEC` | `35.0` | `/web/search` 顶层 wait_for 超时 |
| `WEB_SEARCH_REQUEST_TIMEOUT_SEARCH_AND_FETCH_SEC` | `90.0` | `/web/search-and-fetch` 顶层超时 |
| `WEB_SEARCH_REQUEST_TIMEOUT_FETCH_SEC` | `120.0` | `/web/fetch` 顶层超时 |
| `REDIS_HOST / REDIS_PORT / REDIS_DB / REDIS_PASSWORD / REDIS_KEY_PREFIX` | `127.0.0.1 / 6379 / 0 / "" / script_tools` | 缓存层；不可用时静默降级 |

---

## 3. 统一响应结构

```json
{
  "code": 200,
  "message": "web 搜索完成，命中 5 条（provider=tavily）",
  "data": { },
  "timestamp": "2026-05-27T17:23:00"
}
```

- 成功时 `code = 200`。
- `data` 为业务结构，按端点不同含 `provider / hits / answer / fetch_summary / results / summary / meta` 等字段（详见各端点章节）。默认不回显请求；调试信息统一放入 `data.meta`。

### 3.1 错误码 → HTTP 状态映射

下面是"所有 provider 都失败"时对外 HTTP 状态的归一规则，由 `api/web_search.py::_failure_http_code` 实现：

| HTTP | 何时返回 | 描述 |
|------|----------|------|
| 200 | 成功；或所有 provider 返回 `empty`（hits 为空）也视为成功 | 业务"没搜到"，非系统错误 |
| 401 | `x-api-key` 缺失或错误 | 仅当 `WEB_SEARCH_API_KEY` 已配置 |
| 422 | Pydantic 校验失败 / 所有 provider `unconfigured` | 入参非法或没有可用 provider |
| 502 | 单/多个 provider 返回 `auth/rate_limit/plan_limit/network/parse/unknown` 且最终未拿到结果 | provider 自身故障 |
| 503 | 多个 provider 失败但等级不一致（保守降级） | 触发条件较少 |
| 504 | 顶层 `asyncio.wait_for` 超时，或所有 attempts 都是 `timeout` | provider/链路阻塞 |

各 attempt 的 `error.code` 标准化为：`auth / rate_limit / plan_limit / timeout / empty / network / parse / unconfigured / unknown`，由 `services/web_search/base.py::ProviderError` 定义。

---

## 4. 公共请求字段

下面字段在 `/web/search` 与 `/web/search-and-fetch` 都通用，定义见 `schemas/web_search.py`。

### 4.1 `search`（必填）

```jsonc
{
  "query": "理想汽车 2024 销量",
  "top_k": 5,
  "time_range": "month",
  "start_date": null,
  "end_date": null,
  "topic": "general",
  "include_domains": ["36kr.com", "caixin.com"],
  "exclude_domains": [],
  "locale": { "country": "cn", "language": "zh-CN" },
  "safe_search": false
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `query` | string | 是 | — | 搜索关键词，最长 500 |
| `top_k` | int | 否 | `WEB_SEARCH_DEFAULT_TOP_K`（5） | 命中条数；硬上限 `WEB_SEARCH_MAX_TOP_K`（10） |
| `time_range` | `any / day / week / month / year` | 否 | `any` | 时间窗；`start_date/end_date` 非空时本字段被忽略 |
| `start_date` | string(`YYYY-MM-DD`) | 否 | `null` | 与 `end_date` 必须成对；存在时优先于 `time_range` |
| `end_date` | string(`YYYY-MM-DD`) | 否 | `null` | 同上 |
| `topic` | `general / news / finance` | 否 | `general` | 仅 Tavily 原生支持；SearchAPI 忽略 |
| `include_domains` | string[] | 否 | `[]` | 最多 20；Tavily 原生参数，SearchAPI 内部装饰为 `(site:a OR site:b)` |
| `exclude_domains` | string[] | 否 | `[]` | 最多 20；SearchAPI 内部装饰为 `-site:a` |
| `locale.country` | ISO 2-letter | 否 | `null` | Tavily 映射到国家全名；SearchAPI 映射到 `gl` |
| `locale.language` | BCP-47 | 否 | `null` | SearchAPI 取前缀映射 `hl`；Tavily 暂无对应 |
| `safe_search` | bool | 否 | `false` | SearchAPI 翻译为 `safe=active/off`；Tavily 直传 |

校验规则：`top_k > WEB_SEARCH_MAX_TOP_K` 或日期不合规将抛 HTTP 422。

### 4.2 `provider`（可选）

```jsonc
{
  "name": "auto",
  "fallback_chain": null,
  "tavily": { "include_raw_content": "markdown", "include_answer": "basic" },
  "searchapi_google": { "device": "desktop", "verbatim": false }
}
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `name` | `auto / tavily / searchapi_google` | `auto` | `auto` 才会跑 fallback 链；指定具体 provider 时不降级 |
| `fallback_chain` | provider 列表 | `null` | 仅当 `name=auto`；为空回落到 `WEB_SEARCH_DEFAULT_PROVIDERS` |
| `tavily` | object | `null` | Tavily 专属覆盖项；见 4.2.1 |
| `searchapi_google` | object | `null` | SearchAPI 专属覆盖项；见 4.2.2 |

#### 4.2.1 `provider.tavily`（Tavily 直通）

按官方 [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search) 字段同名透传，未设置即不传，按 Tavily 默认。

| 字段 | 类型 | 说明 |
|------|------|------|
| `search_depth` | `basic / advanced / fast / ultra-fast` | 默认 `basic`；`advanced` 双倍 credit |
| `chunks_per_source` | 1-3 | 仅 `advanced` 有效 |
| `include_answer` | `bool / basic / advanced` | 返回 LLM 摘要到 `data.answer` |
| `include_raw_content` | `bool / markdown / text` | 返回正文到 `results[].raw_content`；**与 `/search-and-fetch` 的 `prefer_provider_native_content` 联动可省一轮抓取** |
| `include_images / include_image_descriptions / include_favicon` | bool | — |
| `auto_parameters` | bool | Tavily 自动调优参数 |
| `exact_match` | bool | 强制精确匹配（query 内引号短语生效） |
| `include_usage` | bool | 默认 `true`；用于回填 `data.meta.attempts[*].credits_used`（需开启 `include_provider_attempts=true`） |

#### 4.2.2 `provider.searchapi_google`（SearchAPI 直通）

按官方 [SearchAPI Google](https://www.searchapi.io/docs/google) 字段同名透传：

| 字段 | 类型 | 说明 |
|------|------|------|
| `device` | `desktop / mobile / tablet` | — |
| `location` | string | 例如 `"New York,United States"` |
| `uule` | string | Google 加密位置串；与 `location` 互斥 |
| `nfpr` | bool | 关闭拼写自动纠错 |
| `verbatim` | bool | 强制原文搜索 |
| `optimization_strategy` | `performance / ads` | — |
| `page` | 1-10 | 分页（Google 已锁 `num=10`） |

### 4.3 `fetch`（仅 `/web/search-and-fetch`）

```jsonc
{
  "enabled": true,
  "max_content_chars": 8000,
  "concurrency": 5,
  "html_timeout_sec": 15.0,
  "doc_download_timeout_sec": 60.0,
  "prefer_provider_native_content": true,
  "only_first_n": null
}
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enabled` | bool | `true` | 关掉则全部 `content.status=skipped` |
| `max_content_chars` | int | `WEB_SEARCH_DEFAULT_CONTENT_CHARS`（8000） | 截断阈值；超限抛 422 |
| `concurrency` | int | `WEB_SEARCH_DEFAULT_CONCURRENCY`（5） | 并发抓取协程数；超限抛 422 |
| `html_timeout_sec` | float | `15.0` | 单页 HTML GET 超时（5~60） |
| `doc_download_timeout_sec` | float | `60.0` | 文档下载超时（10~180） |
| `prefer_provider_native_content` | bool | `true` | 命中 `raw_content` 时直接复用，省一轮抓取（**省 3~8s**） |
| `only_first_n` | int? | `null` | 只抓前 N 条；`null` 表示 = `top_k` |

### 4.4 调试字段开关

搜索类端点支持以下顶层布尔字段：

| 字段 | 默认 | 说明 |
|------|------|------|
| `include_request_echo` | `false` | 在 `data.meta.request` 返回规范化后的请求，包含公共搜索参数、provider 覆盖项和响应调试开关 |
| `include_provider_attempts` | `false` | 在 `data.meta.attempts` 返回 provider 尝试历史，包含失败 provider 与缓存命中标记 |
| `include_raw_provider_payload` | `false` | 返回 `hits[].provider_raw`，并在 `include_provider_attempts=true` 时返回 `data.meta.attempts[*].raw` |

`/web/fetch` 仅支持 `include_request_echo`，开启后在 `data.meta.request` 返回 URL 与抓取选项。

### 4.5 `include_raw_provider_payload`

顶层布尔字段，默认 `false`。开启后返回：

- `hits[].provider_raw`：provider 返回的原始 dict（Tavily 的 raw_content 可达 50KB+，请按需开启）
- `data.meta.attempts[*].raw`：每次尝试的完整响应 body（需同时开启 `include_provider_attempts=true`）

仅供调试 / 审计场景使用。

---

## 5. `POST /api/web/search`

### 5.1 作用

只做 SERP，不抓正文。流程：

1. 用业务请求体 sha256 作为 Redis cache key；`include_request_echo/include_provider_attempts` 不参与缓存指纹。命中缓存且开启 `include_provider_attempts=true` 时，`data.meta.attempts` 返回 `[{name:"cache", ...}]`。
2. 由 `services/web_search/registry.py::build_chain` 解析 provider 链。
3. 顶层 `asyncio.wait_for(WEB_SEARCH_REQUEST_TIMEOUT_SEARCH_SEC)` 包裹整条链。
4. 逐 provider 调用：成功且 `hits` 非空就停止；空结果继续尝试下一个 provider。
5. 成功时写缓存，TTL = `WEB_SEARCH_CACHE_TTL_SEARCH_SEC`。

### 5.2 请求

```http
POST /api/web/search
Content-Type: application/json
x-api-key: <可选，取决于配置>
```

请求体：

```json
{
  "search": {
    "query": "理想汽车 2024 销量",
    "top_k": 5,
    "time_range": "month"
  },
  "provider": { "name": "auto" }
}
```

最小请求：

```json
{ "search": { "query": "刀片电池技术原理" } }
```

### 5.3 响应

```jsonc
{
  "code": 200,
  "message": "web 搜索完成，命中 5 条（provider=tavily）",
  "data": {
    "provider": {
      "selected": "tavily",
      "credits_used": 1,
      "elapsed_ms": 1342
    },
    "hits": [
      {
        "rank": 1,
        "title": "...",
        "url": "https://www.36kr.com/...",
        "display_url": "www.36kr.com",
        "snippet": "...",
        "published_at": "2024-11-12",
        "score": 0.812,
        "favicon": null,
        "provider": "tavily",
        "source_id": "web-www-36kr-com-p-2999",
        "content": null,
        "provider_raw": null
      }
    ],
    "answer": null
  },
  "timestamp": "2026-05-27T17:23:00"
}
```

字段含义：

- `provider.selected`：最终被采用的 provider 名称；fallback 命中 / 缓存命中时也会填。
- `provider.credits_used`：仅当 provider 返回了用量信息时累加（目前只有 Tavily）。
- `hits[].source_id`：稳定 slug（基于 URL 生成），便于下游做唯一键。
- `answer`：仅当 `provider.tavily.include_answer` 启用且 Tavily 返回时填。
- `meta.request`：仅当 `include_request_echo=true` 时返回规范化后的请求。
- `meta.attempts[]`：仅当 `include_provider_attempts=true` 时返回完整尝试历史，包含失败 provider 与缓存命中标记。

开启调试字段时，`data` 会额外包含：

```jsonc
"meta": {
  "request": {
    "search": { /* 规范化后的入参 */ },
    "provider": { "name": "auto", "fallback_chain": null, "tavily": null, "searchapi_google": null },
    "include_request_echo": true,
    "include_provider_attempts": true,
    "include_raw_provider_payload": false
  },
  "attempts": [
    { "name": "tavily", "ok": true, "hit_count": 5, "elapsed_ms": 1342, "credits_used": 1, "error": null, "raw": null }
  ]
}
```

### 5.4 curl 示例

```bash
curl -X POST "http://127.0.0.1:2906/api/web/search" \
  -H "Content-Type: application/json" \
  -d '{
    "search": {
      "query": "理想汽车 2024 销量",
      "top_k": 5,
      "time_range": "month",
      "include_domains": ["36kr.com", "caixin.com"]
    },
    "provider": {
      "name": "auto",
      "tavily": { "include_answer": "basic" }
    }
  }'
```

如配置了 `WEB_SEARCH_API_KEY`：`-H "x-api-key: your-api-key"`。

---

## 6. `POST /api/web/search-and-fetch`

### 6.1 作用

在 `/search` 流程基础上追加并发正文抓取：

1. SERP 同上。
2. 命中后调 `services/web_search/fetcher.py::enrich_with_content`，把每条 `hit` 挂上 `content` 子对象。
3. **Tavily 短路**：若 `fetch.prefer_provider_native_content=true` 且 `hit.raw_content` 已在（来自 Tavily `include_raw_content=markdown/text`），直接复用，`content.source="tavily_raw_content"`，节省一轮 HTTP（3~8s）。
4. 其他条目：用 `asyncio.Semaphore(min(concurrency, WEB_SEARCH_MAX_CONCURRENCY))` 包 `api/url_content_fetch.py::fetch_url_content`，复用项目已有的 HEAD 嗅探 + 文档解析（PDF/Office/图片）+ Markdown 清洗 + 图片有效性校验 + 视频抽取。
5. 顶层 `asyncio.wait_for(WEB_SEARCH_REQUEST_TIMEOUT_SEARCH_AND_FETCH_SEC=90s)` 包裹。
6. 成功时写缓存，TTL = `WEB_SEARCH_CACHE_TTL_FETCH_SEC`。

### 6.2 请求

```json
{
  "search": {
    "query": "理想汽车 2024 销量",
    "top_k": 5,
    "time_range": "month"
  },
  "provider": {
    "name": "tavily",
    "tavily": { "include_raw_content": "markdown" }
  },
  "fetch": {
    "enabled": true,
    "max_content_chars": 4000,
    "concurrency": 5,
    "prefer_provider_native_content": true
  }
}
```

### 6.3 响应

```jsonc
{
  "code": 200,
  "message": "web 搜索+正文完成，命中 5 条；正文 OK 4/5（provider=tavily）",
  "data": {
    "provider": {
      "selected": "tavily",
      "credits_used": 1,
      "elapsed_ms": 4220
    },
    "hits": [
      {
        "rank": 1,
        "title": "...",
        "url": "https://www.36kr.com/...",
        "display_url": "www.36kr.com",
        "snippet": "...",
        "published_at": "2024-11-12",
        "score": 0.812,
        "favicon": null,
        "provider": "tavily",
        "source_id": "web-www-36kr-com-p-2999",
        "content": {
          "status": "provider_native",
          "kind": "html",
          "text": "# 理想汽车 ... [正文已截断]",
          "char_count": 4000,
          "truncated": true,
          "source": "tavily_raw_content",
          "final_url": "https://www.36kr.com/...",
          "elapsed_ms": 0,
          "error": null
        }
      }
    ],
    "answer": null,
    "fetch_summary": {
      "requested": 5,
      "ok": 4,
      "skipped": 0,
      "failed": 1,
      "elapsed_ms": 2820
    },
    "meta": {
      "request": { "search": { }, "provider": { }, "fetch": { } },
      "attempts": [ { "name": "tavily", "ok": true, "hit_count": 5, "elapsed_ms": 1450, "credits_used": 1, "error": null } ]
    }
  },
  "timestamp": "2026-05-27T17:23:00"
}
```

上例中的 `meta` 仅在请求开启 `include_request_echo=true` 或 `include_provider_attempts=true` 时出现；默认响应不会返回该对象。

### 6.4 `content` 子对象语义

| 字段 | 取值 / 说明 |
|------|-------------|
| `status` | `ok / empty / timeout / http_error / too_large / skipped / provider_native / cached` |
| `kind` | `html / pdf / office / image / text / other` |
| `text` | 截断后的 markdown 正文 |
| `char_count` | 数值化的字符数（已截断后） |
| `truncated` | 是否被 `max_content_chars` 截断 |
| `source` | `url_content_fetch / tavily_raw_content / cached / skipped / provider_native` —— 用于回归分析正文质量与命中率 |
| `final_url` | 重定向后的最终 URL（来自 `httpx.Response.url`） |
| `elapsed_ms` | 单条抓取耗时 |
| `error` | 失败原因（成功为 `null`） |

### 6.5 `fetch_summary`

| 字段 | 说明 |
|------|------|
| `requested` | 计划抓取的条数（受 `only_first_n` 与 `top_k` 影响） |
| `ok` | `status ∈ {ok, provider_native}` 的条数 |
| `skipped` | 被 `enabled=false` 或 `only_first_n` 跳过的条数 |
| `failed` | 其余状态（timeout / http_error / too_large / empty 等） |
| `elapsed_ms` | 整体抓取耗时（不含 SERP） |

### 6.6 curl 示例

```bash
curl -X POST "http://127.0.0.1:2906/api/web/search-and-fetch" \
  -H "Content-Type: application/json" \
  -d '{
    "search": {
      "query": "刀片电池技术原理",
      "top_k": 3,
      "time_range": "year"
    },
    "provider": {
      "name": "tavily",
      "tavily": { "include_raw_content": "markdown" }
    },
    "fetch": {
      "max_content_chars": 4000,
      "prefer_provider_native_content": true
    }
  }'
```

---

## 7. `POST /api/web/fetch`

### 7.1 作用

不做搜索，仅按已知 URL 批量抓取正文，复用 `api/url_content_fetch.py::fetch_url_content`：

- HEAD 分流：根据 `content-type` / 扩展名识别 PDF / Office / 图片 / HTML
- 文档：调 `DocumentParserService.parse` 解析
- HTML：trafilatura → markdown 清洗 → 图片 URL 校验 → 视频抽取
- 单条 URL 级 Redis 缓存（key 含 URL + `options`，TTL = `WEB_SEARCH_CACHE_TTL_FETCH_SEC`）

### 7.2 请求

```json
{
  "urls": [
    "https://www.36kr.com/p/29991234",
    "https://example.com/whitepaper.pdf"
  ],
  "options": {
    "max_content_chars": 8000,
    "concurrency": 5,
    "html_timeout_sec": 15.0,
    "doc_download_timeout_sec": 60.0
  }
}
```

| 字段 | 类型 | 限制 | 说明 |
|------|------|------|------|
| `urls` | `HttpUrl[]` | 1~10 | 需为 `http(s)://` 完整 URL |
| `options` | `FetchOptions` | 见 4.3 | `enabled` / `prefer_provider_native_content` / `only_first_n` 在本端点忽略 |

### 7.3 响应

```jsonc
{
  "code": 200,
  "message": "web fetch 完成，OK 2/2",
  "data": {
    "results": [
      {
        "status": "ok",
        "kind": "html",
        "text": "...",
        "char_count": 6210,
        "truncated": false,
        "source": "url_content_fetch",
        "final_url": "https://www.36kr.com/p/29991234",
        "elapsed_ms": 2120,
        "error": null
      }
    ],
    "summary": {
      "requested": 2,
      "ok": 2,
      "skipped": 0,
      "failed": 0,
      "elapsed_ms": 5210
    },
    "meta": {
      "request": { "urls": [], "options": {}, "include_request_echo": true }
    }
  }
}
```

`results[i]` 与请求中的 `urls[i]` 严格一一对应（包括缓存命中条目，缓存命中条目的 `source` 字段会被覆盖为 `"cached"`）。上例中的 `meta` 仅在 `include_request_echo=true` 时出现。

### 7.4 curl 示例

```bash
curl -X POST "http://127.0.0.1:2906/api/web/fetch" \
  -H "Content-Type: application/json" \
  -d '{
    "urls": ["https://www.36kr.com/p/29991234"],
    "options": { "max_content_chars": 8000 }
  }'
```

---

## 8. 中性字段 → Provider 实际参数映射

服务端做的"提供商对齐"全表如下（实现见 `services/web_search/tavily_provider.py::_build_payload` 与 `services/web_search/searchapi_google_provider.py::_build_params`）：

| 中性字段 | Tavily | SearchAPI Google |
|----------|--------|------------------|
| `query` | `query` | `q`（含 domain 装饰） |
| `top_k` | `max_results`（clamp 0-20） | 切片：Google 锁 `num=10`，取前 `top_k` 条 |
| `time_range=day/week/month/year` | `time_range=day/week/month/year` | `time_period=last_day/last_week/last_month/last_year` |
| `start_date / end_date` | `start_date / end_date`（YYYY-MM-DD） | `time_period_min / time_period_max`（MM/DD/YYYY） |
| `topic=general/news/finance` | `topic` 同名 | 无原生映射，忽略 |
| `include_domains=["a","b"]` | `include_domains=["a","b"]`（原生，最多 300） | `q` 装饰 `(site:a OR site:b)` |
| `exclude_domains=["c"]` | `exclude_domains=["c"]`（原生，最多 150） | `q` 装饰 `-site:c` |
| `locale.country="cn"` | 内置 14 国 ISO→全名表；命中则传 `country="china"`，未命中跳过 | `gl="cn"` |
| `locale.language="zh-CN"` | 无对应 | `hl="zh"`（取语言主体段） |
| `safe_search=true` | `safe_search=true` | `safe="active"` |
| `safe_search=false` | `safe_search=false` | `safe="off"` |

未在中性层暴露的能力（如 Tavily `search_depth/include_answer/include_raw_content`、SearchAPI `device/location/uule/verbatim` 等）必须通过 `provider.tavily / provider.searchapi_google` 直通。

---

## 9. 错误处理实例

### 9.1 单 provider 失败 + fallback 成功

```jsonc
{
  "code": 200,
  "message": "web 搜索完成，命中 3 条（provider=searchapi_google）",
  "data": {
    "provider": {
      "selected": "searchapi_google",
      "credits_used": null,
      "elapsed_ms": 1622
    },
    "meta": {
      "attempts": [
        { "name": "tavily", "ok": false, "hit_count": 0, "elapsed_ms": 342,
          "error": { "code": "auth", "http_status": 401, "message": "Unauthorized" } },
        { "name": "searchapi_google", "ok": true, "hit_count": 3, "elapsed_ms": 1280, "error": null }
      ]
    },
    "hits": []
  }
}
```

注意：HTTP 仍为 200，因为业务请求最终拿到结果。`attempts` 仅在 `include_provider_attempts=true` 时返回，用于完整保留失败历史。

### 9.2 所有 provider 都失败

```jsonc
{
  "code": 502,
  "message": "all_providers_failed; tavily:auth(401); searchapi_google:rate_limit(429)",
  "data": {
    "provider": {
      "selected": null,
      "credits_used": null,
      "elapsed_ms": 1622
    },
    "hits": [],
    "answer": null,
    "meta": {
      "attempts": [
        ...
      ]
    }
  }
}
```

### 9.3 顶层超时

```jsonc
{
  "code": 504,
  "message": "provider_timeout: 顶层等待超时",
  "data": {
    "provider": { "selected": null, "credits_used": null, "elapsed_ms": 35000 },
    "hits": [],
    "answer": null,
    "meta": { "request": { } }
  }
}
```

### 9.4 入参非法

```jsonc
{
  "detail": [{
    "type": "value_error",
    "loc": ["body", "search"],
    "msg": "Value error, top_k=11 超过上限 10",
    "input": { "query": "x", "top_k": 11 }
  }]
}
```

（这是 FastAPI 默认的 422 Body，不走 `create_standard_response`）

---

## 10. 缓存策略

- **存储**：Redis（`docker-compose.yaml` 自带 `redis:7-alpine`，仅绑 `127.0.0.1:6379`）。
- **Key 形态**：`{REDIS_KEY_PREFIX}:web_search:v1:{kind}:{sha256_hex}`，其中 `kind ∈ {search, search_and_fetch, fetch}`。
- **指纹**：`sha256(canonical_json(业务请求体))`；同样请求保证同 key，参数顺序无关。`include_request_echo/include_provider_attempts` 不参与搜索缓存指纹，避免调试开关击穿缓存。
- **TTL**：
  - `/web/search`：`WEB_SEARCH_CACHE_TTL_SEARCH_SEC=300s`
  - `/web/search-and-fetch`：`WEB_SEARCH_CACHE_TTL_FETCH_SEC=1800s`
  - `/web/fetch`：单 URL 维度，TTL 同上
- **命中标记**：
  - 当请求开启 `include_provider_attempts=true` 时，`data.meta.attempts` 返回 `[{"name":"cache", "ok":true, "hit_count":N, "elapsed_ms":<10, "credits_used":null, "error":null}]`
  - `hits[*].content.source` 被覆盖为 `"cached"`（仅 `/web/search-and-fetch`）
- **降级**：Redis 启动 `PING` 失败、运行期 GET/SET 异常都被静默 try/except 兜底，`utils/redis_client.py::get_redis()` 返回 `None`，整条链路退化为不缓存，业务不受影响。

---

## 11. 并发与超时

| 层 | 默认 | 行为 |
|----|------|------|
| Provider 调用 | 30s | 单 provider 自身超时；`asyncio.wait_for` 包裹 |
| 顶层 `/search` | 35s | 全链路超时 → 504 |
| 顶层 `/search-and-fetch` | 90s | 全链路超时 → 504 |
| 顶层 `/fetch` | 120s | 全链路超时 → 504 |
| 抓取并发 | `min(opts.concurrency, WEB_SEARCH_MAX_CONCURRENCY=10)` | `asyncio.Semaphore` 控制 |
| HTTP 客户端 | `max_connections=20, max_keepalive_connections=10` | `httpx.AsyncClient` 全局单例（lifespan 管理） |
| 全局代理 | `OUTBOUND_PROXY_URL` | 若配置则注入 `httpx` 的 `proxy=` |

---

## 12. Dify / 内部脚本集成示例

### 12.1 Dify Workflow（薄壳）

照 `scripts/region_job_market_scan.yml` 4 节点骨架：

```
[开始 start_node]
  → [构造请求体 build_payload (Code)]
  → [HTTP Request 调 /api/web/search-and-fetch]
  → [标准化输出 response_envelope (Code)]
  → [结束 end_node]
```

`http-request` 节点：

```yaml
method: post
url: "{{#env.BACKEND_BASE_URL#}}/api/web/search-and-fetch"
headers: |
  Content-Type:application/json
  x-api-key:{{#env.WEB_SEARCH_API_KEY#}}
timeout: { connect: 10, read: 120, write: 10 }
```

`environment_variables` 只留 `BACKEND_BASE_URL` 与 `WEB_SEARCH_API_KEY`；**禁止再持有 `TAVILY_API_KEY / SEARCHAPI_IO_API_KEY`**。

### 12.2 Python 调用（asyncio + httpx）

```python
import httpx

async def search_web(query: str, *, top_k: int = 5, fetch: bool = True):
    payload = {
        "search": {"query": query, "top_k": top_k, "time_range": "month"},
        "provider": {"name": "auto", "tavily": {"include_raw_content": "markdown"}},
    }
    if fetch:
        payload["fetch"] = {"max_content_chars": 8000, "prefer_provider_native_content": True}
    endpoint = "/api/web/search-and-fetch" if fetch else "/api/web/search"
    async with httpx.AsyncClient(base_url="http://127.0.0.1:2906", timeout=120) as cli:
        r = await cli.post(endpoint, json=payload, headers={"x-api-key": "..."})
        r.raise_for_status()
        return r.json()
```

---

## 13. 部署与本地联调

### 13.1 起 Redis（推荐）

```bash
docker compose up -d redis
docker compose ps
```

Redis 不可用时应用仍能启动，仅缓存层降级。日志会写 `WARN`：`Redis 连接失败，Web 搜索缓存层将降级为不缓存`。

### 13.2 装依赖

`requirements.txt` 已含本服务全部依赖：

- `httpx / trafilatura / json-repair / tavily-python`（旧有）
- `redis>=5.0 / cachetools / pytest* / fakeredis`（本次新增；pytest 仅做开发期单测，运行不依赖）

### 13.3 配置 `.env`

```env
TAVILY_API_KEY=tvly-xxxxxxxxxxxx
SEARCHAPI_IO_API_KEY=your-searchapi-key
WEB_SEARCH_API_KEY=                    # 内部联调可留空
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
```

更细粒度参数（超时 / 并发 / TTL）见 §2.3 与 `.env.example` 中 `web_search.py` 段。

### 13.4 冒烟测试（无 pytest 依赖）

```bash
python -m scripts.smoke_web_search
```

脚本覆盖 6 个核心用例：Tavily 成功 + `raw_content` 短路、auto fallback、`top_k=11` 校验、缓存命中、顶层超时、`/web/fetch` 正常路径。退出码 0 = 全部通过。

---

## 14. 路线与已知约束

### 14.1 已知约束

1. **`top_k` 上限 10**：SearchAPI Google 自 2025-09 起锁 `num=10`；想要更多结果需配合 Tavily `max_results` ≤ 20 或在 `provider.searchapi_google.page` 翻页（v1 不做自动翻页聚合）。
2. **`topic` 仅 Tavily 支持**：SearchAPI 忽略；想做"news 限定"建议直接用 Tavily 或在 query 上写关键词。
3. **`locale.country` 在 Tavily 侧只覆盖 14 个常用 ISO 国家**：见 `services/web_search/tavily_provider.py::_ISO_TO_TAVILY_COUNTRY`，按需扩充。
4. **`include_raw_provider_payload=true` 会显著放大响应体**：Tavily 单条 `raw_content` 可达 50KB+，仅调试 / 审计场景启用。
5. **Redis 缓存以请求体为 key**：因此 `include_raw_provider_payload` 等会改变响应大小的字段值不同 → 不同 key、互不命中。

### 14.2 后续计划（不在本次范围）

- Dify YAML 重写（`scripts/web_search (3).yml` → 调本服务），并吊销已写入 Git 历史的两把 provider key。
- 增加 provider：Bing / Brave / Serper 等（接 `BaseSearchProvider` 即可）。
- 多 provider 并行 + 合并 + 去重（v1 是串行 fallback）。
- 按 `x-api-key` 的请求级限流（`slowapi`）。
