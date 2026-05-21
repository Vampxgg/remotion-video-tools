# 天眼查企业数据接口说明

本文档对应项目中的 `api/tianyancha.py` 与 `services/tianyancha_client.py`，面向集成方、Dify Workflow、前端和自动化脚本，说明本服务暴露的天眼查相关接口、请求路径、参数、缓存策略和远程接口调用方式。

- **Base URL**：`http://<host>:<port>/api`，默认端口参考 `APP_PORT=2906`。
- **本地接口前缀**：`/api/tianyancha`
- **实现源码**：
  - `api/tianyancha.py`
  - `services/tianyancha_client.py`
  - `utils/settings.py`

---

## 1. 接口总览

| 方法 | 路径 | 说明 | 是否调用天眼查远程 |
|------|------|------|------------------|
| `POST` | `/api/tianyancha/search` | 企业高级搜索，支持关键词、地区、行业筛选 | 默认可能调用；命中缓存则不调用 |
| `GET` | `/api/tianyancha/company/{keyword}` | 企业基本信息查询 | 本地无缓存或强制刷新时调用 |
| `GET` | `/api/tianyancha/companies` | 本地企业库查询 | 否 |
| `POST` | `/api/tianyancha/research/region-companies` | 区域企业调研，适合 Dify / Agent 使用 | 默认可能调用；命中缓存则不调用 |
| `GET` | `/api/tianyancha/resolve/area` | 解析天眼查地区代码 | 只拉公共地区字典 |
| `GET` | `/api/tianyancha/resolve/category` | 解析天眼查行业代码 | 只拉公共行业字典 |

---

## 2. 认证与配置

### 2.1 本地接口认证

如果配置了 `TIANYANCHA_API_KEY`，调用本地接口时必须带请求头：

```http
X-API-Key: <TIANYANCHA_API_KEY>
```

如果 `TIANYANCHA_API_KEY` 为空，本地接口不校验 `X-API-Key`。

### 2.2 天眼查远程认证

服务端调用天眼查官方接口时使用 `TIANYANCHA_TOKEN`：

```http
Authorization: <TIANYANCHA_TOKEN>
```

必要配置：

```env
TIANYANCHA_TOKEN=你的天眼查Token
TIANYANCHA_ENABLE_REMOTE=true
```

如果 `TIANYANCHA_ENABLE_REMOTE=false`，服务端会直接禁止远程调用。

### 2.3 相关配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `TIANYANCHA_API_KEY` | `None` | 本地接口访问密钥 |
| `TIANYANCHA_TOKEN` | `None` | 天眼查开放平台 Token |
| `TIANYANCHA_SEARCH_URL` | `http://open.api.tianyancha.com/services/open/searchx` | 企业高级搜索远程地址 |
| `TIANYANCHA_BASEINFO_URL` | `http://open.api.tianyancha.com/services/open/ic/baseinfo/normal` | 企业基本信息远程地址 |
| `TIANYANCHA_AREA_CODE_URL` | `https://jindi-oss-open.oss-cn-beijing.aliyuncs.com/document/newAreaCodeV2024.json` | 地区代码字典 |
| `TIANYANCHA_CATEGORY_URL` | `https://jindi-oss-open.oss-cn-beijing.aliyuncs.com/document/category.json` | 行业代码字典 |
| `TIANYANCHA_HTTP_TIMEOUT` | `15.0` | 远程 HTTP 超时秒数 |
| `TIANYANCHA_SEARCH_CACHE_TTL_SECONDS` | `86400` | 搜索缓存 TTL，默认 1 天 |
| `TIANYANCHA_BASEINFO_TTL_DAYS` | `30` | 企业详情缓存 TTL，默认 30 天 |
| `TIANYANCHA_MAX_PAGE_SIZE` | `20` | 搜索每页最大条数 |
| `TIANYANCHA_MAX_PAGES_PER_REQUEST` | `5` | 单次请求允许的最大页码 |
| `TIANYANCHA_MAX_DETAIL_CALLS_PER_REQUEST` | `5` | 单次最多补拉详情数量 |
| `TIANYANCHA_DIFY_DEFAULT_LIMIT` | `20` | Dify 区域调研默认返回数 |
| `TIANYANCHA_DIFY_MAX_LIMIT` | `50` | Dify 区域调研最大返回数 |

