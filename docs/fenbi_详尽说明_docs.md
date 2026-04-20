# 粉笔网关 API 详尽说明（GET meta + POST action）

本文档面向**集成方、前端、自动化脚本与 AI Agent**，说明 [`api/fenbi_gateway.py`](api/fenbi_gateway.py) 暴露的两个接口如何组合使用。

- **Base URL**：`http://<host>:<port>/api`（端口以你启动 `uvicorn` 为准，仓库 `main.py` 常见为 `2906`）。
- **粉笔接口**：
  - `GET /api/scrape/fenbi/meta` — 字典与可选职位侧条件、专业树。
  - `POST /api/scrape/fenbi/action` — 统一数据动作（公告列表、职位列表、单条正文、单条职位详情、组合查询）。

上游域名与官网 PC 一致：`market-api.fenbi.com`、`hera-webapp.fenbi.com`。列表项内 **`list_row` 及职位详情原始结构** 随粉笔改版可能变化，集成时应**以实际 JSON 为准**，本文对上游字段作**典型说明**而非固定契约。

---

## 1. 统一外层响应（所有成功/失败）

本服务**不直接返回**粉笔原始 HTTP 包，而是统一为：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | 业务码。成功为 **200**；参数错误多为 **400**；资源不存在 **404**；上游失败 **502**。 |
| `message` | string | 人类可读说明；成功时也可能含提示（如正文过短）。 |
| `data` | object / array / null | 成功时为业务载荷；失败时多为 `null`。 |
| `timestamp` | string | ISO 8601 时间戳。 |

**注意**：HTTP 状态码与 `code` 在错误时通常一致；成功时 HTTP 一般为 200，以 `data` 为准。

---

## 2. `GET /api/scrape/fenbi/meta`

### 2.1 作用概述

1. **始终**拉取招考侧 **`exam/conditions`**，得到地区、考试类型、年份、报名状态、招录人数区间等枚举（本服务映射为 `districts`、`exam_types` 等顶层字段）。
2. **可选**：传入 **`exam_type`** 时，再拉该考试类型下的 **`position/commonConditions`**，得到职位筛选用的 `majorTypeId`、`majorDegrees` 及官网定义的其它职位侧条件（整体放在 `position_conditions`）。
3. **可选**：**`expand_majors=1`** 且已传 **`exam_type`** 时，按学历维度展开**三级专业树** `major_tree`（请求耗时会明显增加）。

### 2.2 Query 参数

| 参数 | 类型 | 必填 | 约束 | 说明 |
|------|------|------|------|------|
| `exam_type` | int | 否 | — | 与 `exam_types[].value`（或等价字段）一致，表示**当前业务要选中的考试大类**（国考/省考/事业单位等）。不传则**不请求** `commonConditions`，`position_conditions` 与 `major_tree` 为 `null`。 |
| `expand_majors` | int | 否 | 仅允许 **0** 或 **1** | **1** 表示在已传 `exam_type` 的前提下展开完整专业树；**0** 不展开。若 `expand_majors=1` 但未传 `exam_type`，接口返回 **400**。 |

### 2.3 GET 调用场景组合（Case 表）

| Case ID | `exam_type` | `expand_majors` | 典型场景 | 行为摘要 |
|---------|-------------|-----------------|----------|----------|
| G1 | 不传 | 0（默认） | 仅需全局字典：有哪些考试类型、省份、年份；尚未选定考试大类 | 只打 `exam/conditions`；轻量、最快；适合 App 首屏、配置中心。 |
| G2 | 传，如 `4` | 0 | 已选定「事业单位」等，需要职位筛选用的 `majorDegrees`、其它职位条件，但**不需要**展开上千节点专业树 | 打 `exam/conditions` + `commonConditions`；`major_tree` 仍为 `null`。 |
| G3 | 传 | 1 | 职位检索要**级联选专业**（门类→学科→专业），需一次性拿树给前端 | 在 G2 基础上再并发拉三级 `major/listByLevel`；**慢、体量大**，建议配合缓存结果前端落盘。 |
| G4 | 不传 | 1 | **非法** | 返回 400：`expand_majors=1 时必须同时提供 exam_type`。 |

**缓存说明**：本服务对 **整包 `data`（在写入 `cached` 字段之前）** 按键 `(exam_type, expand_majors)` 做内存缓存，TTL 为代码常量 **`META_CACHE_TTL_SEC`（当前 600 秒）**。响应中：

- `cached: true` — 命中缓存（短时间内重复请求同参数组合）。
- `cached: false` — 刚穿透上游。
- `cache_ttl_sec` — TTL 秒数说明，便于客户端估算何时刷新 meta。

