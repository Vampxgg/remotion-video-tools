# Vertex Gemini 文生图 / 图生图接口说明

本文档对应服务中的 **`POST /api/generate_image`**（FastAPI 路由挂载在 `main.py` 的 `/api` 前缀下，完整路径为 **`/api/generate_image`**）。底层调用 **Google Cloud Vertex AI** 的 `generateContent`（`v1beta1`），生成结果图片会上传至 **GCS** 并返回公网可访问的 `public_url`。

---

## 1. 快速索引

| 项目 | 说明 |
|------|------|
| **方法 / 路径** | `POST /api/generate_image` |
| **Content-Type** | `application/json` |
| **认证** | 由部署方在 GCP 上配置 **Application Default Credentials**（服务账号需具备 Vertex AI 与 GCS 写权限）；HTTP 层是否再加鉴权取决于网关配置，本接口代码本身未校验 API Key。 |
| **OpenAPI / Dify** | 可参考仓库内 [`openapi_gemini_image_dify.json`](./openapi_gemini_image_dify.json)。 |
| **实现源码** | `api/cre_image.py` |

---

## 2. 三种模型能力总览

`model_id` 必须为下列之一（大小写敏感）。

### 2.1 对照表

| 能力项 | `gemini-2.5-flash-image` | `gemini-3-pro-image-preview` | `gemini-3.1-flash-image-preview` |
|--------|---------------------------|------------------------------|----------------------------------|
| **定位** | 2.5 代 Flash 图像 | 3 Pro 预览（质量/思考更强） | 3.1 Flash 预览（功能最全） |
| **默认模型** | 否 | 否 | **是**（未传 `model_id` 时） |
| **aspect_ratio** | 见 §3.2「2.5 / 3 Pro 集合」 | 同左 | 在左基础上 **额外** `1:4`、`4:1`、`1:8`、`8:1` |
| **image_size** | **不支持**（传则 422） | `1K` / `2K` / `4K` | `512` / `1K` / `2K` / `4K` |
| **response_mime_type** | `image/png`、`image/jpeg` | 同左 | 同左 |
| **参考图张数上限** | 10 | 6 | 14 |
| **include_response_text（配文 TEXT）** | 支持 | 支持 | 支持 |
| **include_thoughts / thinking_level** | **不支持**（传则 422） | 支持 | 支持 |
| **prominent_people** | **不支持**（传则 422） | **不支持**（传则 422） | 支持：`BLOCK_PROMINENT_PEOPLE` |
| **personGeneration（person_generation）** | **不下发**到 Vertex（代码中为降低 2.5 的 400 风险） | 下发 `ALLOW_ADULT` 等 | 同 3 Pro |
| **单次 Vertex 读超时** | 120s | 300s | 180s |

### 2.2 宽高比枚举（按模型）