---

## 3. 统一响应结构

接口通过 `create_standard_response` 返回统一外层结构，典型形式如下：

```json
{
  "code": 200,
  "message": "操作说明",
  "data": {},
  "timestamp": "2026-05-21T17:00:00"
}
```

说明：

- 成功时 `code` 通常为 `200`。
- 失败时 `code` 会映射为 `400`、`401`、`403`、`404`、`429`、`500`、`502` 等。
- 业务数据都在 `data` 内。

---

## 4. `POST /api/tianyancha/search`

### 4.1 作用

企业高级搜索。支持按关键词、国民经济行业代码、地区代码查询企业。服务端会：

1. 根据请求参数生成搜索指纹 `fingerprint`。
2. 优先查本地搜索缓存。
3. 未命中缓存或 `force_remote=true` 时，调用天眼查远程搜索接口。
4. 将搜索结果去重写入本地企业库。
5. 如果 `enrich_detail=true`，按限制补拉企业基本信息。

### 4.2 请求

```http
POST /api/tianyancha/search
Content-Type: application/json
X-API-Key: <可选，取决于配置>
```

请求体：

```json
{
  "word": "新能源汽车",
  "category_guobiao": null,
  "area_code": "440300",
  "page_num": 1,
  "page_size": 20,
  "enrich_detail": false,
  "force_remote": false,
  "refresh_detail": false,
  "max_detail_calls": null
}
```

### 4.3 参数说明

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `word` | string / null | 条件必填之一 | `null` | 搜索关键词，例如企业名、品牌、行业词 |
| `category_guobiao` | string / null | 条件必填之一 | `null` | 国民经济行业代码 |
| `area_code` | string / null | 条件必填之一 | `null` | 天眼查地区代码 |
| `page_num` | int | 否 | `1` | 页码，最大默认 `5` |
| `page_size` | int | 否 | `20` | 每页条数，最大默认 `20` |
| `enrich_detail` | bool | 否 | `false` | 是否对本页企业补拉基本信息 |
| `force_remote` | bool | 否 | `false` | 是否跳过本地搜索缓存 |
| `refresh_detail` | bool | 否 | `false` | 补详情时是否忽略详情 TTL |
| `max_detail_calls` | int / null | 否 | `null` | 本次最多补拉详情数量，最大默认 `5` |

校验规则：

- `word`、`category_guobiao`、`area_code` 至少提供一个。
- `page_size` 不能超过 `TIANYANCHA_MAX_PAGE_SIZE`。
- `page_num` 不能超过 `TIANYANCHA_MAX_PAGES_PER_REQUEST`。
- `max_detail_calls` 不能超过 `TIANYANCHA_MAX_DETAIL_CALLS_PER_REQUEST`。

### 4.4 示例

```bash
curl -X POST "http://127.0.0.1:2906/api/tianyancha/search" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "word": "新能源汽车",
    "area_code": "440300",
    "page_num": 1,
    "page_size": 20,
    "enrich_detail": true,
    "max_detail_calls": 3
  }'
```

### 4.5 返回

```json
{
  "code": 200,
  "message": "天眼查企业搜索完成",
  "data": {
    "source": "remote",
    "cache_hit": false,
    "remote_called": true,
    "detail_remote_calls": 3,
    "created_count": 10,
    "updated_count": 10,
    "total": 100,
    "companies": [],
    "query": {
      "fingerprint": "sha256...",
      "word": "新能源汽车",
      "category_guobiao": null,
      "area_code": "440300",
      "page_num": 1,
      "page_size": 20,
      "total": 100,
      "fetched_at": "2026-05-21T09:00:00+00:00"
    },
    "warnings": []
  }
}
```

关键字段说明：

