# 粉笔招考 API 使用说明（`/api/scrape/fenbi/*`）

本文档描述本服务中与粉笔（fenbi.com）相关的 HTTP 接口。应用入口见 [`main.py`](../main.py)：路由前缀为 **`/api`**，故完整路径形如 **`POST http://<host>:<port>/api/scrape/fenbi/article`**。默认本地开发端口以你启动 `uvicorn` 为准（仓库中 `main` 常用 `2906`）。

---

## 1. 通用约定

### 1.1 方法与请求头

| 项 | 说明 |
|----|------|
| 方法 | 本文档所列接口均为 **POST**（含无业务字段的占位 body） |
| `Content-Type` | **`application/json`** |
| 字符编码 | UTF-8 |

### 1.2 统一响应包体

成功与业务失败时，响应体均采用同一外层结构（由本服务封装，**不是**粉笔原始 JSON）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | 业务状态：成功为 **200**；错误时为 **4xx/5xx**（与 HTTP 状态码一致） |
| `message` | string | 说明信息 |
| `data` | object / null | 成功时为具体载荷；失败时多为 `null` |
| `timestamp` | string | ISO 8601 时间戳 |

**成功示例：**

```json
{
  "code": 200,
  "message": "Success",
  "data": { },
  "timestamp": "2025-03-23T12:00:00.000000"
}
```

**失败示例：**

```json
{
  "code": 400,
  "message": "无法解析粉笔公告 id: 'xxx'",
  "data": null,
  "timestamp": "2025-03-23T12:00:00.000000"
}
```

### 1.3 HTTP 状态码（常见）

| HTTP | 含义 |
|------|------|
| 200 | 请求被处理；是否成功请看 `code` |
| 400 | 参数不合法（如公告 id 无法解析） |
| 404 | 资源不存在（如 Hera 无该公告） |
| 502 | 上游粉笔/Hera 不可用、非 JSON、或业务错误被映射为网关错误 |

### 1.4 后端实际调用的上游

| 用途 | 基址 / 路径（概念） |
|------|---------------------|
| 公告摘要与 HTML 正文 | `https://hera-webapp.fenbi.com`（`/api/website/article/summary`、`contentURL` 或 `/api/article/detail`） |
| 考试日历、招考列表、筛选条件、职位列表/详情 | `https://market-api.fenbi.com/toolkit/api/v1/pc/...`（带 `app`、`av`、`hav`、`kav` 等 query） |

官网改版可能导致字段变化；若接口突然失败，需对照官网 Network 核对路径与参数。

---

## 2. ID 与页面类型（必读）

避免混用不同体系的 id：