- **2.5 Flash 与 3 Pro**（集合相同）：  
  `1:1`, `3:2`, `2:3`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`
- **3.1 Flash 预览**：上述全部，再加上：  
  `1:4`, `4:1`, `1:8`, `8:1`

---

## 3. 请求体：`GenerateImagePayload`

所有字段均为 JSON 根级字段（扁平对象）。

### 3.1 字段说明（全量）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `prompt` | string | **是** | — | 文生图：画面描述。图生图：如何修改、如何融合参考图等。会与 `negative_prompt` 拼接后作为用户文本发给模型。 |
| `model_id` | string | 否 | `gemini-3.1-flash-image-preview` | 见 §2。 |
| `system_instruction` | string | 否 | null | 映射 Vertex `systemInstruction`，最长 32000。空串会被视为未传。 |
| `aspect_ratio` | string | 否 | `1:1` | 须为该 `model_id` 允许的枚举之一；空串会回退为 `1:1`。 |
| `image_size` | string | 否 | null | 仅 3 Pro / 3.1 可用，取值见 §2.1；2.5 勿传。空串视为 null。 |
| `response_mime_type` | string | 否 | null | `image/png` 或 `image/jpeg`。写入 `imageConfig.imageOutputOptions.mimeType`。若 Vertex 报未知字段，可省略本字段。 |
| `response_count` | integer | 否 | `1` | 生成张数，范围 **1–8**。底层 Vertex **每次** `candidateCount=1`；多张时服务端**并行**多次调用（**计费约按次数倍增**）。 |
| `negative_prompt` | string | 否 | null | 负向提示，服务端拼接到 prompt 后的英文固定句式中。空串视为 null。 |
| `person_generation` | string | 否 | `allow_adult` | `allow_adult` / `allow_all` / `disallow`，映射 Vertex `personGeneration`。**2.5 模型不会下发该字段**。 |
| `reference_images` | array \| string | 否 | null | 参考图列表；非空即图生图/多图条件。可为 **JSON 数组**；若为 **字符串**，服务端会 `json.loads`（便于 Dify）。见 §4。 |
| `reference_image_url` | string | 否 | null | **单张**参考图 https URL；与 `reference_images` **不能同时有内容**（否则 ValueError → 业务 422）。 |
| `include_response_text` | boolean | 否 | false | 为 true 时在 `generationConfig.responseModalities` 中加入 `TEXT`，并解析配文到 `data.text_parts`。 |
| `include_thoughts` | boolean | 否 | false | 为 true 时设置 `thinkingConfig.includeThoughts`；仅 3.x 预览模型。 |
| `thinking_level` | string | 否 | null | `HIGH` / `MINIMAL` / `LOW`，写入 `thinkingConfig.thinkingLevel`；仅 3.x 预览。 |
| `prominent_people` | string | 否 | null | 仅 **3.1**：`BLOCK_PROMINENT_PEOPLE`，写入 `imageConfig.prominentPeople`。 |
| `location_override` | string | 否 | null | 指定 Vertex **location**（如 `global`），设置后**不再**按内置列表轮询其它区域。空串视为 null。 |
| `safety_filter_level` | string | 否 | `OFF` | 映射所有安全类别的 `safetySettings.threshold`。可选：`OFF`、`BLOCK_NONE`、`BLOCK_LOW_AND_ABOVE`、`BLOCK_MEDIUM_AND_ABOVE`、`BLOCK_ONLY_HIGH`。 |

### 3.2 Dify / 低代码兼容（服务端预处理）

以下在 Pydantic 校验**之前**做宽松解析，减少工具链只传字符串导致的 422：

- `response_count`：字符串如 `"3"` 会转为整数；空串视为 `1`。
- `include_response_text` / `include_thoughts`：接受 `"true"`/`"false"`、`"1"`/`"0"`、`yes`/`no` 等。
- `reference_images`：若为字符串，按 JSON 数组解析（见 §4）。
- 若干可选字符串字段：纯空串视为 `null`。

---

## 4. 参考图：`reference_images` 与 `ReferenceImageInput`

每条参考图为一个对象，**以下三个来源必须且只能填一个**（否则校验失败）：

| 字段 | 说明 |
|------|------|
| `image_base64` | Base64 字符串；允许带 `data:image/png;base64,` 前缀。解码后单张不超过 **7MB**（`MAX_REFERENCE_IMAGE_BYTES`）。 |
| `mime_type` | 建议填写 `image/png` 或 `image/jpeg`；URL 下载时若省略会用响应头 `Content-Type`。 |
| `image_url` | **仅支持 https**。服务端会拉取图片；单张不超过 7MB；响应须为图片类型。若配置环境变量 **`CRE_IMAGE_ALLOWED_URL_HOSTS`**（逗号分隔主机名），则仅允许这些主机；**未配置则不限制主机**（仍须 https）。 |
| `gs_uri` | `gs://bucket/object`，须当前 GCP 项目可访问；配合 `mime_type`（默认 `image/jpeg`）映射为 Vertex `fileData`。 |

**顺序**：请求组装时，参考图 parts **在前**，用户 **文本 prompt** 在后（同一 `user` 消息的 `parts` 列表）。

**`reference_image_url`**：等价于 `reference_images: [{"image_url": "<该 URL>"}]`，便于 Dify 只暴露一个字符串变量。

---

## 5. 响应结构

### 5.1 统一外壳：`StandardResponse`

HTTP 状态码与 JSON 内的 `code` 在多数成功路径上一致；**部分业务错误**仍可能返回 **200** 但 `code != 200`，集成时务必读 **`code` + `message`**。

```json
{
  "code": 200,
  "message": "图片生成成功",
  "data": { },
  "timestamp": "2025-03-26T12:00:00.000000"
}
```

- `timestamp`：ISO 8601 字符串（`datetime.now().isoformat()`）。
- `data`：成功且出图时为对象（见下）；失败时可能为 `null` 或省略（`exclude_none`）。

### 5.2 成功时的 `data`：`GenerateImageResponse`

```json
{
  "images": [
    {
      "public_url": "https://storage.googleapis.com/…/gemini_images/….png",
      "local_path": null,
      "mime_type": "image/png"
    }
  ],
  "text_parts": []
}
```