| 字段 | 说明 |
|------|------|
| `source` | `cache` 表示来自缓存，`remote` 表示调用远程 |
| `cache_hit` | 是否命中本地搜索缓存 |
| `remote_called` | 是否实际调用天眼查远程搜索 |
| `detail_remote_calls` | 本次补拉企业详情次数 |
| `created_count` | 本次新入库企业数量 |
| `updated_count` | 本次更新企业数量 |
| `total` | 天眼查搜索返回总数 |
| `companies` | 企业列表 |
| `query` | 本次查询缓存记录 |
| `warnings` | 非致命提示，例如无数据 |

---

## 5. `GET /api/tianyancha/company/{keyword}`

### 5.1 作用

查询单个企业基本信息。服务端会先查本地库，本地无数据、详情过期或 `force_remote=true` 时才调用天眼查远程详情接口。

`keyword` 可传：

- 企业名称
- 天眼查企业 ID
- 统一社会信用代码
- 注册号
- 组织机构代码
- 税号

### 5.2 请求

```http
GET /api/tianyancha/company/{keyword}?force_remote=false
X-API-Key: <可选，取决于配置>
```

Query 参数：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `force_remote` | bool | `false` | 是否强制远程刷新 |

### 5.3 示例

```bash
curl "http://127.0.0.1:2906/api/tianyancha/company/百度在线网络技术北京有限公司?force_remote=false" \
  -H "X-API-Key: your-api-key"
```

### 5.4 返回

```json
{
  "code": 200,
  "message": "天眼查企业详情查询完成",
  "data": {
    "source": "cache",
    "cache_hit": true,
    "remote_called": false,
    "company": {
      "id": 1,
      "tianyancha_id": 123456,
      "name": "示例企业",
      "credit_code": "91110000...",
      "reg_status": "存续",
      "reg_capital": "1000万人民币",
      "legal_person_name": "张三",
      "base": "北京",
      "city": "北京市",
      "district": "海淀区",
      "industry": "软件和信息技术服务业",
      "business_scope": "..."
    }
  }
}
```

---

## 6. `GET /api/tianyancha/companies`

### 6.1 作用

查询本地已入库企业，不调用天眼查远程接口。适合低成本检索、后台列表、二次筛选。

### 6.2 请求

```http
GET /api/tianyancha/companies?keyword=百度&area=北京&limit=20
X-API-Key: <可选，取决于配置>
```

Query 参数：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `keyword` | string / null | `null` | 企业名、统一社会信用代码、注册号、组织机构代码 |
| `area` | string / null | `null` | 省、市、区关键字 |
| `industry` | string / null | `null` | 行业关键字 |
| `reg_status` | string / null | `null` | 经营状态，例如 `存续`、`在业` |
| `skip` | int | `0` | 分页偏移 |
| `limit` | int | `50` | 返回条数，最大 `100` |

### 6.3 示例

```bash
curl "http://127.0.0.1:2906/api/tianyancha/companies?keyword=百度&area=北京&limit=20" \
  -H "X-API-Key: your-api-key"
```

### 6.4 返回

```json
{
  "code": 200,
  "message": "本地企业库查询完成，共返回 20 条",
  "data": {
    "companies": [],
    "skip": 0,
    "limit": 20
  }
}
```

---

## 7. `POST /api/tianyancha/research/region-companies`

### 7.1 作用

区域企业调研接口，适合 Dify Workflow、智能体、自动化研究任务使用。它将地区、行业、关键词组合成搜索任务，并自动：

1. 解析地区代码。
2. 解析行业代码。
3. 多关键词、多页搜索企业。
4. 对企业去重。
5. 写入本地企业库。
6. 根据 `detail_level` 控制是否补拉详情。

### 7.2 请求

```http
POST /api/tianyancha/research/region-companies
Content-Type: application/json
X-API-Key: <可选，取决于配置>
```

请求体：

```json
{
  "region": "深圳市",
  "industry": "软件和信息技术服务业",
  "keywords": ["人工智能", "大模型"],
  "limit": 20,
  "detail_level": "summary",
  "force_remote": false
}
```

