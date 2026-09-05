# 区域岗位统一搜索接口文档

## 1. 接口概览

接口地址：

```http
POST /api/jobs/region-search
```

用途：按业务区域统一获取智联招聘和 BOSS 直聘岗位数据，并返回平台中立的统一字段结构。

设计原则：

- 对外以 `region`、`query`、`collection`、`output` 建模，不暴露某个平台的历史请求格式。
- 区域主输入使用业务语义，例如 `国家 / 省份 / 城市 / 区县`。
- 平台编码作为 `platform_hints`，用于提高解析稳定性；已传编码时优先用于对应平台查询。
- 每个来源独立成功或失败，默认一个来源失败不影响另一个来源返回。
- BOSS 与智联字段差异通过统一字段结构承接，源平台特有字段仅在 `include_raw=true` 时返回。

## 2. 请求体

### 2.1 完整请求示例

```json
{
  "region": {
    "country": "CN",
    "province": "广东",
    "city": "深圳",
    "district": null,
    "platform_hints": {
      "zhilian_city_id": "765",
      "boss_city_code": 101280600
    }
  },
  "query": {
    "keywords": ["前端开发工程师"],
    "keyword_mode": "any"
  },
  "sources": ["zhilian", "boss_zhipin"],
  "collection": {
    "max_pages_per_source": 1,
    "max_records_per_source": 20,
    "detail_level": "summary",
    "timeout_seconds": 90,
    "on_source_error": "continue"
  },
  "output": {
    "deduplicate": true,
    "include_raw": false,
    "include_source_metadata": true
  }
}
```

### 2.2 最小请求示例

```json
{
  "region": {
    "city": "深圳"
  },
  "query": {
    "keywords": ["前端开发工程师"]
  }
}
```

默认行为：

- 默认来源：`["zhilian", "boss_zhipin"]`
- 默认每来源页数：`1`
- 默认每来源最多返回：`20`
- 默认数据深度：`summary`
- 默认来源失败策略：`continue`
- 默认开启保守去重
- 默认不返回原始字段

## 3. 请求字段说明

### 3.1 `region`

区域信息。该对象描述业务区域，而不是平台内部参数。

```json
{
  "country": "CN",
  "province": "广东",
  "city": "深圳",
  "district": null,
  "platform_hints": {
    "zhilian_city_id": "765",
    "boss_city_code": 101280600
  }
}
```

字段说明：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `country` | string | 否 | `CN` | 国家/地区代码，当前仅支持 `CN` |
| `province` | string/null | 否 | `null` | 省份，例如 `广东` |
| `city` | string | 是 | 无 | 城市，例如 `深圳` |
| `district` | string/null | 否 | `null` | 区县/区域。第一版只记录，不承诺平台级精准筛选 |
| `platform_hints` | object | 否 | `{}` | 平台编码提示 |

`platform_hints` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `zhilian_city_id` | string/null | 智联城市 ID，例如深圳为 `"765"` |
| `boss_city_code` | integer/null | BOSS 城市编码，例如深圳为 `101280600` |

说明：

- 智联招聘支持通过中文城市名解析 cityId；如果调用方已知 cityId，可传 `zhilian_city_id` 跳过解析。
- BOSS 直聘更依赖平台城市编码；如果调用方已知编码，建议传 `boss_city_code`。
- 如果不传平台编码，智联会尝试自动解析；BOSS 使用内置常用城市映射，未覆盖城市会导致 BOSS 来源失败。

### 3.2 `query`

岗位查询条件。

```json
{
  "keywords": ["前端开发工程师"],
  "keyword_mode": "any"
}
```

字段说明：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `keywords` | string[] | 是 | 无 | 岗位关键词列表，最多 10 个 |
| `keyword_mode` | string | 否 | `any` | 关键词匹配模式，第一版仅支持 `any` |

### 3.3 `sources`

数据来源列表。

```json
["zhilian", "boss_zhipin"]
```

可选值：

