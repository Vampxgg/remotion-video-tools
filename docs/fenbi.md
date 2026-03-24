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
| [`/api/scrape/fenbi/options`](#32-get-apiscrapefenbioptions) | **【强烈推荐】** 获取包含完整级联专业树的全局筛选项数据 |
| [`/api/scrape/fenbi/data`](#33-post-apiscrapefenbidata) | **【核心接口】** 扁平化参数，一次性/按需检索招考公告和职位列表 |

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


## 3.4 `GET /api/scrape/fenbi/options`

| 字段 | 类型 | 必填 | 默认 | 约束 | 说明 |
|------|------|------|------|------|------|
| `district_id` | int | 否 | 0 | — | 地区 id |
| `exam_type` | int | 否 | 4 | — | 考试类型 |
| `year` | int | 否 | 2025 | — | 年份 |
| `enroll_status` | int | 否 | 0 | — | 报名状态（合法值见 conditions） |
| `recruit_num_code` | int | 否 | 0 | — | 招录人数区间编码 |

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