| 字段 | 说明 |
|------|------|
| `images` | 本次调用返回的图片列表；`response_count`>1 时为多张。 |
| `public_url` | GCS 公共访问前缀 + 对象路径（与 `cre_image.py` 中 `GCS_PUBLIC_URL_PREFIX` 一致）。 |
| `mime_type` | 以模型返回的 `inlineData.mimeType` 为准（扩展名按 MIME 决定）。 |
| `text_parts` | 仅当 `include_response_text` 为 true 时可能非空，为模型返回的文本片段列表。 |

### 5.3 常见错误表现

| 场景 | HTTP | `code`（约） | 说明 |
|------|------|----------------|------|
| Pydantic / FastAPI 校验失败 | **422** | — | 标准 `detail` 数组。 |
| 模型能力校验（宽高比、image_size、参考图张数等） | **422** | — | `detail` 为字符串说明。 |
| 业务逻辑 ValueError（如参考图冲突、URL 非法） | **200** | **422** | `message` 为错误信息，`data` 常为空。 |
| Vertex / 网络 HTTP 错误 | **200** | **502** | `message` 含状态码与片段响应体。 |
| 未解析到任何图片 | **200** | **500** | `message` 如「API 响应中未找到图片数据」。 |
| `response_count`>1 时并行中某次失败 | **200** | **502** | `message` 指明第几次失败。 |

---

## 6. Vertex / 运行时行为摘要

- **候选数**：固定 **`candidateCount: 1`**（避免「多候选 + 图像」类 400）。多张图靠 **`response_count` 并行多次**。
- **地域**：未设置 `location_override` 时，优先尝试 **`global`**，再尝试少量随机顺序的区域（见 `GOOGLE_LOCATIONS`）；429/5xx 会换区重试。
- **生成配置节选**：`temperature=1`、`topP=0.95`、`maxOutputTokens=32768`；`responseModalities` 至少含 `IMAGE`，可选 `TEXT`。
- **图片落库**：从 Vertex 响应中取出 `inlineData`（图片）解码后上传 GCS，路径目录默认为 `gemini_images/`。

---

## 7. 分场景请求示例（可复制 JSON）

以下均可作为 `POST /api/generate_image` 的 body。按需替换 `model_id` 与参数。

### 7.1 纯文生图（三模型通用字段）

**最小请求（默认 3.1 + 1:1）：**

```json
{
  "prompt": "一只坐在窗台上的橘猫，午后阳光，插画风格"
}
```

**指定 2.5 + 16:9：**

```json
{
  "prompt": "城市夜景，赛博朋克",
  "model_id": "gemini-2.5-flash-image",
  "aspect_ratio": "16:9"
}
```

**指定 3 Pro + 2K：**

```json
{
  "prompt": "产品摄影，白色背景，极简玻璃水杯",
  "model_id": "gemini-3-pro-image-preview",
  "aspect_ratio": "1:1",
  "image_size": "2K"
}
```

**指定 3.1 + 512 + 竖版超窄（仅 3.1 支持）：**

```json
{
  "prompt": "电影海报竖版构图",
  "model_id": "gemini-3.1-flash-image-preview",
  "aspect_ratio": "1:8",
  "image_size": "512"
}
```

### 7.2 图生图（单张 URL）

**方式 A — `reference_image_url`（推荐 Dify 单字符串）：**

```json
{
  "prompt": "保持主体不变，改为水彩风格",
  "model_id": "gemini-3.1-flash-image-preview",
  "reference_image_url": "https://example.com/ref.png"
}
```

**方式 B — `reference_images` 数组：**

```json
{
  "prompt": "把背景换成海滩",
  "reference_images": [
    { "image_url": "https://example.com/portrait.jpg", "mime_type": "image/jpeg" }
  ]
}
```

**方式 C — Dify 整段字符串（服务端会 JSON 解析）：**

```json
{
  "prompt": "融合两张图的风格",
  "reference_images": "[{\"image_url\":\"https://example.com/a.png\"},{\"image_url\":\"https://example.com/b.png\"}]"
}
```

### 7.3 多参考图（注意各模型张数上限）

**3.1（最多 14 张，示例 2 张）：**

```json
{
  "prompt": "将图1的人物服装换为图2的样式",
  "model_id": "gemini-3.1-flash-image-preview",
  "reference_images": [
    { "image_url": "https://example.com/person.jpg" },
    { "image_url": "https://example.com/outfit.jpg" }
  ]
}
```

**3 Pro（最多 6 张）；2.5（最多 10 张）** — 结构相同，仅减少元素个数并改 `model_id`。

### 7.4 Base64 参考图