| 值 | 说明 |
| --- | --- |
| `zhilian` | 智联招聘 |
| `boss_zhipin` | BOSS 直聘 |

只查 BOSS：

```json
["boss_zhipin"]
```

只查智联：

```json
["zhilian"]
```

### 3.4 `collection`

采集控制参数。

```json
{
  "max_pages_per_source": 1,
  "max_records_per_source": 20,
  "detail_level": "summary",
  "timeout_seconds": 90,
  "on_source_error": "continue"
}
```

字段说明：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `max_pages_per_source` | integer | 否 | `1` | 每个来源最多采集页数，不代表每页条数 |
| `max_records_per_source` | integer | 否 | `20` | 每个来源最多返回职位数 |
| `detail_level` | string | 否 | `summary` | 数据深度 |
| `timeout_seconds` | number | 否 | `90` | 单来源超时时间 |
| `on_source_error` | string | 否 | `continue` | 单来源失败时的处理策略 |

`detail_level` 可选值：

| 值 | 说明 |
| --- | --- |
| `summary` | 只返回列表字段，默认；智联和 BOSS 均不逐条打开详情 |
| `description` | 额外补岗位描述/职责，耗时更长 |

`on_source_error` 可选值：

| 值 | 说明 |
| --- | --- |
| `continue` | 一个来源失败，继续返回其他来源数据 |
| `fail` | 任一来源失败则整体失败 |

当前服务端限制：

| 配置 | 默认上限 |
| --- | --- |
| `REGION_JOBS_MAX_PAGES_PER_SOURCE` | `3` |
| `REGION_JOBS_MAX_RECORDS_PER_SOURCE` | `50` |
| `REGION_JOBS_MAX_COMBINATIONS` | `10` |

### 3.5 `output`

输出控制。

```json
{
  "deduplicate": true,
  "include_raw": false,
  "include_source_metadata": true
}
```

字段说明：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `deduplicate` | boolean | 否 | `true` | 是否进行保守去重 |
| `include_raw` | boolean | 否 | `false` | 是否返回平台原始字段 |
| `include_source_metadata` | boolean | 否 | `true` | 是否返回来源采集状态 |

## 4. 响应结构

接口使用项目统一响应格式：

```json
{
  "code": 200,
  "message": "区域岗位搜索完成，共 10 条",
  "data": {},
  "timestamp": "2026-05-21T10:45:00"
}
```

`data` 示例：

```json
{
  "request": {
    "region": {
      "country": "CN",
      "province": "广东",
      "city": "深圳",
      "district": null
    },
    "keywords": ["前端开发工程师"],
    "keyword_mode": "any",
    "sources": ["zhilian", "boss_zhipin"],
    "detail_level": "summary"
  },
  "summary": {
    "total": 10,
    "total_before_dedup": 12,
    "deduplicated_count": 2,
    "sources_succeeded": ["zhilian", "boss_zhipin"],
    "sources_failed": []
  },
  "source_status": {
    "zhilian": {
      "ok": true,
      "count": 20,
      "pages_fetched": 1,
      "queries_attempted": 1,
      "pages_requested": 1,
      "region_code": "765",
      "detail_level_applied": "summary",
      "error": null,
      "warnings": []
    },
    "boss_zhipin": {
      "ok": true,
      "count": 15,
      "pages_fetched": 1,
      "queries_attempted": 1,
      "pages_requested": 1,
      "region_code": 101280600,
      "detail_level_applied": "summary",
      "error": null,
      "warnings": [],
      "worker_id": "boss-a",
      "worker_status": {
        "boss-a": {
          "state": "healthy",
          "in_flight": 0,
          "proxy_id": "boss-proxy-a",
          "proxy_state": "leased",
          "local_proxy_url_masked": "http://user:***@127.0.0.1:18081"
        }
      }
    }
  },
  "jobs": []
}
```

### 4.1 `summary`

| 字段 | 说明 |
| --- | --- |
| `total` | 最终返回职位数量 |
| `total_before_dedup` | 去重前职位数量 |
| `deduplicated_count` | 去重掉的职位数量 |
| `sources_succeeded` | 成功的数据来源 |
| `sources_failed` | 失败的数据来源 |