### 7.3 参数说明

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `region` | string | 是 | 无 | 区域名称或天眼查地区代码 |
| `industry` | string / null | 否 | `null` | 行业名称或行业代码 |
| `keywords` | string[] | 否 | `[]` | 企业搜索关键词，最多 10 个 |
| `limit` | int | 否 | `20` | 最多返回企业数，最大默认 `50` |
| `detail_level` | string | 否 | `summary` | `summary` 或 `baseinfo` |
| `force_remote` | bool | 否 | `false` | 是否跳过搜索缓存 |

`detail_level` 说明：

| 值 | 行为 |
|----|------|
| `summary` | 只查企业搜索列表，成本最低 |
| `baseinfo` | 在搜索结果基础上补拉企业基本信息，成本更高 |

### 7.4 示例

```bash
curl -X POST "http://127.0.0.1:2906/api/tianyancha/research/region-companies" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "region": "深圳市",
    "industry": "软件和信息技术服务业",
    "keywords": ["人工智能", "智能制造"],
    "limit": 30,
    "detail_level": "summary",
    "force_remote": false
  }'
```

### 7.5 正常返回

```json
{
  "code": 200,
  "message": "区域企业调研完成",
  "data": {
    "need_clarification": false,
    "summary": {
      "region": "深圳市",
      "area_code": "440300",
      "industry": "软件和信息技术服务业",
      "category_guobiao": "65",
      "keywords": ["人工智能", "智能制造"],
      "requested_limit": 30,
      "returned_count": 30
    },
    "companies": [],
    "cache": {
      "query_results": [
        {
          "word": "人工智能",
          "page_num": 1,
          "cache_hit": false,
          "total": 100
        }
      ]
    },
    "cost_control": {
      "remote_search_calls": 1,
      "remote_detail_calls": 0,
      "detail_level": "summary",
      "force_remote": false
    },
    "warnings": []
  }
}
```

### 7.6 需要澄清时的返回

当地区或行业名称匹配不唯一时，接口不会继续搜索，而是返回候选项：

```json
{
  "code": 200,
  "message": "区域或行业需要进一步确认",
  "data": {
    "need_clarification": true,
    "area_candidates": [
      {
        "name": "深圳市",
        "full_name": "广东省深圳市",
        "code": "440300",
        "level": "city"
      }
    ],
    "category_candidates": [],
    "message": "区域或行业匹配不唯一，请选择候选项后重试。"
  }
}
```

处理方式：

- 如果 `area_candidates` 不为空，取目标项的 `code` 作为下次请求的 `region`。
- 如果 `category_candidates` 不为空，取目标项的 `code` 作为下次请求的 `industry`。

---

## 8. `GET /api/tianyancha/resolve/area`

### 8.1 作用

解析地区名称为天眼查地区代码。接口会拉取公共地区字典并缓存到进程内存。

### 8.2 请求

```http
GET /api/tianyancha/resolve/area?region=深圳
X-API-Key: <可选，取决于配置>
```

Query 参数：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `region` | string | 是 | 地区名称或地区代码 |

### 8.3 示例

```bash
curl "http://127.0.0.1:2906/api/tianyancha/resolve/area?region=深圳" \
  -H "X-API-Key: your-api-key"
```

### 8.4 返回

```json
{
  "code": 200,
  "message": "地区代码解析完成",
  "data": {
    "area_code": "440300",
    "candidates": []
  }
}
```

匹配规则：

- 如果传入值是 `6-12` 位字母或数字，直接当作地区代码返回。
- 优先精确匹配 `name` 或 `full_name`。
- 精确匹配失败后进行模糊匹配。
- 如果模糊匹配唯一，返回该代码。
- 如果模糊匹配多个，返回候选列表。

---

## 9. `GET /api/tianyancha/resolve/category`

### 9.1 作用

解析行业名称为天眼查国民经济行业代码。接口会拉取公共行业字典并缓存到进程内存。

### 9.2 请求

```http
GET /api/tianyancha/resolve/category?industry=软件
X-API-Key: <可选，取决于配置>
```