---

### 2.4 `data` 字段详解（成功时）

以下字段在 **`code===200` 且 `data` 非 null** 时出现（除非另有说明）。

#### 2.4.1 招考侧枚举（来自 `exam/conditions` 的 `data`）

| 字段 | 类型 | 说明 | 典型用途 |
|------|------|------|----------|
| `districts` | array | 对应上游 `districtList`。每项一般为带 **`name` / `value`（或 id）** 的对象，表示省份/地区。 | 填 POST **`district_id`**：取选中项的 **数值 id**（常见为 `value`）。**0** 常表示全国或未选省，具体以返回项为准。 |
| `exam_types` | array | 对应 `examTypeList`。 | 填 POST **`exam_type`**（列表类 `op` **必填**）。**注意**：`0` 是合法值（如国考），不要用「假值」判断省略。 |
| `years` | array | 对应 `yearList`。 | 填 POST **`year`**，筛选招考年度。 |
| `enroll_statuses` | array | 对应 `enrollStatusList`。 | 填 POST **`enroll_status`**。 |
| `recruit_nums` | array | 对应 `recruitNumList`。 | 填 POST **`recruit_num_code`**（招录人数区间编码）。 |

**应用方式小结**：把上述列表当作**下拉框数据源**；用户选中后，把对应 **id/value（整数）** 原样填入 POST 的同名语义字段。

#### 2.4.2 `position_conditions`（仅当请求带 `exam_type`）

| 值 | 说明 |
|----|------|
| `null` | 未传 `exam_type`，本服务未请求 `commonConditions`。 |
| object | 上游 `commonConditions` 的 **`data` 原样**（本服务不删减键）。 |

**常见关键子字段（典型，以实际 JSON 为准）**：

| 子字段（典型） | 说明 | 如何用于 POST |
|----------------|------|----------------|
| `majorTypeId` | 当前考试类型下专业体系类型 id | **不直接**出现在 POST 顶层；服务端在 GET 展开 `major_tree` 时已使用。 |
| `majorDegrees` | 学历档位列表，元素多含 **`name` / `value`** | 用户选中学历后，将 **`value` 整数** 填入 POST **`major_degree`**（仅 `positions` / `both` 有意义）。 |
| 其它筛选项 | 如政治面貌、职位类型等（名称随考试类型变化） | 若官网职位检索使用 **`optionContents`** 结构，需从返回对象中构造与官网一致的 **JSON 片段**，填入 POST **`option_contents`**（数组，见下文 POST 说明）。 |

#### 2.4.3 `major_tree`（仅当 `expand_majors=1` 且成功展开）

| 值 | 说明 |
|----|------|
| `null` | 未展开，或上游无 `majorTypeId`/`majorDegrees` 导致无法展开。 |
| object | **键**：`majorDegrees` 中每一项的 **`value` 的字符串形式**（如 `"2"` 表示本科）。**值**：该学历下的**树根数组**。 |

**树节点结构（本服务组装）**：

```text
{ "name": string, "value": string, "children": [ 子节点... ] }
```

- **叶子或末级节点**的 `value`（如 `ID_xxxx`）通常对应职位查询的 **`majorCode`**。
- **应用方式**：UI 级联选中叶子后，将 **`major_code`** 设为该 `value` 字符串；同时 **`major_degree`** 设为当前树所在键对应的学历整型（即你请求展开时用的那档学历的 `value`）。

#### 2.4.4 元信息字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `cache_ttl_sec` | int | 服务端对该 meta 组合的缓存秒数（当前实现为 600）。 |
| `exam_type_requested` | int / null | 回显本次请求传入的 `exam_type`；未传则为 `null`。 |
| `expand_majors` | bool | 是否请求了专业树展开。 |
| `cached` | bool | 是否命中服务端内存缓存。 |

---

## 3. `POST /api/scrape/fenbi/action`

### 3.1 请求约定

- **Header**：`Content-Type: application/json`
- **Body**：JSON 对象，**必须**包含 **`op`**；其余字段按 `op` 选填或必填。

### 3.2 `op` 与校验规则

| `op` | 必填字段 | 列表类共用字段是否参与 |
|------|----------|------------------------|
| `announcements` | `exam_type` | 是（见 3.3） |
| `positions` | `exam_type` | 是 |
| `both` | `exam_type` | 是（**同一组参数**同时驱动公告列表与职位列表） |
| `article` | `article_id` | 否（列表字段可忽略） |
| `position_detail` | `position_id` | 否 |