### 4.2 `source_status`

按来源返回采集状态。

| 字段 | 说明 |
| --- | --- |
| `ok` | 来源是否成功 |
| `count` | 该来源返回职位数量 |
| `pages_fetched` | 该来源实际累计采集页数；多关键词时为所有关键词页数之和 |
| `queries_attempted` | 该来源实际执行的关键词/区域查询组合数 |
| `pages_requested` | 该来源计划请求的累计页数 |
| `region_code` | 该来源使用的平台区域编码 |
| `detail_level_applied` | 该来源应用的数据深度 |
| `error` | 错误信息，成功时为 `null` |
| `warnings` | 非致命警告 |
| `worker_id` | BOSS 多 worker 模式下本次采集使用的 worker；其他来源为 `null` |
| `worker_status` | BOSS 多 worker 模式下的 worker 健康/冷却与代理快照；代理 URL 只返回脱敏后的 `local_proxy_url_masked`，其他来源为 `null` |

## 5. `jobs[]` 统一职位字段

每条岗位统一为平台中立结构。

```json
{
  "job_id": "boss_zhipin:088ddf36b5ea1be10nB42tq1ElpW",
  "source": "boss_zhipin",
  "source_job_id": "088ddf36b5ea1be10nB42tq1ElpW",
  "matched_keyword": "前端开发工程师",
  "job_name": "前端开发工程师",
  "company": {
    "name": "年年租",
    "industry": "互联网",
    "scale": "20-99人",
    "type_or_stage": "未融资",
    "logo_url": null,
    "profile_url": null
  },
  "salary": {
    "text": "7-8K",
    "min": 7.0,
    "max": 8.0,
    "months": null
  },
  "location": {
    "country": "CN",
    "province": "广东",
    "city": "深圳",
    "district": null,
    "business_district": null,
    "address": null,
    "gps": {
      "longitude": 114.006654,
      "latitude": 22.659316
    }
  },
  "requirements": {
    "experience": "1-3年",
    "degree": "学历不限",
    "skills": [],
    "labels": []
  },
  "benefits": ["节日福利", "零食下午茶"],
  "description": {
    "text": null,
    "responsibilities": null,
    "requirements": null,
    "status": "not_requested"
  },
  "links": {
    "detail_url": "https://www.zhipin.com/job_detail/xxx.html",
    "company_url": null
  },
  "metadata": {
    "collected_at": "2026-05-21T10:45:00",
    "page": 1,
    "query_keyword": "前端开发工程师",
    "raw_available": false
  }
}
```

### 5.1 职位字段说明

| 字段 | 说明 |
| --- | --- |
| `job_id` | 统一职位 ID，格式为 `{source}:{source_job_id}` |
| `source` | 来源平台 |
| `source_job_id` | 平台原始职位 ID |
| `matched_keyword` | 在职位名、技能、标签或行业等字段中明确命中的关键词；无明确命中时为 `null` |
| `job_name` | 职位名称 |
| `company` | 公司信息 |
| `salary` | 薪资信息 |
| `location` | 区域和地址信息 |
| `requirements` | 任职要求摘要 |
| `benefits` | 福利列表 |
| `description` | 岗位描述和职责 |
| `links` | 详情页/公司页链接 |
| `metadata` | 采集元数据 |

说明：`metadata.query_keyword` 表示本条记录来自哪个查询关键词；它不等同于字段级命中。需要判断相关性时优先看 `matched_keyword`。

### 5.2 `description.status`

| 值 | 说明 |
| --- | --- |
| `not_requested` | 未请求详情，通常是 `detail_level=summary` |
| `success` | 详情提取成功 |
| `empty` | 已请求详情，但未提取到描述 |
| `failed: ...` | 详情提取失败 |

## 6. 常用调用示例

### 6.1 查询深圳前端岗位，两平台汇总

