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
- 平台编码只作为 `platform_hints`，用于提高解析稳定性，不作为主输入。
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

- 智联招聘更适合通过中文城市名解析 cityId。
- BOSS 直聘更依赖平台城市编码；如果调用方已知编码，建议传 `boss_city_code`。
- 如果不传平台编码，服务端会尝试自动解析或使用内置常用城市映射。

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
| `summary` | 只返回列表字段，速度较快，默认 |
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
      "region_code": "765",
      "detail_level_applied": "summary",
      "error": null,
      "warnings": []
    },
    "boss_zhipin": {
      "ok": true,
      "count": 15,
      "pages_fetched": 1,
      "region_code": 101280600,
      "detail_level_applied": "summary",
      "error": null,
      "warnings": []
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
| `pages_fetched` | 该来源实际采集页数 |
| `region_code` | 该来源使用的平台区域编码 |
| `detail_level_applied` | 该来源应用的数据深度 |
| `error` | 错误信息，成功时为 `null` |
| `warnings` | 非致命警告 |

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
| `matched_keyword` | 匹配关键词 |
| `job_name` | 职位名称 |
| `company` | 公司信息 |
| `salary` | 薪资信息 |
| `location` | 区域和地址信息 |
| `requirements` | 任职要求摘要 |
| `benefits` | 福利列表 |
| `description` | 岗位描述和职责 |
| `links` | 详情页/公司页链接 |
| `metadata` | 采集元数据 |

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
- 智联详情可通过职位编号补取，适合 `detail_level=description`。

### 9.2 BOSS 直聘

- BOSS 更依赖城市编码，例如深圳 `101280600`。
- 如果调用方知道 BOSS 城市编码，建议传 `platform_hints.boss_city_code`。
- BOSS 列表接口不包含完整岗位职责。
- 当 `detail_level=description` 时，服务端会逐条打开详情页提取 `.job-sec-text`。
- BOSS 依赖已登录 Chrome 调试端口和页面正常加载。
- 遇到登录失效、验证码、环境异常或风控时，该来源可能失败。

### 9.3 区县筛选

`region.district` 当前只作为业务区域记录，不保证下发为平台筛选条件。

原因：

- 智联和 BOSS 的区县/商圈筛选参数不一致。
- 第一版如果强行统一，容易给调用方造成“精准筛选”的误解。

### 9.4 页数和条数

- `max_pages_per_source` 是最多采集页数，不代表每页返回多少条。
- 每页条数由平台控制。
- 对外调用方应该用 `max_records_per_source` 控制最终返回规模。

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
- BOSS 需要逐条打开详情页。
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