| 概念 | 典型字段 / 场景 | 能否直接喂给 `/scrape/fenbi/article` |
|------|-----------------|--------------------------------------|
| **招考公告 id（Hera）** | 列表 `queryByCondition` 返回文章对象上的 **`id`**；URL `.../exam-information-detail/{id}` | **可以** |
| **考试 examId** | 列表项中的 `examId`，时间线相关 | **不能**当作公告 id；需走时间线页或官网其它入口 |
| **时间线条目 id** | `getTimeLineDetails` 返回的 `datas` 内 id | **不一定**等于公告 id；说明见 [5.2](#52-post-apiscrapefenbitimeline) |
| **职位 position id** | `position/queryByConditions` 的 `datas[].id` | 用于 `/scrape/fenbi/positions` 的详情；**不是**公告 id |
| **职位详情里的 articleId** | `position/detail` 返回的 `articleId` | 一般为关联公告，可按需再调 `article` 拉正文 |

---

## 3. 接口一览

我们将复杂的粉笔官网交互简化为“一 GET 一 POST”两个终极接口：
- 使用 `GET /api/scrape/fenbi/options` 获取全部基础配置和完整的专业层级树。
- 使用 `POST /api/scrape/fenbi/data` 执行查公告、查职位的扁平化查询。

| 路径 | 摘要 |
|------|------|
| [`/api/scrape/fenbi/article`](#31-post-apiscrapefenbiarticle) | 单条公告：Hera 摘要 + 正文提取 |
| [`/api/scrape/fenbi/timeline`](#32-post-apiscrapefenbitimeline) | （已废弃，推荐用 data 接口）考试日历时间线 |
| [`/api/scrape/fenbi/home-links`](#33-post-apiscrapefenbihome-links) | 首页 SSR 可见的招考详情链接 |
| [`/api/scrape/fenbi/options`](#34-get-apiscrapefenbioptions) | **【强烈推荐】** 获取包含完整级联专业树的全局筛选项数据 |
| [`/api/scrape/fenbi/data`](#35-post-apiscrapefenbidata) | **【核心接口】** 扁平化参数，一次性/按需检索招考公告和职位列表 |

---

## 3.1 `POST /api/scrape/fenbi/article`

根据 **Hera 公告 id** 拉取结构化字段与正文（Markdown/纯文本风格，由解析逻辑决定）。

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `article_id` | string | 是 | 纯数字 id，或详情页 URL，或 URL 中带 `id=` |

**`article_id` 支持形式示例：**

- `"464861463860224"`
- `"https://fenbi.com/page/exam-information-detail/464861463860224"`

### 成功时 `data` 主要字段

| 字段 | 说明 |
|------|------|
| `article_id` | 字符串形式的公告 id |
| `title` / `source` | 标题、来源 |
| `issue_time_ms` / `update_time_ms` | 时间戳（毫秒，上游字段） |
| `business_type` / `content_type` / `favorite_num` | 上游元数据 |
| `detail_url` | 实际拉取 HTML 的地址 |
| `summary_api` | 摘要接口 URL（便于排查） |
| `content_text` | 抽取的正文 |
| `content_chars` | 正文字符数 |

`message` 在正文过短时可能提示「正文较短…」。

### 请求示例

```json
{
  "article_id": "464018322510848"
}
```

---

## 3.2 `POST /api/scrape/fenbi/timeline`

对应 market-api：**`GET .../exam/getTimeLineDetails`**（查询参数由服务端组装）。

### 请求体

| 字段 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `district_id` | int | 否 | 0 | — | 地区 id，`0` 常表示全国/不限 |
| `offset` | int | 否 | 0 | ≥ 0 | 分页偏移 |
| `size` | int | 否 | 10 | 1～50 | 每页条数 |

### 成功时 `data`

| 字段 | 说明 |
|------|------|
| `total` | 上游返回的总数（若有） |
| `items` | 时间线条目数组（上游 `datas`） |
| `raw_code` / `raw_msg` | 上游 `code` / `msg`，便于对账 |

**注意：** 时间线条目的 `id` 与「招考公告详情 id」不是同一套体系时，**不要**直接当作 `article_id` 调用 [3.1](#31-post-apiscrapefenbiarticle)。

### 请求示例

```json
{
  "district_id": 836,
  "offset": 0,
  "size": 10
}
```

---

## 3.3 `POST /api/scrape/fenbi/home-links`

抓取 `https://fenbi.com/` HTML，用正则提取首页上出现的 **`/page/exam-information-detail/{id}`** 链接（依赖 SSR 输出，条数与排序随官网变化）。

### 请求体

| 字段 | 类型 | 必填 | 默认 | 约束 |
|------|------|------|------|------|
| `max_links` | int | 否 | 80 | 1～200 |

### 成功时 `data`

| 字段 | 说明 |
|------|------|
| `total` | 本次解析到的条数 |
| `items` | `{ "article_id", "title", "url" }[]` |

---

## 3.4 `POST /api/scrape/fenbi/conditions`

代理 **`GET .../exam/conditions`**，返回官网招考资讯列表页使用的筛选维度（地区、考试类型、年份、报名状态、招录人数区间等）。**建议先调本接口**，把返回里的 `value`/`code` 填到 [3.6](#36-post-apiscrapefenbiannouncements)。

### 请求体

空对象即可（占位，便于扩展）：

```json
{}
```

### 成功时 `data`

为上游 `data` 对象，结构以实时响应为准（通常含 `districtList`、`examTypeList`、年份列表、`enrollStatusList`、`recruitNumList` 等）。

---

## 3.5 `POST /api/scrape/fenbi/position-conditions`

代理 **`GET .../position/commonConditions`**，用于职位库页筛选（考试类型、地址、专业级联选项等，结构以实时响应为准）。

### 请求体

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `exam_type` | int | 是 | 考试类型，如 `4` 表示事业单位（与官网一致） |
| `exam_id` | int | 否 | 指定某场考试后，与官网「单场考试职位」页一致 |

### 请求示例

```json
{
  "exam_type": 4,
  "exam_id": 536633
}
```

---

## 3.5.1 `POST /api/scrape/fenbi/position-majors`

代理 **`GET .../major/listByLevel`**，用于职位库页获取专业的级联选项（专业门类 -> 学科 -> 专业）。
必须结合 [3.5](#35-post-apiscrapefenbi-position-conditions) 返回的 `majorTypeId` 和 `majorDegrees` 来使用。

### 请求体

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `major_type_id` | int | 是 | — | 对应 3.5 接口返回的 `majorTypeId` (如 102) |
| `major_degree` | int | 是 | — | 学历档，对应 3.5 返回的 `majorDegrees` 中的 `value` (如 2=本科，4=硕士) |
| `parent_code` | string | 否 | null | 上级节点编码。查一级（门类）时不传；查二级（学科）时传一级的结果 `value`；查三级（专业）时传二级的 `value` |

### 请求示例（查本科的一级分类：专业门类）

```json
{
  "major_type_id": 102,
  "major_degree": 2
}
```

**响应示例：**

```json
{
  "code": 200,
  "message": "Success",
  "data": [
    { "value": "ID_210399", "name": "哲学" },
    { "value": "ID_210405", "name": "经济学" }
  ]
}
```

### 请求示例（查本科下的“哲学”门类 -> 学科）

```json
{
  "major_type_id": 102,
  "major_degree": 2,
  "parent_code": "ID_210399"
}
```

---

## 3.6 `POST /api/scrape/fenbi/announcements`

代理官网 **`POST .../exam/queryByCondition`**，按条件分页拉取招考资讯列表；可选对本页结果批量拉 Hera 正文。

### 与官网 URL 查询参数的对应关系

| 本请求字段 | 官网列表 URL 常见参数 | 发往上游 JSON 字段 |
|------------|----------------------|-------------------|
| `exam_type` | `type` | `examType` |
| `district_id` | `region` | `districtId` |
| `year` | `year` | `year` |
| `enroll_status` | `registration` | `enrollStatus` |
| `recruit_num_code` | `recruitment` | `recruitNumCode` |
| `start` / `page_size` | （分页） | `start` / `len` |

### 请求体

| 字段 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `district_id` | int | 否 | 0 | — | 地区 id |
| `exam_type` | int | 否 | 4 | — | 考试类型 |
| `year` | int | 否 | 2025 | — | 年份 |
| `enroll_status` | int | 否 | 0 | — | 报名状态（合法值见 conditions） |
| `recruit_num_code` | int | 否 | 0 | — | 招录人数区间编码 |
| `start` | int | 否 | 0 | ≥ 0 | 分页起始 |
| `page_size` | int | 否 | 20 | 1～50 | 每页条数 |
| `need_total` | bool | 否 | true | — | 是否请求 total |
| `title_keyword` | string | 否 | null | — | **非粉笔接口**：仅对本服务**当前页**合并结果（置顶+列表）的标题做**子串**过滤，忽略大小写 |
| `include_detail` | bool | 否 | false | — | 是否并发拉 Hera 正文 |
| `max_details` | int | 否 | 5 | 0～30 | 最多拉几条详情（按合并后顺序取前 N 条有 `article_id` 的项） |
| `detail_concurrency` | int | 否 | 3 | 1～10 | 拉详情并发数 |

### 成功时 `data`

| 字段 | 说明 |
|------|------|
| `total` | 上游返回的总条数（在 `need_total` 为 true 时一般有值；若经 `title_keyword` 过滤，`items` 可能变少但 `total` 仍为上游语义） |
| `stick_top_count` | 置顶条数 |
| `page_items_count` | 当前页非置顶列表条数 |
| `items` | 见下表 |
| `warnings` | 字符串数组，如详情截断提示 |
| `upstream` | `{ "path", "request_body" }`，便于排查 |

**`items[]` 每项：**

| 字段 | 说明 |
|------|------|
| `article_id` | 字符串，与 `list_row.id` 一致，**可直接用于 [3.1](#31-post-apiscrapefenbiarticle)** |
| `list_row` | 上游文章对象原文（含 `title`、`examId`、`tagsList`、`announcementArticleInfoRet` 等） |
| `detail` | 若开启 `include_detail` 且该 id 在拉取范围内：与 [3.1](#31-post-apiscrapefenbiarticle) `data` 同结构 |
| `detail_error` | 若拉取失败：错误说明字符串 |

**处理顺序说明：** 服务端将 **置顶 `stickTopArticles` 与当前页 `articles` 合并** 后再做 `title_keyword` 过滤与详情拉取。

### 请求示例（仅列表）

```json
{
  "district_id": 836,
  "exam_type": 4,
  "year": 2025,
  "enroll_status": 0,
  "recruit_num_code": 0,
  "start": 0,
  "page_size": 10,
  "need_total": true
}
```

### 请求示例（列表 + 前 3 条正文）

```json
{
  "district_id": 836,
  "exam_type": 4,
  "year": 2025,
  "start": 0,
  "page_size": 10,
  "include_detail": true,
  "max_details": 3,
  "detail_concurrency": 2
}
```

---


---

## 4. 推荐调用顺序（典型场景）

1. **获取查询条件字典**  
   使用 `GET /api/scrape/fenbi/options` 拿到所有的 `exam_type`、`district_id` 以及嵌套的 `major_tree`。
2. **查职位或看公告**  
   拿着这些 id，填充到 `POST /api/scrape/fenbi/data` 的对应字段发起请求。如果还需要详情内容，带上 `include_detail=true`。

---

## 5. 性能、限流与稳定性

- **`data` 开详情**：会对 Hera 发起多次请求，请合理设置 `max_details` 与 `detail_concurrency`，避免触发上游限流。
- **正文质量**：依赖 Hera HTML 结构及可选 `trafilatura`；极短正文时 `article` 的 `message` 会提示可能需调整规则。
- **合规**：请遵守粉笔服务条款与 robots/使用政策；本服务仅作技术转发与解析，不保证官网长期兼容。

---

## 6. 文档与代码同步

接口实现位置：[ `api/fenbi_recruit.py` ](../api/fenbi_recruit.py)。若行为与本文档不一致，以代码为准，并建议更新本文档。