```json
{
  "region": {
    "province": "广东",
    "city": "深圳",
    "platform_hints": {
      "boss_city_code": 101280600
    }
  },
  "query": {
    "keywords": ["前端开发工程师"]
  },
  "sources": ["zhilian", "boss_zhipin"],
  "collection": {
    "max_pages_per_source": 1,
    "max_records_per_source": 20,
    "detail_level": "summary"
  }
}
```

### 6.2 只查 BOSS，并补岗位职责

```json
{
  "region": {
    "city": "深圳",
    "platform_hints": {
      "boss_city_code": 101280600
    }
  },
  "query": {
    "keywords": ["前端开发工程师"]
  },
  "sources": ["boss_zhipin"],
  "collection": {
    "max_pages_per_source": 1,
    "max_records_per_source": 5,
    "detail_level": "description"
  }
}
```

### 6.3 只查智联

```json
{
  "region": {
    "city": "深圳"
  },
  "query": {
    "keywords": ["前端开发工程师"]
  },
  "sources": ["zhilian"],
  "collection": {
    "max_pages_per_source": 1,
    "max_records_per_source": 20
  }
}
```

### 6.4 多关键词区域采集

```json
{
  "region": {
    "province": "广东",
    "city": "深圳",
    "platform_hints": {
      "boss_city_code": 101280600
    }
  },
  "query": {
    "keywords": ["前端开发工程师", "React", "Vue"]
  },
  "sources": ["zhilian", "boss_zhipin"],
  "collection": {
    "max_pages_per_source": 1,
    "max_records_per_source": 30,
    "detail_level": "summary",
    "on_source_error": "continue"
  },
  "output": {
    "deduplicate": true,
    "include_raw": false,
    "include_source_metadata": true
  }
}
```

## 7. curl 示例

```bash
curl -X POST "http://127.0.0.1:2906/api/jobs/region-search" \
  -H "Content-Type: application/json" \
  -d '{
    "region": {
      "province": "广东",
      "city": "深圳",
      "platform_hints": {
        "boss_city_code": 101280600
      }
    },
    "query": {
      "keywords": ["前端开发工程师"]
    },
    "sources": ["zhilian", "boss_zhipin"],
    "collection": {
      "max_pages_per_source": 1,
      "max_records_per_source": 20,
      "detail_level": "summary"
    }
  }'
```

如果配置了 `REGION_JOBS_API_KEY`，需要添加请求头：

```bash
-H "x-api-key: your-api-key"
```

## 8. 错误处理

### 8.1 单来源失败，继续返回

当 `on_source_error=continue` 时，一个来源失败不会导致整体失败。

示例：

```json
{
  "summary": {
    "sources_succeeded": ["zhilian"],
    "sources_failed": ["boss_zhipin"]
  },
  "source_status": {
    "boss_zhipin": {
      "ok": false,
      "count": 0,
      "error": "BOSS 职位接口未触发或超时"
    }
  },
  "jobs": []
}
```

### 8.2 任一来源失败则整体失败

设置：

```json
{
  "collection": {
    "on_source_error": "fail"
  }
}
```

任一来源失败时，接口返回 `503`。

### 8.3 所有来源失败

如果所有来源均失败，接口返回：

```json
{
  "code": 503,
  "message": "所有区域岗位来源均采集失败",
  "data": {
    "source_status": {}
  }
}
```

## 9. 平台差异和注意事项

### 9.1 智联招聘

- 区域输入更适合使用中文城市名，例如 `深圳`。
- 服务端会通过智联城市接口解析 cityId。
- 智联列表接口可返回职位编号、公司、薪资、经验、学历、技能等字段。
- `detail_level=summary` 不补拉职位详情；智联详情只在 `detail_level=description` 时通过职位编号补取。
- 智联详情抓取并发由 `JOB_SEARCH_V2_HTTP_CONCURRENCY` 控制；当 `data_server` 将
  `ZHILIAN_UNIT_MAX_UNITS_PER_RUN` 提到 30 时，本服务也应配置
  `JOB_SEARCH_V2_HTTP_CONCURRENCY=30`，否则上层 30 个 unit 会在详情 HTTP 信号量处排队。