若违反必填，FastAPI/Pydantic 会返回 **422**（校验错误）或本服务对 `article` 等业务返回 **400** 说明。

### 3.3 列表类共用字段（`announcements` / `positions` / `both`）

| 字段 | 类型 | 默认 | 约束 | 说明 |
|------|------|------|------|------|
| `exam_type` | int | — | **必填** | 来自 GET `exam_types` 选中项的 value。 |
| `district_id` | int | `0` | — | 来自 GET `districts`；**0** 表示官网语义下的全国/未指定省（与上游一致）。 |
| `year` | int | 当前自然年 | — | 来自 GET `years`。 |
| `enroll_status` | int | `0` | — | 来自 GET `enroll_statuses`。 |
| `recruit_num_code` | int | `0` | — | 来自 GET `recruit_nums`。 |
| `start` | int | `0` | ≥0 | 分页起始偏移（从 0 开始）。 |
| `page_size` | int | `20` | 1–50 | 每页条数。 |
| `need_total` | bool | `true` | — | 是否向 upstream 请求总数（公告列表）。 |
| `title_keyword` | string | null | — | **仅公告列表**：在当前页合并结果上**本地**子串过滤标题（不增加上游筛选参数）；大小写不敏感。 |
| `include_detail` | bool | `false` | — | 为 `true` 时，对当前页条目**额外**拉详情（公告→Hera 正文；职位→market 详情）。 |
| `max_details` | int | `5` | 0–30 | 详情最大条数；超出部分不拉取并产生 `warnings`。 |
| `detail_concurrency` | int | `3` | 1–10 | 拉详情时的并发上限，防止打爆上游。 |
| `exam_id` | int | null | — | **仅职位列表**：限定某次考试（官网时间线/考试 id，来自列表行字段时需对照 `list_row`）。 |
| `major_degree` | int | null | — | **仅职位列表**：来自 GET `position_conditions.majorDegrees[].value`。 |
| `major_code` | string | null | — | **仅职位列表**：叶子专业 code，来自 `major_tree` 或官网等价控件。 |
| `option_contents` | array | null | — | **仅职位列表**：与官网 `optionContents` 一致的高级筛选结构（对象数组）。 |

### 3.4 单条类字段

| 字段 | 类型 | `op` | 说明 |
|------|------|------|------|
| `article_id` | string | `article` | 纯数字 id，或包含 `exam-information-detail/{id}` 的 URL，或带 `id=` 的 URL。 |
| `position_id` | int | `position_detail` | 职位 id，通常来自 `positions` 返回的 `items[].position_id` 或 `list_row.id`。 |

---

## 4. GET → POST 映射与填写流程（推荐）

### 4.1 标准集成顺序

1. **调用 `GET .../meta`（Case G1）**  
   - 渲染：考试类型、地区、年份、报名状态、招录人数区间。
2. **用户选定 `exam_type` 后**  
   - 再调 **`GET .../meta?exam_type=<选定值>&expand_majors=0`（Case G2）** 或 **`expand_majors=1`（Case G3）**  
   - 得到职位侧 `position_conditions` 与可选 `major_tree`。
3. **构造 `POST .../action`**  
   - 公告：`op=announcements`，把 meta 里选中的 id 填入 `exam_type`、`district_id`、`year` 等。  
   - 职位：`op=positions`，额外填 `major_degree`、`major_code`、`option_contents`（若需要）。  
   - 既要公告又要职位：**`op=both`**，一套参数共用。

### 4.2 字段映射速查表

| GET `data` 路径 | POST 字段 | 用于 `op` |
|-----------------|-----------|-----------|
| `exam_types[].value` | `exam_type` | announcements, positions, both |
| `districts[].value`（或等价 id） | `district_id` | 同上 |
| `years[].value` | `year` | 同上 |
| `enroll_statuses[].value` | `enroll_status` | 同上 |
| `recruit_nums[].value` | `recruit_num_code` | 同上 |
| `position_conditions.majorDegrees[].value` | `major_degree` | positions, both |
| `major_tree` 叶子 `value` | `major_code` | positions, both |
| 列表项 `article_id` / 官网 URL | `article_id` | article |
| 列表项 `position_id` | `position_id` | position_detail |

---

## 5. POST 各 `op` 的响应结构（`data` 部分）

### 5.1 `op = announcements`