Query 参数：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `industry` | string | 是 | 行业名称或行业代码 |

### 9.3 示例

```bash
curl "http://127.0.0.1:2906/api/tianyancha/resolve/category?industry=软件" \
  -H "X-API-Key: your-api-key"
```

### 9.4 返回

```json
{
  "code": 200,
  "message": "行业代码解析完成",
  "data": {
    "category_guobiao": "65",
    "candidates": []
  }
}
```

匹配规则：

- 如果传入值匹配单个字母或 `2-4` 位数字，直接当作行业代码返回。
- 优先精确匹配行业名称。
- 精确匹配失败后进行模糊匹配。
- 如果模糊匹配唯一，返回该代码。
- 如果模糊匹配多个，返回候选列表。

---

## 10. 远程天眼查接口

以下接口不建议前端直接调用，由后端 `TianyanchaClient` 统一封装，负责认证、缓存、错误处理和入库。

### 10.1 企业高级搜索

配置项：

```env
TIANYANCHA_SEARCH_URL=http://open.api.tianyancha.com/services/open/searchx
```

内部请求：

```http
GET http://open.api.tianyancha.com/services/open/searchx
Authorization: <TIANYANCHA_TOKEN>
```

请求参数：

| 远程字段 | 来源字段 | 说明 |
|----------|----------|------|
| `word` | `word` | 搜索关键词 |
| `categoryGuobiao` | `category_guobiao` | 国民经济行业代码 |
| `areaCode` | `area_code` | 地区代码 |
| `pageNum` | `page_num` | 页码 |
| `pageSize` | `page_size` | 每页条数 |

返回处理：

- `error_code=0`：成功，读取 `result.items` 和 `result.total`。
- `error_code=300000`：无数据，作为非致命结果处理。
- 其他错误码抛出 `TianyanchaAPIError`。

### 10.2 企业基本信息

配置项：

```env
TIANYANCHA_BASEINFO_URL=http://open.api.tianyancha.com/services/open/ic/baseinfo/normal
```

内部请求：

```http
GET http://open.api.tianyancha.com/services/open/ic/baseinfo/normal
Authorization: <TIANYANCHA_TOKEN>
```

请求参数：

| 远程字段 | 说明 |
|----------|------|
| `keyword` | 企业名、天眼查 ID、统一社会信用代码、注册号、组织机构代码等 |

返回处理：

- `error_code=0`：成功，读取 `result`。
- 其他错误码抛出 `TianyanchaAPIError`。

### 10.3 公共地区字典

配置项：

```env
TIANYANCHA_AREA_CODE_URL=https://jindi-oss-open.oss-cn-beijing.aliyuncs.com/document/newAreaCodeV2024.json
```

内部请求：

```http
GET https://jindi-oss-open.oss-cn-beijing.aliyuncs.com/document/newAreaCodeV2024.json
```

说明：

- 不带 `Authorization`。
- 拉取后会展开为省、市、区三级扁平列表。
- 进程内缓存到 `_area_cache`。

### 10.4 公共行业字典

配置项：

```env
TIANYANCHA_CATEGORY_URL=https://jindi-oss-open.oss-cn-beijing.aliyuncs.com/document/category.json
```

内部请求：

```http
GET https://jindi-oss-open.oss-cn-beijing.aliyuncs.com/document/category.json
```

说明：

- 不带 `Authorization`。
- 拉取后会展开为一级、二级、三级行业扁平列表。
- 进程内缓存到 `_category_cache`。

---

## 11. 企业字段说明

接口返回的 `company` / `companies[]` 常见字段如下：