### 9.2 BOSS 直聘

- BOSS 更依赖城市编码，例如深圳 `101280600`。
- 如果调用方知道 BOSS 城市编码，建议传 `platform_hints.boss_city_code`。
- BOSS 列表接口不包含完整岗位职责。
- 当 `detail_level=description` 时，服务端会逐条打开详情页提取 `.job-sec-text`。
- BOSS 依赖已登录 Chrome 调试端口和页面正常加载。
- 遇到登录失效、验证码、环境异常、空响应或风控时，该来源可能失败；错误信息会包含关键词、城市编码、页码和响应摘要。
- 单 Chrome profile 模式下，`BOSS_ZHIPIN_MAX_CONCURRENCY` 只允许 1 或 2；需要更高总并发时，应配置 `BOSS_ZHIPIN_WORKERS`，用多个独立账号 / Chrome profile / 调试端口 / 稳定代理出口组成 worker 池。
- 推荐使用 `BOSS_ZHIPIN_PROXY_POOL` + worker `proxy_id` 做稳定租约。代理池配置的是本地可消费 HTTP/SOCKS 端口，例如 `http://127.0.0.1:18081`，不是 VLESS/CF 优选入口 IP；VLESS/Clash 节点必须先由 Clash/mihomo 或认证转发器暴露为本地端口。
- Chrome 启动时必须使用与 worker `local_proxy_url` 对应的 `chrome_proxy_server`，例如 `--proxy-server="http=127.0.0.1:18081;https=127.0.0.1:18081"`。只给 httpx 配 `proxy_url` 而 Chrome 不走同出口，会导致 Chrome 铸造的 `__zp_stoken__` 与 API 请求出口不一致。
- 旧式 worker `proxy_url` 仍兼容，但它只作用于 httpx；使用旧式配置时需手工确认 Chrome 进程同出口。
- 多 worker 模式下，单 worker 风控会同步冷却对应代理；全部 worker 冷却时，BOSS 来源才整体不可用。

代理池配置示例：

```env
BOSS_ZHIPIN_PROXY_POOL=[{"proxy_id":"boss-proxy-a","local_proxy_url":"http://127.0.0.1:18081","chrome_proxy_server":"http=127.0.0.1:18081;https=127.0.0.1:18081","upstream_label":"CF官方优选1"},{"proxy_id":"boss-proxy-b","local_proxy_url":"http://127.0.0.1:18082","chrome_proxy_server":"http=127.0.0.1:18082;https=127.0.0.1:18082","upstream_label":"CF官方优选2"}]
BOSS_ZHIPIN_WORKERS=[{"worker_id":"boss-a","browser_host_port":"127.0.0.1:9527","profile_id":"account-a","proxy_id":"boss-proxy-a","per_worker_concurrency":1},{"worker_id":"boss-b","browser_host_port":"127.0.0.1:9528","profile_id":"account-b","proxy_id":"boss-proxy-b","per_worker_concurrency":1}]
```

100+ 代理库存不要继续塞 `.env` 单行 JSON，推荐改成文件：

```env
BOSS_ZHIPIN_PROXY_POOL_FILE=secrets/boss-proxy-pool.json
BOSS_ZHIPIN_WORKERS_FILE=secrets/boss-workers.json
BOSS_ZHIPIN_PROXY_SELECTION_STRATEGY=random
BOSS_ZHIPIN_PROXY_COOLDOWN_MINUTES=120
```

`boss-proxy-pool.json` 维护代理库存：

```json
[
  {
    "proxy_id": "boss-proxy-001",
    "enabled": true,
    "kind": "local_http",
    "group": "cf-vless",
    "local_proxy_url": "http://127.0.0.1:18081",
    "chrome_proxy_server": "http=127.0.0.1:18081;https=127.0.0.1:18081",
    "upstream_label": "CF官方优选1"
  }
]
```

`boss-workers.json` 只维护当前启用的账号 worker：