| 字段 | 说明 |
|------|------|
| `total` | 上游返回的总条数（若 `need_total` 为 false 可能为 null，视上游而定）。 |
| `stick_top_count` | 置顶公告条数。 |
| `page_items_count` | 当前页非置顶公告条数。 |
| `items` | 数组；每项含 `article_id`、`list_row`（上游文章对象，**典型**含 `id`、`title`、`issueTime` 等）。 |
| `items[].detail` | 若 `include_detail=true` 且成功：Hera 汇总+正文结构（见 5.4 与下表）。 |
| `items[].detail_error` | 详情失败时的错误说明。 |
| `warnings` | 字符串数组，如详情截断提示。 |
| `upstream` | `path` + `request_body`，便于排查。 |

### 5.2 `op = positions`

| 字段 | 说明 |
|------|------|
| `total` | 职位总条数（上游字段）。 |
| `items` | 每项 `position_id`、`list_row`（上游职位行）。 |
| `items[].detail` | `include_detail` 时 `position/detail` 的 `data`。 |
| `items[].detail_error` | 详情失败说明。 |
| `warnings` | 同上。 |
| `upstream` | 同上。 |

### 5.3 `op = both`

`data` 为对象：

```json
{
  "announcements": { /* 同 5.1 */ },
  "positions": { /* 同 5.2 */ }
}
```

**应用场景**：选岗大屏、备考助手同时展示「公告列表 + 可报职位」；**注意**两边共用同一套筛选，若需不同筛选应发两次 POST。

### 5.4 `op = article`（单条公告正文）

`data` 为本服务组装对象（非上游单层原始体）：

| 字段 | 说明 |
|------|------|
| `article_id` | 字符串 id。 |
| `title` / `source` | 标题、来源。 |
| `issue_time_ms` / `update_time_ms` | 毫秒时间戳。 |
| `business_type` / `content_type` / `favorite_num` | 上游元数据。 |
| `detail_url` | 实际拉取 HTML 的 URL。 |
| `summary_api` | 摘要接口地址。 |
| `content_text` | 从 HTML 抽取的正文（可能为 Markdown 风格，视 trafilatura/回退逻辑而定）。 |
| `content_chars` | 正文字符数。 |

`message` 可能提示正文过短（解析规则或页面结构变化）。

### 5.5 `op = position_detail`

`data` **直接为**上游 `position/detail` 返回的 **`data` 对象**（字段随粉笔业务变化）。**典型**可能含：职位名称、用人单位、`articleId`（关联公告，可再调 `op=article`）、专业要求等。

---

## 6. 应用场景与完整请求示例

以下示例中 `BASE` 请替换为 `http://127.0.0.1:2906/api` 等。

### 场景 A：App 首屏——只展示考试类型与省份

- **GET**：`GET {BASE}/scrape/fenbi/meta`（Case G1）  
- **POST**：无。  
- **目的**：最轻量，避免不必要 `commonConditions`。

### 场景 B：用户选了「事业单位」——要筛专业但不展开整棵树

1. `GET {BASE}/scrape/fenbi/meta?exam_type=4&expand_majors=0`（Case G2）  
2. `POST {BASE}/scrape/fenbi/action`  
   ```json
   {
     "op": "positions",
     "exam_type": 4,
     "district_id": 159,
     "year": 2026,
     "start": 0,
     "page_size": 20,
     "major_degree": 2,
     "major_code": "ID_210405"
   }
   ```  
   `major_degree` / `major_code` 来自用户在前端选择的学历与专业（数据源自 G2 的 `position_conditions` + 若前端自行逐级请求 `listByLevel` 也可不依赖 `expand_majors`）。

### 场景 C：职位页一次拿全专业树（重请求）

- **GET**：`GET {BASE}/scrape/fenbi/meta?exam_type=4&expand_majors=1`（Case G3）  
- **用途**：离线缓存专业树、搜索框自动完成、树形选择器。  
- **注意**：体量大、耗时长；建议客户端持久化，不要每次打开页都全量拉。

### 场景 D：只要公告列表，且标题含关键字

```json
POST {BASE}/scrape/fenbi/action
{
  "op": "announcements",
  "exam_type": 4,
  "district_id": 0,
  "year": 2026,
  "start": 0,
  "page_size": 20,
  "title_keyword": "税务"
}
```

说明：`title_keyword` **只过滤当前页**合并后的结果，不是上游全文搜索；要全覆盖需翻页循环。

### 场景 E：列表 + 前 N 条带正文（控制成本）

```json
POST {BASE}/scrape/fenbi/action
{
  "op": "announcements",
  "exam_type": 4,
  "district_id": 336,
  "year": 2026,
  "include_detail": true,
  "max_details": 3,
  "detail_concurrency": 2
}
```