| 字段 | 说明 |
|------|------|
| `id` | 本地数据库 ID |
| `tianyancha_id` | 天眼查企业 ID |
| `name` | 企业名称 |
| `credit_code` | 统一社会信用代码 |
| `reg_number` | 注册号 |
| `org_number` | 组织机构代码 |
| `reg_status` | 经营状态 |
| `reg_capital` | 注册资本 |
| `legal_person_name` | 法定代表人 |
| `base` | 省份或地区 |
| `city` | 城市 |
| `district` | 区县 |
| `district_code` | 区县代码 |
| `industry` | 行业 |
| `category` | 行业大类 |
| `business_scope` | 经营范围 |
| `reg_location` | 注册地址 |
| `staff_num_range` | 人员规模 |
| `tags` | 标签 |
| `search_seen_at` | 最近一次搜索命中时间 |
| `baseinfo_fetched_at` | 最近一次详情拉取时间 |

`GET /api/tianyancha/company/{keyword}` 返回详情时会包含更多原始字段：

- `raw_search`：远程搜索接口原始企业数据。
- `raw_baseinfo`：远程详情接口原始企业数据。

---

## 12. 缓存与成本控制

### 12.1 搜索缓存

`POST /api/tianyancha/search` 会将搜索参数序列化后生成 SHA-256 指纹：

```text
fingerprint = sha256(sorted_json(params))
```

缓存命中条件：

- 参数完全一致。
- 缓存未超过 `TIANYANCHA_SEARCH_CACHE_TTL_SECONDS`。
- 请求未设置 `force_remote=true`。

### 12.2 企业详情缓存

企业基本信息通过 `baseinfo_fetched_at` 判断是否过期：

- 默认 TTL：`TIANYANCHA_BASEINFO_TTL_DAYS=30`
- 未过期时优先使用本地数据。
- `force_remote=true` 可强制刷新单个企业。
- 搜索接口中 `refresh_detail=true` 可强制刷新补拉详情。

### 12.3 成本控制建议

| 场景 | 推荐参数 |
|------|----------|
| 智能体区域调研初筛 | `detail_level=summary` |
| 只需要本地已有数据 | 使用 `GET /api/tianyancha/companies` |
| 需要少量重点企业详情 | `enrich_detail=true` 且设置较小 `max_detail_calls` |
| 需要强制最新结果 | `force_remote=true` |
| 降低远程调用次数 | 保持 `force_remote=false`，优先利用搜索缓存 |

---

## 13. 错误码

天眼查远程错误码会转成本地标准响应。

| 天眼查错误码 | 说明 | 本地 HTTP / 业务码 |
|--------------|------|------------------|
| `0` | 请求成功 | `200` |
| `300000` | 无数据 | 搜索中作为 warning，详情中通常转 `404` |
| `300001` | 请求失败 | `502` |
| `300002` | 账号失效 | `401` |
| `300003` | 账号过期 | `401` |
| `300004` | 访问频率过快 | `429` |
| `300005` | 无权限访问此 API | `403` |
| `300006` | 余额不足 | `402` |
| `300007` | 剩余次数不足 | `402` |
| `300008` | 缺少必要参数 | `502` |
| `300009` | 账号信息有误 | `401` |
| `300010` | URL 不存在 | `404` |
| `300011` | 此 IP 无权限访问此 API | `403` |
| `300012` | 报告生成中 | `502` |

网络异常，例如连接超时、上游非 2xx，会返回类似：

```json
{
  "code": 502,
  "message": "天眼查网络请求失败: ...",
  "data": null
}
```

---

## 14. 推荐集成方式

### 14.1 Dify / 智能体

优先使用：

```http
POST /api/tianyancha/research/region-companies
```

推荐默认参数：

```json
{
  "region": "深圳市",
  "industry": "软件和信息技术服务业",
  "keywords": ["人工智能"],
  "limit": 20,
  "detail_level": "summary",
  "force_remote": false
}
```

如返回 `need_clarification=true`，应让用户或工作流选择候选地区 / 行业代码后重试。

### 14.2 前端企业搜索页

使用：

```http
POST /api/tianyancha/search
```

前端可先调用：

```http
GET /api/tianyancha/resolve/area?region=深圳
GET /api/tianyancha/resolve/category?industry=软件
```

拿到明确代码后再搜索，减少歧义。

### 14.3 后台本地企业库

使用：

```http
GET /api/tianyancha/companies
```

该接口不产生天眼查远程调用成本。