```json
[
  {
    "worker_id": "boss-a",
    "browser_host_port": "127.0.0.1:9527",
    "profile_id": "account-a",
    "proxy_id": "boss-proxy-001",
    "per_worker_concurrency": 1
  }
]
```

`profile_id` 应保持稳定，用于复用已登录 Chrome profile。复用已登录浏览器时建议保留
初始 `proxy_id`，确保 Chrome 铸造 `__zp_stoken__` 的出口和 httpx 请求出口一致。
`BOSS_ZHIPIN_PROXY_SELECTION_STRATEGY` 当前支持 `ordered`、`random`、`round_robin`，
主要用于异常恢复时重新分配代理。

`proxy_id` 是内部稳定标识，建议按 `boss-proxy-001` 递增命名；100 个代理是库存，不代表要开 100 个 Chrome worker。普通全局 VPN 只能改变整机出口，不能提供多 worker 独立出口，还可能让 Chrome/httpx 出口不一致。

上游 Clash/mihomo 应固定端口到固定节点，例如 `127.0.0.1:18081 -> CF官方优选1`、`127.0.0.1:18082 -> CF官方优选2`。`7890 AUTO` 只能证明可访问，不能提供多 worker 出口隔离。

BOSS 专用 mihomo 推荐放在 `static/proxy/mihomo-boss/`，由生成脚本把一份 Clash/Mihomo YAML 展开为固定端口：

```powershell
python scripts/generate_boss_proxy_pool.py `
  --source-yaml static/proxy/mihomo-boss/config.yaml `
  --mihomo-config static/proxy/mihomo-boss/config.yaml `
  --proxy-pool secrets/boss-proxy-pool.json `
  --start-port 18081
```

生成规则是 `boss-proxy-001 -> 127.0.0.1:18081`、`boss-proxy-002 -> 127.0.0.1:18082`，依次递增。以后更换 100+ 节点时，只需先替换 `source-yaml`，再运行生成脚本；worker 文件只维护“当前启用哪些账号绑定哪些 proxy_id”。

如果普通 CFW 开启 TUN，必须确认 BOSS 专用节点的 `server` IP 不被 CFW 再代理，否则会形成 `mihomo-boss -> CFW -> 代理节点` 的套代理。生产环境建议把 BOSS 专用 CF 节点与普通 CFW 节点分池，并让 CFW 对这些 BOSS 节点 server IP 走 `DIRECT`，或在采集机关闭 CFW TUN。

可用 `scripts/start_boss_workers.ps1` 按 `.env` 中的 `BOSS_ZHIPIN_WORKERS` 与 `BOSS_ZHIPIN_PROXY_POOL` 启动本机 Chrome：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_boss_mihomo.ps1
powershell -ExecutionPolicy Bypass -File scripts/start_boss_workers.ps1 -EnvFile .env
powershell -ExecutionPolicy Bypass -File scripts/start_boss_workers.ps1 -WorkersFile secrets/boss-workers.json -ProxyFile secrets/boss-proxy-pool.json -DryRun
powershell -ExecutionPolicy Bypass -File scripts/start_boss_workers.ps1 -WorkersFile secrets/boss-workers.json -ProxyFile secrets/boss-proxy-pool.json -WorkerId boss-b -ProxyId boss-proxy-004 -DryRun
powershell -ExecutionPolicy Bypass -File scripts/restart_boss_worker.ps1 -WorkerId boss-b -ProxyId boss-proxy-004 -WorkersFile secrets/boss-workers.json -ProxyFile secrets/boss-proxy-pool.json
python scripts/check_boss_proxy_pool.py --proxy-file secrets/boss-proxy-pool.json --workers-file secrets/boss-workers.json
```

首次启动后需分别登录每个 worker 对应的 Chrome profile。

#### 9.2.1 主服务 4 worker + BOSS 单进程服务

BOSS 采集依赖已登录 Chrome profile、远程调试端口、稳定代理出口和进程内 worker
状态（冷却、自愈、in-flight 计数）。这些状态不能由多个 Uvicorn worker 进程重复管理。

生产部署需要主 API 使用 4 个 worker 时，推荐拆成两个服务：

```powershell
# 1) 启动 BOSS 专用 mihomo 固定端口代理
powershell -ExecutionPolicy Bypass -File scripts/start_boss_mihomo.ps1