**应用**：摘要预览、RAG 抽样；**勿**把 `max_details` 调得过大以免触发上游限流。

### 场景 F：已知官网详情页链接——只拉正文

```json
POST {BASE}/scrape/fenbi/action
{
  "op": "article",
  "article_id": "https://fenbi.com/page/exam-information-detail/464861463860224"
}
```

### 场景 G：从职位列表点进详情

先 `op=positions` 得到 `items[0].position_id`，再：

```json
POST {BASE}/scrape/fenbi/action
{
  "op": "position_detail",
  "position_id": 123456789
}
```

### 场景 H：组合大屏 `both`

```json
POST {BASE}/scrape/fenbi/action
{
  "op": "both",
  "exam_type": 1,
  "district_id": 1978,
  "year": 2026,
  "enroll_status": 0,
  "recruit_num_code": 0,
  "start": 0,
  "page_size": 10
}
```

---

## 7. `option_contents` 进阶说明（职位高级筛选）

- **来源**：官网职位检索若使用 JSON 筛选器，结构体现在浏览器 **Network** 的 `position/queryByConditions` 请求体中。  
- **填写**：将相同结构的数组赋给 POST 的 **`option_contents`**（本服务 **key 名**为 `option_contents`，上游 JSON 内仍为 `optionContents` 由服务端转换）。  
- **场景**：政治面貌、基层经验、是否应届等无法用单一 `major_code` 表达的条件。  
- **风险**：结构强依赖官网版本，升级后需重新对照 Network。

---

## 8. 分页与一致性

- **`start` / `page_size`**：公告与职位均支持；翻页时递增 `start`（步长一般为 `page_size`）。  
- **`title_keyword`**：仅影响本服务内存过滤后的当前页，**总条数 `total` 仍反映上游未加关键字前的统计**（若需「全局关键字搜索」需产品层自行循环或多接口策略）。  
- **`both`**：公告与职位使用**相同**筛选维度；若总数不一致属于业务常态（数据源不同）。

---

## 9. 错误与排障

| 现象 | 可能原因 | 建议 |
|------|----------|------|
| HTTP 400（meta） | `expand_majors=1` 未带 `exam_type` | 按 Case 表修正参数。 |
| HTTP 422 | POST 缺 `exam_type` / `article_id` / `position_id` | 对照 3.2 必填表。 |
| HTTP 404 | `article` 摘要返回未找到 | 核对 id 是否与 Hera 一致。 |
| HTTP 502 | 上游超时、非 JSON、业务 code≠1 | 看 `message`；对照 `upstream` 内 path/body；稍后重试。 |
| `detail_error` 有值 | 单条详情失败 | 重试该 id 或降并发。 |
| `list_row` 字段变了 | 粉笔改版 | 以新 JSON 为准更新你的解析逻辑。 |

---

## 10. 附录：官网职位库路径与考试类型数字（参考）