```json
{
  "prompt": "基于参考图生成同角色侧面像",
  "model_id": "gemini-3-pro-image-preview",
  "reference_images": [
    {
      "image_base64": "data:image/png;base64,iVBORw0KGgo...",
      "mime_type": "image/png"
    }
  ]
}
```

### 7.5 GCS 参考图（`gs_uri`）

```json
{
  "prompt": "在参考图基础上增加雨夜氛围",
  "model_id": "gemini-3.1-flash-image-preview",
  "reference_images": [
    {
      "gs_uri": "gs://your-bucket/path/ref.jpg",
      "mime_type": "image/jpeg"
    }
  ]
}
```

### 7.6 指定输出 MIME、系统指令、负向提示

```json
{
  "prompt": "科幻太空站内部",
  "model_id": "gemini-3.1-flash-image-preview",
  "response_mime_type": "image/png",
  "system_instruction": "始终生成高清、无文字水印的配图。",
  "negative_prompt": "模糊，低分辨率，畸形手指，文字水印"
}
```

### 7.7 一次请求多张图（`response_count`）

```json
{
  "prompt": "同一角色不同表情，四宫格概念",
  "model_id": "gemini-2.5-flash-image",
  "response_count": 4,
  "aspect_ratio": "1:1"
}
```

### 7.8 需要配文（`include_response_text`）

```json
{
  "prompt": "生成一张节日海报并给一句简短标题文案",
  "model_id": "gemini-3.1-flash-image-preview",
  "include_response_text": true
}
```

成功时查看 `data.text_parts`。

### 7.9 思考过程（仅 3 Pro / 3.1）

```json
{
  "prompt": "复杂场景多角色构图",
  "model_id": "gemini-3-pro-image-preview",
  "include_thoughts": true,
  "thinking_level": "HIGH"
}
```

**切勿**在 `gemini-2.5-flash-image` 上同时传 `include_thoughts` 或 `thinking_level`（会 422）。

### 7.10 知名人物限制（仅 3.1）

```json
{
  "prompt": "写实风格人像",
  "model_id": "gemini-3.1-flash-image-preview",
  "prominent_people": "BLOCK_PROMINENT_PEOPLE"
}
```

### 7.11 固定 Vertex 区域

```json
{
  "prompt": "测试 global 端点",
  "model_id": "gemini-3.1-flash-image-preview",
  "location_override": "global"
}
```

### 7.12 安全阈值示例

```json
{
  "prompt": "儿童插画，温馨",
  "model_id": "gemini-3.1-flash-image-preview",
  "safety_filter_level": "BLOCK_MEDIUM_AND_ABOVE"
}
```

---

## 8. 按模型的「禁止组合」速查（避免 422）

| 若使用模型 | 不要传（或不要设为 true） |
|------------|---------------------------|
| **gemini-2.5-flash-image** | `image_size`；`include_thoughts`；`thinking_level`；`prominent_people`；不支持的 `aspect_ratio`（如 `1:4`）；参考图 **>10** |
| **gemini-3-pro-image-preview** | `image_size` 不在 `1K/2K/4K`；`prominent_people`；`aspect_ratio` 为 `1:4` 等 3.1 专属；参考图 **>6**；`include_thoughts`/`thinking_level` 实际仅该模型支持，可传 |
| **gemini-3.1-flash-image-preview** | `image_size` 不在 `512/1K/2K/4K`；参考图 **>14**；`aspect_ratio` 不在 §2.2 扩展集合 |

---

## 9. 环境变量（部署侧）

| 变量 | 作用 |
|------|------|
| `CRE_IMAGE_ALLOWED_URL_HOSTS` | 可选。逗号分隔的**主机名**（小写比较）。设置后，`image_url` 仅允许这些主机；**不设置**则任意 **https** 图片 URL（仍校验大小与 Content-Type）。 |

---

## 10. 与 OpenAPI 文档的关系

- 机器可读契约：[`openapi_gemini_image_dify.json`](./openapi_gemini_image_dify.json)（OpenAPI 3.0.3，面向 Dify 等工具）。
- 人工详尽说明与场景示例：以 **本文档** 为准；若实现变更，请以 `api/cre_image.py` 中 `MODEL_CAPS`、`GenerateImagePayload`、`generate_image` 为准并同步更新本文档。

---

## 11. 版本与维护

- 文档编写依据：`api/cre_image.py` 当前逻辑（含 `MODEL_CAPS`、校验、`build_request_body` 等）。
- 模型名称、配额与 Vertex 字段以 Google 官方为准；若云端升级导致某字段 400，可优先尝试 **省略** `response_mime_type` 或调整 `location_override`。