# 2) 启动多个已登录 Chrome worker（一个账号/profile/端口/代理对应一个 worker）
powershell -ExecutionPolicy Bypass -File scripts/start_boss_workers.ps1 -EnvFile .env

# 3) 启动 BOSS 单进程服务；它内部仍可调度 boss-a/b/c 等多个账号
uvicorn boss_server:app --host 127.0.0.1 --port 2926 --workers 1

# 4) 启动主 API 服务；BOSS 路由会代理到 BOSS_SERVICE_URL
uvicorn main:app --host 0.0.0.0 --port 2906 --workers 4
```

主服务侧配置：

```env
BOSS_SERVICE_URL=http://127.0.0.1:2926
BOSS_SERVICE_API_KEY=
BOSS_PROXY_TIMEOUT_SEC=95
```

多账号扩容仍通过增加 `boss-workers.json` 条目完成；不要通过把 BOSS 服务自身改成
多 worker 来扩容同一批账号。需要更大规模时，应拆分多个 BOSS 服务实例，每个实例管理
不同的账号、Chrome profile、调试端口和代理池分片。

#### 9.2.2 Chrome worker 自动编排（免手动运维）

当 `BOSS_ZHIPIN_MANAGE_CHROME_WORKERS=true` 时，`boss_server` 启动会以
`secrets/boss-workers.json` 为唯一真相，在 lifespan 内自动完成一轮 reconcile，
不再需要手动 kill 进程 / 跑 `start_boss_workers.ps1` / 查端口：

1. 清理：杀掉 DrissionPage 自起的临时 profile 浏览器（`%TEMP%\DrissionPage\userData\*`）；
   删除配置里已不存在的孤儿运行态状态文件（如从 3 个 worker 缩到 2 个后残留的 `boss-c.json`）。
2. 对齐：遍历配置里每个 worker 逐个 `ensure_worker`——端口已在监听且代理一致就复用，
   否则用绝对路径重启 Chrome。因此改 `boss-workers.json` 增减 worker 后，只需重启
   `boss_server`，多一个少一个都会被自动对齐，不会因运行态漂移导致抢端口/抢 profile。
3. 验证：每个 worker 打开 `https://httpbin.org/ip` 校验出口 IP，结果写入日志
   （`logs/boss/reconciler.log`）与 `/health` 的 `worker_report`，可直观核对出口是否正确；
   随后页面停在 BOSS 搜索首页。

因此正常运维只需一条命令：

```powershell
uvicorn boss_server:app --host 127.0.0.1 --port 2926 --workers 1
```

`scripts/start_boss_workers.ps1` 仅保留为应急手动备用；它已把 `ProfileRoot`/`StateRoot`
绝对化（以脚本上级目录为项目根），避免 Git Bash / 服务进程 / PowerShell 当前目录不一致
导致的 Chrome profile 路径漂移。

根治说明：所有连接已存在 Chrome 调试端口的地方统一走 `services/browser_connect.py`
的 `connect_existing()`（内部 `existing_only()`）。端口连不上时直接抛错，绝不再静默自起
无登录、无代理的临时 profile 浏览器。

#### 9.2.3 浏览器调试端口隔离

BOSS worker 独占 `9527/9528/9529`。智联 V2 主链路走 HTTP 直连，浏览器仅在 fallback
时使用，但其默认端口与 BOSS 的 `account-a`(9527) 相同，会造成交叉污染。已在 `.env`
把智联及旧浏览器模块统一隔离到专用端口：

```env
JOB_SEARCH_BROWSER_HOST_PORT=127.0.0.1:9540
ZHIPIN_BROWSER_HOST_PORT=127.0.0.1:9540
DRISSION_BROWSER_HOST_PORT=127.0.0.1:9540
TUOYU_SERP_BROWSER_HOST_PORT=127.0.0.1:9540
```