与 [fenbi.com 职位入口](https://fenbi.com/page/positions-exams) 常见路由对应关系（**以 GET `exam_types` 返回为准，此表仅助记**）：

| 路径片段 | 常见含义 |
|----------|----------|
| `/page/positions-exams/0` | 国考 |
| `/page/positions-exams/1` | 省考 |
| `/page/positions-exams/3` | 选调 |
| `/page/positions-exams/4` | 事业单位 |
| `/page/positions-exams/6` | 三支一扶 |
| `/page/positions-exams/8` | 招警 |
| `/page/positions-exams/9` | 国企 |

---

## 11. 附录 B：本服务实际请求的粉笔上游路径与参数（全量）

以下与 [`api/fenbi_gateway.py`](api/fenbi_gateway.py) 中 `HERA_ORIGIN`、`MARKET_PC`、`_market_qs()`、`_headers()` 及各处 `client.get` / `client.post` **逐项对应**。  
**说明**：上游 JSON 的 `code` 字段本服务按 **`1` 表示成功** 解析；响应体结构以粉笔实时返回为准，此处只列**本服务传入的路径与参数**。

### 11.1 基址与公共约定

| 常量 | 值 |
|------|-----|
| `HERA_ORIGIN` | `https://hera-webapp.fenbi.com` |
| `MARKET_PC` | `https://market-api.fenbi.com/toolkit/api/v1/pc` |

**所有**通过 `httpx.AsyncClient(headers=_headers(), ...)` 发出的请求，均携带以下 **HTTP 请求头**：

| 请求头 | 值 |
|--------|-----|
| `User-Agent` | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36` |
| `Referer` | `https://fenbi.com/` |
| `Origin` | `https://fenbi.com` |
| `Cookie` | **默认附带**（`sess` + `userid` 等，与官网登录态一致），使 `position/detail` 等返回完整结构化字段；**无** `Authorization` 头。 |

**Cookie 来源（优先级）**：

1. 若进程环境中 **已设置** `FENBI_COOKIE`（含 `.env` 经 `load_dotenv` 加载）：使用该字符串作为完整 `Cookie` 头；若值为仅空白则**不**发送 `Cookie`。  
2. 若 **未设置** `FENBI_COOKIE`：使用 [`fenbi_gateway.py`](api/fenbi_gateway.py) 内模块级默认 Cookie（与当前部署约定一致）。

**安全**：会话会过期；勿将含有效 `sess` 的代码或 `.env` 提交到公共仓库。示例键名见 [`.env.example`](.env.example)。

**Market-API 公共 Query（`_market_qs()`）**  
凡请求路径在 `MARKET_PC` 下的接口，均在 URL 上附加（`extra` 为可选扩展，与本表合并）：

| Query 参数 | 类型（逻辑） | 固定值 | 说明 |
|------------|----------------|--------|------|
| `app` | string | `web` | 客户端类型，与官网 PC Web 一致。 |
| `av` | int | `100` | 应用版本类参数（粉笔约定，含义以官方为准）。 |
| `hav` | int | `100` | 同上。 |
| `kav` | int | `100` | 同上。 |

`GET /meta` 使用独立 `httpx` 客户端（同上 `_headers()`）；`POST /action` 内列表与详情亦同上。  
**Hera** 请求（摘要、详情 HTML）**不**附带 `app/av/hav/kav`，仅使用上述浏览器头。

---

### 11.2 Market-API：`GET …/exam/conditions`

| 项 | 内容 |
|----|------|
| **完整路径** | `GET {MARKET_PC}/exam/conditions` |
| **即** | `https://market-api.fenbi.com/toolkit/api/v1/pc/exam/conditions` |
| **Query** | 仅 **11.1** 中公共四项：`app`、`av`、`hav`、`kav`。 |
| **Body** | 无。 |
| **触发本服务场景** | `GET /api/scrape/fenbi/meta`（任意 `exam_type` / `expand_majors` 组合在**未命中缓存**时必调）。 |
| **本服务使用的响应** | `data.districtList` → `districts`；`data.examTypeList` → `exam_types`；`data.yearList` → `years`；`data.enrollStatusList` → `enroll_statuses`；`data.recruitNumList` → `recruit_nums`。 |

---

### 11.3 Market-API：`GET …/position/commonConditions`

| 项 | 内容 |
|----|------|
| **完整路径** | `GET {MARKET_PC}/position/commonConditions` |
| **即** | `https://market-api.fenbi.com/toolkit/api/v1/pc/position/commonConditions` |
| **Query** | 公共四项 **加上**： |

| Query 参数 | 类型 | 说明 |
|------------|------|------|
| `app` / `av` / `hav` / `kav` | 同 11.1 | 必填（由 `_market_qs` 注入）。 |
| `examType` | string | **字符串形式**的考试类型 id（代码中为 `str(exam_type)`），与 `GET /meta` 的 query `exam_type` 一致。 |

- **Body**：无。  
- **触发本服务场景**：`GET /api/scrape/fenbi/meta` 且 **query 中传了 `exam_type`**。  
- **本服务使用的响应**：整个 `data` 对象放入 `position_conditions`；其中 `majorTypeId`、`majorDegrees` 在 `expand_majors=1` 时用于拉专业树。

---

### 11.4 Market-API：`GET …/major/listByLevel`（专业树，可多请求）

| 项 | 内容 |
|----|------|
| **完整路径** | `GET {MARKET_PC}/major/listByLevel` |
| **即** | `https://market-api.fenbi.com/toolkit/api/v1/pc/major/listByLevel` |

**Query（第一层：根级门类，无父节点）**

| Query 参数 | 说明 |
|------------|------|
| `app` / `av` / `hav` / `kav` | 同 11.1。 |
| `majorTypeId` | int，来自 `position_conditions.majorTypeId`。 |
| `majorDegree` | int，来自 `majorDegrees[].value`（对**每一种学历**并发一棵树）。 |

**Query（第二、三层：子级）**  
在上一层返回的每一项的 `value` 上继续请求，**额外增加**：

| Query 参数 | 说明 |
|------------|------|
| `parentCode` | string，上一级节点的 `value`（粉笔专业编码）。 |

- **触发本服务场景**：`GET /api/scrape/fenbi/meta?exam_type=…&expand_majors=1`，且 `majorTypeId` 与 `majorDegrees` 均存在时；对每个 `majorDegree` 并发整棵三级树，内部为 **1 + N1 + N2** 量级 GET（N1=第一层子节点数，N2=第二层节点数）。  
- **本服务使用的响应**：JSON 中列表字段 **`datas`**（元素含 `name`、`value` 等），组装为 `major_tree`。

---

### 11.5 Market-API：`POST …/exam/queryByCondition`（招考公告列表）

| 项 | 内容 |
|----|------|
| **完整路径** | `POST {MARKET_PC}/exam/queryByCondition` |
| **即** | `https://market-api.fenbi.com/toolkit/api/v1/pc/exam/queryByCondition` |
| **Query** | 仅公共四项：`app`、`av`、`hav`、`kav`（**无**其它 query）。 |
| **Header** | `Content-Type: application/json` + 11.1 浏览器头。 |

**JSON Body（本服务固定字段名，驼峰与上游一致）**

| 字段 | 类型 | 来源（本服务 POST body） | 说明 |
|------|------|---------------------------|------|
| `districtId` | int | `district_id` | 地区 id，`0` 常表示全国等。 |
| `examType` | int | `exam_type` | 考试类型。 |
| `year` | int | `year` | 年份。 |
| `enrollStatus` | int | `enroll_status` | 报名状态编码。 |
| `recruitNumCode` | int | `recruit_num_code` | 招录人数区间编码。 |
| `start` | int | `start` | 分页起始，从 0 起。 |
| `len` | int | `page_size` | 每页条数（本服务限制 1–50）。 |
| `needTotal` | bool | `need_total` | 是否请求总条数。 |

**触发本服务场景** | `POST /action` 且 `op` 为 `announcements` 或 `both`。 |

**本服务使用的响应** | `data.stickTopArticles`、`data.articles`、`data.total`；列表项上的公告 id 来自每条的 `id`。 |

---

### 11.6 Market-API：`POST …/position/queryByConditions`（职位列表）

| 项 | 内容 |
|----|------|
| **完整路径** | `POST {MARKET_PC}/position/queryByConditions` |
| **即** | `https://market-api.fenbi.com/toolkit/api/v1/pc/position/queryByConditions` |
| **Query** | 公共四项：`app`、`av`、`hav`、`kav`。 |
| **Header** | `Content-Type: application/json` + 11.1 浏览器头。 |

**JSON Body（本服务组装规则）**

| 字段 | 类型 | 来源 | 必填 | 说明 |
|------|------|------|------|------|
| `examType` | int | `exam_type` | 是 | 考试类型。 |
| `districtIds` | int[] | `district_id` | 是 | 本服务将单个 `district_id` 包成**单元素数组**传入（`[district_id]`）。 |
| `start` | int | `start` | 是 | 分页起始。 |
| `len` | int | `page_size` | 是 | 每页条数。 |
| `examId` | int | `exam_id` | 否 | 限定某次考试；仅当请求体提供了 `exam_id` 时加入 JSON。 |
| `majorDegree` | int | `major_degree` | 否 | 学历档；有值才加入。 |
| `majorCode` | string | `major_code` | 否 | 终端专业 code；**非空字符串**才加入。 |
| `optionContents` | array | `option_contents` | 否 | 高级筛选；仅当 `option_contents` 不为 `null` 时加入，**键名为上游的 `optionContents`**。 |

**触发本服务场景** | `POST /action` 且 `op` 为 `positions` 或 `both`。 |

**本服务使用的响应** | 顶层 **`datas`**（职位行数组）、**`total`**；职位 id 来自每行 **`id`**。 |

---

### 11.7 Market-API：`GET …/position/detail`（单条职位详情 JSON）

| 项 | 内容 |
|----|------|
| **完整路径** | `GET {MARKET_PC}/position/detail` |
| **即** | `https://market-api.fenbi.com/toolkit/api/v1/pc/position/detail` |
| **Query** | 公共四项 **加上**： |

| Query 参数 | 说明 |
|------------|------|
| `positionId` | int，职位主键（与列表 `datas[].id` 一致）。 |

| **Body** | 无。 |
| **触发本服务场景** | ① `POST /action` 且 `op` 为 `position_detail`；② `op` 为 `positions` 或 `both` 且 `include_detail=true` 时，对每条列表项的 `position_id` 并发调用（受 `max_details`、`detail_concurrency` 限制）。 |
| **本服务使用的响应** | 顶层 `data` **原样**作为网关 `data` 或列表项中的 `detail`（不做 HTML 解析）。 |

**与官网「详情页」关系**：官网若展示职位详情页，通常由该 JSON 渲染；**本服务不请求** `fenbi.com/page/...` 的 HTML 职位详情路由。

---

### 11.8 Hera：`GET …/api/website/article/summary`（公告摘要）

| 项 | 内容 |
|----|------|
| **完整路径** | `GET {HERA_ORIGIN}/api/website/article/summary` |
| **即** | `https://hera-webapp.fenbi.com/api/website/article/summary` |
| **Query** | 仅下表（**无** market 的 app/av/hav/kav）： |

| Query 参数 | 说明 |
|------------|------|
| `id` | string，Hera 公告 id（与列表 `articles[].id`、官网 `exam-information-detail/{id}` 一致）。 |

| **Body** | 无。 |
| **触发本服务场景** | `POST /action` 且 `op` 为 `article`；或公告列表 `include_detail=true` 时对每条 `article_id` 调用。 |
| **本服务使用的响应** | `data` 内 **`id`**、`title`、`source`、`issueTime`、`updateTime`、`businessType`、`contentType`、`favoriteNum`、**`contentURL`**（若有）等；若 `data.id` 缺失则视为未找到公告。 |

---

### 11.9 Hera：公告正文 HTML（第二次 GET）

| 项 | 内容 |
|----|------|
| **URL** | **优先** 使用摘要返回的 **`contentURL`**（可能为完整绝对 URL，指向 Hera 或其它域名，本服务 **原样 GET**）。 |
| **回退** | 若摘要无 `contentURL`，则 `GET {HERA_ORIGIN}/api/article/detail?id={article_id}`，即 `https://hera-webapp.fenbi.com/api/article/detail?id=<id>`。 |
| **Query** | 回退路径仅 `id`；若使用 `contentURL` 则不再附加本服务参数。 |
| **Body** | 无。 |
| **响应类型** | **HTML 文本**（非 JSON）。 |
| **本服务后续处理** | `_html_to_body`：可选 **trafilatura** 转 Markdown；否则 **BeautifulSoup** 按规则抽取正文，再 `_strip_noise`。 |

**与官网「详情页」关系**：用户浏览器中 `fenbi.com/page/exam-information-detail/...` 多为壳；**正文数据链路**通常经 Hera 摘要 + 上述 HTML 地址，与是否展示登录蒙层是不同层面问题。

---

### 11.10 按本服务接口汇总「会打到哪些上游」

| 本服务接口 | 本服务子场景 | 上游请求（顺序/并发因场景而异） |
|------------|--------------|----------------------------------|
| `GET /api/scrape/fenbi/meta` | 任意 | ① `GET …/exam/conditions` |
| 同上 | 带 `exam_type` | ① + ② `GET …/position/commonConditions?examType=…` |
| 同上 | `expand_majors=1` | ① + ② + 多次 `GET …/major/listByLevel` |
| `POST …/action` | `op=announcements` | `POST …/exam/queryByCondition`；若 `include_detail` 则每条再 **Hera summary + HTML** |
| 同上 | `op=positions` | `POST …/position/queryByConditions`；若 `include_detail` 则每条再 `GET …/position/detail` |
| 同上 | `op=both` | 上两行 **并发**（announcements + positions 各一套），详情逻辑同上 |
| 同上 | `op=article` | **Hera summary** + **HTML 详情** |
| 同上 | `op=position_detail` | **仅** `GET …/position/detail?positionId=…` |

---

### 11.11 参数与官网对照建议

- **枚举类 id**（`districtId`、`examType`、`year`、`enrollStatus`、`recruitNumCode`、`majorDegree`、`majorCode` 等）均以 **`GET …/meta`** 返回及 **`position/commonConditions`** 为准，勿手写猜测。  
- 若上游升级导致 path 或字段名变化，以浏览器 **Network** 中官网成功请求为准，并同步修改 [`fenbi_gateway.py`](api/fenbi_gateway.py)。

---

## 12. 文档与代码同步

- 实现与默认常量以 [`api/fenbi_gateway.py`](api/fenbi_gateway.py) 为准（如 `META_CACHE_TTL_SEC`、`page_size` 上限等）。  
- 精简版说明见 [`docs/fenbi.md`](docs/fenbi.md)。  
- **本文**为详尽版：集成测试、产品设计与 Agent 编排请优先参考 **`fenbi_详尽说明_docs.md`**（本文件）。