可选开启单 worker 自愈换代理：

```env
BOSS_ZHIPIN_RECOVER_WORKERS_ON_ACCESS_LIMIT=true
BOSS_ZHIPIN_MANAGE_CHROME_WORKERS=true
BOSS_ZHIPIN_CHROME_RECOVERY_COOLDOWN_MINUTES=5
BOSS_ZHIPIN_LOGIN_REQUIRED_COOLDOWN_MINUTES=0
BOSS_ZHIPIN_CHROME_PROFILE_ROOT=runtime/chrome-profiles
BOSS_ZHIPIN_WORKER_STATE_ROOT=runtime/boss-workers
BOSS_ZHIPIN_WORKER_DEVTOOLS_TIMEOUT_SEC=10
```

开启后，某个 worker 命中 BOSS 代理/IP 访问受限时，系统会把该 worker 当前代理标记为 `cooldown`，从代理池选择一个未租用、未冷却且健康的新代理，重启该 worker 对应的 Chrome，再重建该 worker 的 httpx/session。登录态/验证码类异常会标记为 `login_required`，不继续轮换代理。其他 healthy worker 不会被停止，仍可继续承接并发请求。

注意：如果换代理后原 Chrome profile 需要重新登录，worker 会进入 `login_required` 或 `cooldown`，不会无限换代理重试。只切换 httpx 代理而不重启 Chrome 是错误做法，因为 Chrome 铸造 `__zp_stoken__` 的出口必须和 API 请求出口一致。

推荐放量顺序：

1. 先配置 2 个 worker + 2 个固定本地代理端口，每个 worker `per_worker_concurrency=1`，跑 1 个关键词、1 个城市、1 页的 `description` 小样本。
2. 小样本稳定后扩到 5 个 worker，仍保持每 worker 并发 1。
3. data_server 侧再把 `BOSS_UNIT_MAX_UNITS_PER_RUN` 从 2 提到 3，观察完整采集窗口。
4. 若 `worker_status` 无扩散性冷却、`proxy_state` 未集中进入 `cooldown`、`code=37` 和 timeout 未异常升高，再提到 5。
5. 只有 5×1 稳定至少 48 小时后，才评估单个 worker 并发 2；一旦某 worker 出现 token churn 或风控，立即降回 1。

### 9.3 区县筛选

`region.district` 当前只作为业务区域记录，不保证下发为平台筛选条件。

原因：

- 智联和 BOSS 的区县/商圈筛选参数不一致。
- 第一版如果强行统一，容易给调用方造成“精准筛选”的误解。

### 9.4 页数和条数

- `max_pages_per_source` 是最多采集页数，不代表每页返回多少条。
- 每页条数由平台控制。
- 多关键词时，每个关键词都会按 `max_pages_per_source` 发起查询，`source_status.pages_fetched` 是累计页数。
- 对外调用方应该用 `max_records_per_source` 控制最终返回规模；服务端会按关键词轮转取数，避免第一个关键词占满配额。

### 9.5 岗位职责

如果需要岗位职责，设置：

```json
{
  "collection": {
    "detail_level": "description"
  }
}
```

注意：

- 会显著增加耗时。
- 智联会额外拉职位详情接口，BOSS 会逐条打开详情页。
- 不保证每条岗位都有职责文本。

## 10. 推荐默认值

生产/对外调用建议默认：

```json
{
  "collection": {
    "max_pages_per_source": 1,
    "max_records_per_source": 20,
    "detail_level": "summary",
    "timeout_seconds": 90,
    "on_source_error": "continue"
  },
  "output": {
    "deduplicate": true,
    "include_raw": false,
    "include_source_metadata": true
  }
}
```

需要岗位职责时：

```json
{
  "collection": {
    "max_pages_per_source": 1,
    "max_records_per_source": 5,
    "detail_level": "description"
  }
}
```

