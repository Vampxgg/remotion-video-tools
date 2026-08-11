# -*- coding: utf-8 -*-
# @File：utils/settings.py
"""
全项目唯一的配置入口（基于 pydantic-settings）。

- 本文件按"功能域"拆成多个 mixin 类，再合成一个 ``Settings``，便于维护与 IDE 提示。
- 所有默认值与现有硬编码保持一致；通过 ``.env`` 或环境变量可覆盖任意字段。
- ``load_dotenv()`` 由 ``pydantic-settings`` 内部完成，整个项目只在这里加载一次。
- 五个并行的 ``cre_audio_*`` 模块各自有独立的前缀（``CRE_AUDIO_*`` /
  ``CRE_AUDIO_JSON_*`` / ``CRE_AUDIO_V2_*`` / ``CRE_AUDIO_REFACTORED_*`` /
  ``CRE_AUDIO_ORIGINAL_SPEED_*``），同名概念可独立配置不同值，互不影响。
"""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（utils/ 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = str(_PROJECT_ROOT / ".env")


class _Base(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )


# =====================================================================
# 应用层 / 基础设施
# =====================================================================

class AppSettings(_Base):
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 2906
    APP_WORKERS: int = 4
    APP_PUBLIC_BASE_URL: str = "http://127.0.0.1:2906"
    CORS_ALLOW_ORIGINS: List[str] = ["*"]
    # 当 CORS_ALLOW_ORIGINS=["*"] 时，CORS 规范要求 allow_credentials 必须为 False
    CORS_ALLOW_CREDENTIALS: bool = False
    CORS_ALLOW_METHODS: List[str] = ["*"]
    CORS_ALLOW_HEADERS: List[str] = ["*"]
    # 应用层 IP 黑名单：命中的来源 IP 一律返回 403，临时封禁只改 .env 即可增删。
    BLOCKED_IPS: List[str] = []
    LOG_DISABLE_CONSOLE: bool = True
    LOG_DIR: str = "logs"
    LOG_BACKUP_COUNT: int = 90
    # 注意：STATIC_DIR 走"项目根 + 子路径"，避免历史上 api/static 与 ./static 不一致的隐藏 bug
    STATIC_DIR: str = "static"


class DBSettings(_Base):
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "script_tools_db"
    DB_ECHO: bool = False

    @property
    def url_async(self) -> str:
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def url_sync(self) -> str:
        return (
            f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )


class RabbitSettings(_Base):
    RABBITMQ_HOST: str = "localhost"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USER: str = "guest"
    RABBITMQ_PASSWORD: str = "guest"
    RABBITMQ_VHOST: str = "/"


class RedisSettings(_Base):
    """Redis（当前仅 web_search 在用，作为搜索/抓取缓存层）。

    生产部署：docker-compose 自带 redis:7-alpine，仅绑 127.0.0.1:6379。
    应用启动会 PING 一次，失败仅 WARN 不阻断；调用方拿到 None 视作"缓存未就绪"
    自动降级为直连 provider。
    """
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
    REDIS_KEY_PREFIX: str = "script_tools"

    @property
    def redis_url(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


class CommonSettings(_Base):
    OUTBOUND_PROXY_URL: Optional[str] = None
    FETCH_USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # 所有 fish 系模块共用的密钥；若不同模块要分配不同 key 可在各自前缀里覆盖
    FISH_API_KEY: Optional[str] = None
    # GCP / GCS 共享配置
    GCP_PROJECT_ID: str = "x-pilot-469902"
    GCP_LOCATION_ID: str = "us-central1"
    # Vertex/GCS 服务账号 JSON 路径；留空则回退 google.auth.default()(用户级 ADC)。
    # 相对路径按项目根解析。配置后所有 Vertex 调用(理解/图/视频/live)统一用该凭证。
    GCP_CREDENTIALS_FILE: Optional[str] = None
    GCS_BUCKET_NAME: str = "x-pilot-storage"
    GCS_PUBLIC_URL_PREFIX: str = "https://storage.googleapis.com/x-pilot-storage"
    # Azure 模型注册表（endpoint+apiKey+model→region 路由）。相对路径按项目根解析。
    # 所有 Azure OpenAI 调用(VLM 打标/生图/脚本)统一从此文件按模型名解析密钥，
    # 消除代码/settings 里的明文 key。文件位于 secrets/（已 gitignore，不入库）。
    AZURE_MODELS_FILE: str = "secrets/azure-models.yaml"


# =====================================================================
# 五个并行的 cre_audio_* 模块（独立前缀，默认值与各自历史硬编码对齐）
# =====================================================================

class CreAudioSettings(_Base):
    """对应 api/cre_audio.py（生产线上挂载，DEST 指向 Linux）"""
    CRE_AUDIO_PROXY_URL: Optional[str] = None
    CRE_AUDIO_ENGINE_MODEL: str = "speech-1.6"
    CRE_AUDIO_AUDIO_FORMAT: str = "mp3"
    CRE_AUDIO_PUBLIC_URL_TEMPLATE: str = (
        "https://server.x-pilot.ai/static/meta-doc/video/{workflow_id}/audio/{filename}"
    )
    CRE_AUDIO_DEST_BASE_DIR: str = "/data/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
    CRE_AUDIO_MAX_WORKERS: int = 15
    CRE_AUDIO_API_SEMAPHORE: int = 50
    CRE_AUDIO_MAX_RETRIES: int = 3
    CRE_AUDIO_RETRY_DELAY: float = 2.0
    CRE_AUDIO_TEXT_SPLIT_THRESHOLD: int = 120
    CRE_AUDIO_ENABLE_DYNAMIC_SPEED_ADJUSTMENT: bool = True
    CRE_AUDIO_SPEED_ADJUST_THRESHOLD_RATIO: float = 1.05
    CRE_AUDIO_MAX_SPEECH_SPEED: float = 1.3
    CRE_AUDIO_ENABLE_DYNAMIC_DECELERATION: bool = True
    CRE_AUDIO_MIN_SPEECH_SPEED: float = 0.95
    CRE_AUDIO_START_PADDING_BUFFER_MS: int = 150


class CreAudioJsonSettings(_Base):
    """对应 api/cre_audio_json.py"""
    CRE_AUDIO_JSON_PROXY_URL: Optional[str] = None
    CRE_AUDIO_JSON_ENGINE_MODEL: str = "speech-1.6"
    CRE_AUDIO_JSON_AUDIO_FORMAT: str = "mp3"
    CRE_AUDIO_JSON_PUBLIC_URL_TEMPLATE: str = (
        "http://127.0.0.1:2906/meta-doc/video/{workflow_id}/audio/{filename}"
    )
    CRE_AUDIO_JSON_DEST_BASE_DIR: str = "E:\\Server\\x-pilot-oss\\uploads\\meta-doc\\video"
    CRE_AUDIO_JSON_MAX_WORKERS: int = 15
    CRE_AUDIO_JSON_API_SEMAPHORE: int = 50
    CRE_AUDIO_JSON_MAX_RETRIES: int = 3
    CRE_AUDIO_JSON_RETRY_DELAY: float = 2.0
    CRE_AUDIO_JSON_TEXT_SPLIT_THRESHOLD: int = 120
    CRE_AUDIO_JSON_ENABLE_DYNAMIC_SPEED_ADJUSTMENT: bool = True
    CRE_AUDIO_JSON_SPEED_ADJUST_THRESHOLD_RATIO: float = 1.05
    CRE_AUDIO_JSON_MAX_SPEECH_SPEED: float = 1.3
    CRE_AUDIO_JSON_ENABLE_DYNAMIC_DECELERATION: bool = True
    CRE_AUDIO_JSON_MIN_SPEECH_SPEED: float = 0.95
    CRE_AUDIO_JSON_START_PADDING_BUFFER_MS: int = 150


class CreAudioV2Settings(_Base):
    """对应 api/cre_audioV2.py（带本地 7890 代理）"""
    CRE_AUDIO_V2_PROXY_URL: Optional[str] = None
    CRE_AUDIO_V2_ENGINE_MODEL: str = "speech-1.6"
    CRE_AUDIO_V2_AUDIO_FORMAT: str = "mp3"
    CRE_AUDIO_V2_PUBLIC_URL_TEMPLATE: str = (
        "http://119.45.167.133:17752/meta-doc/video/{workflow_id}/audio/{filename}"
    )
    CRE_AUDIO_V2_DEST_BASE_DIR: str = "E:\\Server\\x-pilot-oss\\uploads\\meta-doc\\video"
    CRE_AUDIO_V2_MAX_WORKERS: int = 5
    CRE_AUDIO_V2_MAX_RETRIES: int = 3
    CRE_AUDIO_V2_RETRY_DELAY: float = 2.0
    CRE_AUDIO_V2_TEXT_SPLIT_THRESHOLD: int = 60
    CRE_AUDIO_V2_TARGET_CN_CHARS_PER_SECOND: float = 5.0
    CRE_AUDIO_V2_TARGET_EN_WORDS_PER_SECOND: float = 2.0
    CRE_AUDIO_V2_MIN_SPEECH_SPEED: float = 0.5
    CRE_AUDIO_V2_MAX_SPEECH_SPEED: float = 2.0


class CreAudioRefactoredSettings(_Base):
    """对应 api/cre_audio_refactored.py"""
    CRE_AUDIO_REFACTORED_PROXY_URL: Optional[str] = None
    CRE_AUDIO_REFACTORED_ENGINE_MODEL: str = "speech-1.6"
    CRE_AUDIO_REFACTORED_AUDIO_FORMAT: str = "mp3"
    CRE_AUDIO_REFACTORED_API_BASE_URL: str = "https://server.x-pilot.ai"
    CRE_AUDIO_REFACTORED_PUBLIC_URL_TEMPLATE: str = (
        "https://server.x-pilot.ai/static/meta-doc/video/{workflow_id}/audio/{filename}"
    )
    CRE_AUDIO_REFACTORED_DEST_BASE_DIR: str = "/data/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
    CRE_AUDIO_REFACTORED_MAX_WORKERS: int = 2
    CRE_AUDIO_REFACTORED_API_SEMAPHORE: int = 50
    CRE_AUDIO_REFACTORED_MAX_RETRIES: int = 3
    CRE_AUDIO_REFACTORED_RETRY_DELAY: float = 2.0
    CRE_AUDIO_REFACTORED_TEXT_SPLIT_THRESHOLD: int = 120
    CRE_AUDIO_REFACTORED_ENABLE_DYNAMIC_SPEED_ADJUSTMENT: bool = True
    CRE_AUDIO_REFACTORED_SPEED_ADJUST_THRESHOLD_RATIO: float = 1.05
    CRE_AUDIO_REFACTORED_MAX_SPEECH_SPEED: float = 1.3
    CRE_AUDIO_REFACTORED_ENABLE_DYNAMIC_DECELERATION: bool = True
    CRE_AUDIO_REFACTORED_MIN_SPEECH_SPEED: float = 0.95
    CRE_AUDIO_REFACTORED_START_PADDING_BUFFER_MS: int = 150


class CreAudioOriginalSpeedSettings(_Base):
    """对应 api/cre_audio_original_speed.py（当前全文注释，但按"不要偷懒"原则
    完整展开镜像 cre_audio.py 的字段集，便于将来取消注释零代码改动激活）。"""
    CRE_AUDIO_ORIGINAL_SPEED_PROXY_URL: Optional[str] = None
    CRE_AUDIO_ORIGINAL_SPEED_ENGINE_MODEL: str = "speech-1.6"
    CRE_AUDIO_ORIGINAL_SPEED_AUDIO_FORMAT: str = "mp3"
    CRE_AUDIO_ORIGINAL_SPEED_PUBLIC_URL_TEMPLATE: str = (
        "http://119.45.167.133:17752/meta-doc/video/{workflow_id}/audio/{filename}"
    )
    CRE_AUDIO_ORIGINAL_SPEED_DEST_BASE_DIR: str = "E:\\Server\\x-pilot-oss\\uploads\\meta-doc\\video"
    CRE_AUDIO_ORIGINAL_SPEED_MAX_WORKERS: int = 15
    CRE_AUDIO_ORIGINAL_SPEED_API_SEMAPHORE: int = 15
    CRE_AUDIO_ORIGINAL_SPEED_SESSION_POOL_SIZE: int = 15
    CRE_AUDIO_ORIGINAL_SPEED_MAX_RETRIES: int = 3
    CRE_AUDIO_ORIGINAL_SPEED_RETRY_DELAY: float = 2.0
    CRE_AUDIO_ORIGINAL_SPEED_TEXT_SPLIT_THRESHOLD: int = 120
    CRE_AUDIO_ORIGINAL_SPEED_ENABLE_DYNAMIC_SPEED_ADJUSTMENT: bool = True
    CRE_AUDIO_ORIGINAL_SPEED_SPEED_ADJUST_THRESHOLD_RATIO: float = 1.05
    CRE_AUDIO_ORIGINAL_SPEED_MAX_SPEECH_SPEED: float = 1.3
    CRE_AUDIO_ORIGINAL_SPEED_ENABLE_DYNAMIC_DECELERATION: bool = True
    CRE_AUDIO_ORIGINAL_SPEED_MIN_SPEECH_SPEED: float = 0.95
    CRE_AUDIO_ORIGINAL_SPEED_START_PADDING_BUFFER_MS: int = 150


# =====================================================================
# 其他独立 router 配置
# =====================================================================

class TtsSettings(_Base):
    """对应 api/tts.py"""
    TTS_PROXY_URL: Optional[str] = None
    TTS_ENGINE_MODEL: str = "speech-1.6"
    TTS_AUDIO_FORMAT: str = "mp3"
    TTS_PUBLIC_URL_TEMPLATE: str = (
        "http://119.45.167.133:17752/meta-doc/video/{workflow_id}/audio/{filename}"
    )
    TTS_DEST_BASE_DIR: str = "E:\\Server\\x-pilot-oss\\uploads\\meta-doc\\video"
    TTS_MAX_WORKERS: int = 5
    TTS_MAX_RETRIES: int = 3
    TTS_RETRY_DELAY: float = 2.0
    TTS_TEXT_SPLIT_THRESHOLD: int = 60
    TTS_ENABLE_DYNAMIC_SPEED_ADJUSTMENT: bool = True
    TTS_SPEED_ADJUST_THRESHOLD_RATIO: float = 1.05
    TTS_MAX_SPEECH_SPEED: float = 1.4
    TTS_MIN_SPEECH_SPEED: float = 0.7
    TTS_START_PADDING_BUFFER_MS: int = 150
    TTS_CHARS_PER_SEC_ESTIMATE: float = 6.0
    TTS_CHARS_PER_SEC_ALPHA: float = 0.2
    TTS_LIBROSA_FALLBACK: bool = True


class MurfTtsSettings(_Base):
    """对应 api/murf_tts.py"""
    MURF_TTS_PROXY_URL: Optional[str] = None
    MURF_TTS_AUDIO_FORMAT: str = "mp3"
    MURF_TTS_PUBLIC_URL_TEMPLATE: str = (
        "http://119.45.167.133:7752/meta-doc/video/{workflow_id}/audio/{filename}"
    )
    MURF_TTS_DEST_BASE_DIR: str = "/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
    MURF_TTS_MAX_WORKERS: int = 5
    MURF_TTS_MAX_RETRIES: int = 3
    MURF_TTS_RETRY_DELAY: float = 2.0


class GoogleTtsSettings(_Base):
    """对应 api/google_tts.py"""
    GOOGLE_TTS_PROXY_URL: Optional[str] = None
    GOOGLE_TTS_AUDIO_FORMAT: str = "mp3"
    GOOGLE_TTS_API_BASE_URL: str = "https://server.x-pilot.ai"
    GOOGLE_TTS_PUBLIC_URL_TEMPLATE: str = (
        "https://server.x-pilot.ai/static/meta-doc/video/{workflow_id}/audio/{filename}"
    )
    GOOGLE_TTS_FINAL_DEST_DIR: str = "/data/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
    GOOGLE_TTS_MAX_WORKERS: int = 4
    GOOGLE_TTS_API_SEMAPHORE: int = 50
    GOOGLE_TTS_MAX_RETRIES: int = 3
    GOOGLE_TTS_RETRY_DELAY: float = 2.0
    GOOGLE_TTS_TEXT_SPLIT_THRESHOLD: int = 4500
    GOOGLE_TTS_ENABLE_DYNAMIC_SPEED_ADJUSTMENT: bool = True
    GOOGLE_TTS_SPEED_ADJUST_THRESHOLD_RATIO: float = 1.05
    GOOGLE_TTS_MAX_SPEECH_SPEED: float = 1.3
    GOOGLE_TTS_ENABLE_DYNAMIC_DECELERATION: bool = True
    GOOGLE_TTS_MIN_SPEECH_SPEED: float = 0.95
    GOOGLE_TTS_START_PADDING_BUFFER_MS: int = 150
    GOOGLE_TTS_MODEL_ID_TEMPLATE: str = "{language}-Chirp3-HD-{character}"
    GOOGLE_TTS_REQUIRED_MODEL_SUBSTRING: str = "Chirp3-HD"


class FishAsrSettings(_Base):
    """对应 api/fish_asr.py"""
    FISH_ASR_API_URL: str = "https://api.fish.audio/v1/asr"
    FISH_ASR_PROXY_URL: Optional[str] = None
    FISH_ASR_THREAD_WORKERS: int = 10
    FISH_ASR_API_SEMAPHORE: int = 20
    FISH_ASR_MAX_RETRIES: int = 3
    FISH_ASR_RETRY_DELAY: float = 1.5
    FISH_ASR_HTTP_TIMEOUT: float = 10.0
    FISH_ASR_HTTP_CONNECT_TIMEOUT: float = 5.0
    FISH_ASR_HTTP_READ_TIMEOUT: float = 60.0
    FISH_ASR_HTTP_WRITE_TIMEOUT: float = 10.0


class CreImageSettings(_Base):
    """对应 api/cre_image.py"""
    CRE_IMAGE_OUTPUT_DIR: str = "gemini_images"
    CRE_IMAGE_MAX_REFERENCE_BYTES: int = 7 * 1024 * 1024
    CRE_IMAGE_HTTPX_READ_TIMEOUT: float = 360.0
    CRE_IMAGE_HTTPX_CONNECT_TIMEOUT: float = 15.0
    CRE_IMAGE_HTTPX_WRITE_TIMEOUT: float = 60.0
    CRE_IMAGE_HTTPX_POOL_TIMEOUT: float = 30.0
    # 留空表示不做白名单限制；逗号分隔多个主机
    CRE_IMAGE_ALLOWED_URL_HOSTS: str = ""


class CreImageAzureSettings(_Base):
    """对应 api/cre_image_azure.py（Azure OpenAI gpt-image-2 文/图生图）。

    与 cre_image.py（Vertex Gemini）物理隔离：独立前缀、独立 GCS 子目录，
    仅共享 GCS 存储桶/公网前缀等基础设施配置（CommonSettings）。
    鉴权走 Azure OpenAI 的 api-key 头；生成走 REST（httpx 直连），不引入 openai SDK。
    """
    # 模型名：运行时由 utils.azure_models 从 secrets/azure-models.yaml 解析出多 region
    # 端点池（endpoint+apiKey+api_version），不再硬编码密钥/明文 ENDPOINTS_JSON。
    CRE_IMAGE_AZURE_MODEL: str = "gpt-image-2"
    # 可选显式覆盖单 endpoint（留空=走 yaml）。切勿把明文 key 写进版本库。
    CRE_IMAGE_AZURE_ENDPOINT: Optional[str] = None
    # 资源密钥；留空则走 yaml 解析（yaml 也无则接口拒绝，不下发外部请求）
    CRE_IMAGE_AZURE_API_KEY: Optional[str] = None
    # 部署名（Azure 以部署名而非模型名寻址）；留空=用 CRE_IMAGE_AZURE_MODEL
    CRE_IMAGE_AZURE_DEPLOYMENT: Optional[str] = None
    # REST 接口版本；留空=用 yaml 顶层 apiVersion
    CRE_IMAGE_AZURE_API_VERSION: Optional[str] = None
    # 多区域 endpoint 池，JSON 数组；配置后优先使用，未配置则从 yaml 的
    # models[CRE_IMAGE_AZURE_MODEL].regions 自动生成端点池。
    # 单项字段：name/endpoint/api_key/deployment/weight/max_concurrency。
    CRE_IMAGE_AZURE_ENDPOINTS_JSON: str = ""
    # 生成默认档位（medium 兼顾速度与质量；high 最慢，low 最快）
    CRE_IMAGE_AZURE_DEFAULT_QUALITY: str = "medium"      # low / medium / high
    CRE_IMAGE_AZURE_DEFAULT_OUTPUT_FORMAT: str = "png"   # png / jpeg
    # 生成结果的 GCS 子目录（与 gemini_images 分开，便于区分来源）
    CRE_IMAGE_AZURE_OUTPUT_DIR: str = "azure_images"
    # edits 接口单张输入图上限（官方 50MB）
    CRE_IMAGE_AZURE_MAX_REFERENCE_BYTES: int = 50 * 1024 * 1024
    # httpx 超时
    CRE_IMAGE_AZURE_HTTPX_READ_TIMEOUT: float = 180.0
    CRE_IMAGE_AZURE_HTTPX_CONNECT_TIMEOUT: float = 15.0
    CRE_IMAGE_AZURE_HTTPX_WRITE_TIMEOUT: float = 60.0
    CRE_IMAGE_AZURE_HTTPX_POOL_TIMEOUT: float = 30.0
    # 参考图 URL 主机白名单；留空表示不限制，逗号分隔多个主机
    CRE_IMAGE_AZURE_ALLOWED_URL_HOSTS: str = ""
    # 429 限流重试次数（指数退避）
    CRE_IMAGE_AZURE_MAX_RETRIES: int = 2
    # 每个 Azure endpoint 的默认并发上限；gpt-image-2 当前每区域 10 RPM，实测 5 并发更稳。
    CRE_IMAGE_AZURE_ENDPOINT_MAX_CONCURRENCY: int = 5
    # 单 endpoint 连续瞬时失败达到阈值后短暂熔断，避免高并发时持续命中坏节点。
    CRE_IMAGE_AZURE_CIRCUIT_FAILURE_THRESHOLD: int = 3
    CRE_IMAGE_AZURE_CIRCUIT_COOLDOWN_SECONDS: float = 60.0
    # 出网代理拆分（方案 A）：本模块的 httpx client 一律 trust_env=False，
    # 不继承进程的 HTTP(S)_PROXY，改由以下两项显式控制，避免代理误掐断长请求。
    # Azure API 出网代理；留空=直连（Azure 通常可直连，直连最稳）。
    CRE_IMAGE_AZURE_PROXY_URL: Optional[str] = None
    # GCS 上传 / gs_uri 下载出网代理；留空=直连（若所在网络需翻墙访问 Google 再填）。
    CRE_IMAGE_AZURE_GCS_PROXY_URL: Optional[str] = None


class GeminiLiveSettings(_Base):
    """对应 api/gemini_live.py + services/gemini_live_client.py + services/sop_assessor.py。

    本模块当前承载"SOP 实训实时评估"场景：用户上传/粘贴 SOP，AI 通过摄像头+麦克风
    实时对照 SOP 评估学员行为，仅通过 function calling 上报评估事件。
    """

    GEMINI_LIVE_MODEL: str = "gemini-live-2.5-flash-native-audio"
    GEMINI_LIVE_LANGUAGE_CODE: str = "zh-CN"
    # 默认仅文本回执——评估事件靠 function calling，不需要模型说话
    GEMINI_LIVE_RESPONSE_MODALITIES: List[str] = ["text"]
    GEMINI_LIVE_ENABLE_TRANSCRIPTION: bool = True
    # SOP 评估场景：默认关闭共情对话与主动音频，避免模型自由发声
    GEMINI_LIVE_ENABLE_AFFECTIVE_DIALOG: bool = False
    GEMINI_LIVE_PROACTIVE_AUDIO: bool = False
    GEMINI_LIVE_CONTEXT_TRIGGER_TOKENS: int = 10000
    GEMINI_LIVE_CONTEXT_TARGET_TOKENS: int = 2048
    GEMINI_LIVE_MAX_AUDIO_BYTES_PER_MESSAGE: int = 64 * 1024
    GEMINI_LIVE_MAX_VIDEO_BYTES_PER_MESSAGE: int = 512 * 1024
    GEMINI_LIVE_SESSION_TIMEOUT_SEC: int = 1800
    GEMINI_LIVE_FRONTEND_DIR: str = "frontend"
    # Live API 视频输入分辨率：实训场景建议 medium，平衡识别精度与 token 成本
    GEMINI_LIVE_MEDIA_RESOLUTION: str = "medium"

    # ===== SOP 实训评估配置 =====
    # 是否默认启用"语音教练"模式：critical/high 级错误时让模型短促语音提醒
    GEMINI_LIVE_SOP_VOICE_COACH_DEFAULT: bool = False
    # 评估事件 NDJSON 落盘目录（相对项目根）
    GEMINI_LIVE_SOP_LOG_SUBDIR: str = "logs/live/sessions"
    # 服务端周期性把 [STATUS] 摘要回灌给模型的间隔（毫秒，0 表示关闭）
    GEMINI_LIVE_SOP_STATUS_HEARTBEAT_MS: int = 20000
    # 单条 SOP 文本最大字节数（防止误传巨大 markdown）
    GEMINI_LIVE_SOP_MAX_PAYLOAD_BYTES: int = 256 * 1024


class CreVideoSettings(_Base):
    """对应 api/cre_video.py"""
    CRE_VIDEO_GCS_OUTPUT_URI: str = "gs://x-pilot-storage/veo_video/"
    CRE_VIDEO_POLLING_INTERVAL_SEC: int = 10
    CRE_VIDEO_POLLING_TIMEOUT_SEC: int = 180
    CRE_VIDEO_HTTPX_TIMEOUT: float = 15.0
    CRE_VIDEO_HTTPX_CONNECT_TIMEOUT: float = 5.0


class FenbiSettings(_Base):
    """对应 api/fenbi_gateway.py（粉笔登录态 Cookie 必须由 .env 提供，无默认）"""
    FENBI_COOKIE: Optional[str] = None
    FENBI_HERA_ORIGIN: str = "https://hera-webapp.fenbi.com"
    FENBI_MARKET_PC: str = "https://market-api.fenbi.com/toolkit/api/v1/pc"
    FENBI_META_CACHE_TTL_SEC: int = 600


class VideoCompressSettings(_Base):
    """对应 api/video_compress.py"""
    VIDEO_COMPRESS_UPLOAD_SUBDIR: str = "compress_uploads"
    VIDEO_COMPRESS_OUTPUT_SUBDIR: str = "compress_outputs"
    VIDEO_COMPRESS_TASK_STATE_SUBDIR: str = "compress_tasks"
    VIDEO_COMPRESS_MAX_UPLOAD_MB: int = 500
    VIDEO_COMPRESS_MAX_CONCURRENT_FFMPEG: int = 6
    VIDEO_COMPRESS_FFMPEG_THREADS: int = 2
    VIDEO_COMPRESS_TIMEOUT_SEC: int = 3600
    VIDEO_COMPRESS_CLEANUP_DELAY_SEC: int = 600


class ConverterSettings(_Base):
    """对应 api/converter.py"""
    CONVERTER_GENERATED_FILES_SUBDIR: str = "generated_files"
    CONVERTER_TEMP_IMAGES_SUBDIR: str = "temp_images"
    CONVERTER_DOC_TEMP_SUBDIR: str = "doc_convert_temp"
    CONVERTER_DOC_MAX_UPLOAD_MB: int = 100
    CONVERTER_DOC_CONVERT_TIMEOUT_SEC: int = 180
    CONVERTER_SOFFICE_PATH: Optional[str] = None
    CONVERTER_CLEANUP_DELAY_SEC: int = 120


class JobSearchSettings(_Base):
    """对应 api/job_search.py（智联 v1）。账号密码必须由 .env 提供，无默认。"""
    ZHILIAN_USERNAME: Optional[str] = None
    ZHILIAN_PASSWORD: Optional[str] = None
    JOB_SEARCH_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    JOB_SEARCH_MAX_CONCURRENT: int = 3
    JOB_SEARCH_REQUEST_CONCURRENCY: int = 1
    JOB_SEARCH_EXECUTOR_WORKERS: int = 1
    JOB_SEARCH_MAX_KEYWORDS: int = 10
    JOB_SEARCH_MAX_PROVINCES: int = 10
    JOB_SEARCH_MAX_COMBINATIONS: int = 20
    JOB_SEARCH_MAX_PAGE_SIZE: int = 5
    JOB_SEARCH_SCRAPE_TIMEOUT_SEC: float = 300.0
    JOB_SEARCH_DETAIL_HTTP_CONCURRENCY: int = 8
    JOB_SEARCH_DETAIL_HTTP_TIMEOUT: float = 10.0
    JOB_SEARCH_ADMIN_EMAIL: str = "1561958968@qq.com"
    JOB_SEARCH_LOGIN_NOTIFY_COOLDOWN_SEC: int = 600
    JOB_SEARCH_SMTP_HOST: Optional[str] = None
    JOB_SEARCH_SMTP_PORT: int = 465
    JOB_SEARCH_SMTP_USERNAME: Optional[str] = None
    JOB_SEARCH_SMTP_PASSWORD: Optional[str] = None
    JOB_SEARCH_SMTP_FROM: Optional[str] = None
    JOB_SEARCH_SMTP_USE_SSL: bool = True
    JOB_SEARCH_SMTP_STARTTLS: bool = False
    ZHAOPIN_DETAIL_API_TEMPLATE: str = (
        "https://fe-api.zhaopin.com/c/i/jobs/position-detail-new?number={number}"
    )
    ZHAOPIN_LIST_URL: str = "https://www.zhaopin.com/sou/jl779/kwB4JMAS33DO/p1"


class JobSearchV2Settings(_Base):
    """对应 api/job_search_v2.py + services/zhaopin_client.py（智联 v2 浏览器 JS 版）。"""
    JOB_SEARCH_V2_API_KEY: Optional[str] = None
    JOB_SEARCH_V2_DIRECT_ENABLED: bool = True
    JOB_SEARCH_V2_BROWSER_FALLBACK_ENABLED: bool = True
    JOB_SEARCH_V2_LIST_PAGE_SIZE: int = 20
    JOB_SEARCH_V2_HTTP_CONCURRENCY: int = 8
    JOB_SEARCH_V2_HTTP_TIMEOUT: float = 10.0
    JOB_SEARCH_V2_TASK_TTL_SECONDS: int = 1800
    JOB_SEARCH_V2_MAX_COMBINATIONS: int = 50
    JOB_SEARCH_V2_MAX_PAGE_SIZE: int = 10
    JOB_SEARCH_V2_SYNC_TIMEOUT: float = 60.0
    ZHAOPIN_CITY_API_TEMPLATE: str = (
        "https://fe-api.zhaopin.com/c/i/city-page/user-city?ipCity={name}"
    )


class TuoyuSerpSettings(_Base):
    """对应 api/tuoyu_serp_search.py"""
    TUOYU_SERP_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    TUOYU_SERP_GOOGLE_HOST: str = "www.google.com"
    TUOYU_SERP_INCLUDE_WECHAT: bool = True


class UrlFetchSettings(_Base):
    """对应 api/url_content_fetch.py"""
    URL_FETCH_MAX_DOCUMENT_BYTES: int = 20 * 1024 * 1024


class DocParserSettings(_Base):
    """对应 services/document_parser_service.py
    生产部署必须显式提供 DOC_PARSER_IMAGE_UPLOAD_URL，避免 192.168.x 内网泄漏。"""
    DOC_PARSER_IMAGE_UPLOAD_URL: Optional[str] = None
    DOC_PARSER_IMAGE_UPLOAD_FIELD: str = "file"
    DOC_PARSER_IMAGE_UPLOAD_TOKEN: Optional[str] = None
    DOC_PARSER_IMAGE_UPLOAD_LOGIN_URL: Optional[str] = None
    DOC_PARSER_IMAGE_UPLOAD_LOGIN: Optional[str] = None
    DOC_PARSER_IMAGE_UPLOAD_PASSWORD: Optional[str] = None
    DOC_PARSER_PDF_MAX_PAGES: int = 50
    DOC_PARSER_MAX_TABLE_ROWS: int = 500
    DOC_PARSER_MIN_IMG_BYTES: int = 5 * 1024
    DOC_PARSER_MIN_IMG_DIM: int = 50
    # 内嵌图片上传并发数（线程池）。大图量文档（如教材 480+ 图）串行上传耗时数十秒，
    # 并发可压到数秒。共享同一 httpx.Client 连接池；设 1 退化为串行。
    DOC_PARSER_IMAGE_UPLOAD_CONCURRENCY: int = 10


class FileParseSettings(_Base):
    """对应 api/file_parser.py（正式文件上传解析服务）。"""
    FILE_PARSE_API_KEY: Optional[str] = None
    # HTTP 上传层限制：单文件和批量总量分开控制，批量总量不是单文件上限乘以文件数。
    # 面向真实教材/项目手册（常见 40-70MB，480+ 图），单文件上限取 80MB。
    FILE_PARSE_MAX_UPLOAD_MB: int = 80
    FILE_PARSE_MAX_BATCH_FILES: int = 10
    FILE_PARSE_MAX_TOTAL_MB: int = 160
    FILE_PARSE_ENABLE_OCR_DEFAULT: bool = False
    FILE_PARSE_ENABLE_IMAGE_UPLOAD_DEFAULT: bool = True
    # API 默认返回长度；用户不传 max_chars 时使用它。
    FILE_PARSE_DEFAULT_MAX_CHARS: int = 60000
    # 文件解析内容硬上限；核心解析清洗、JSON/XML/TXT 截断、API max_chars 上限统一使用它。
    FILE_PARSE_MAX_CONTENT_CHARS: int = 200000
    FILE_PARSE_ALLOWED_EXTENSIONS: List[str] = [
        ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv",
        ".html", ".htm", ".json", ".xml", ".txt", ".md", ".markdown",
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ]


class FileUnderstandSettings(_Base):
    """对应 api/file_understand.py（多模态知识理解微服务）。

    在 /file/parse 抽取(图->公网URL、表格、文本)的基础上，再用 Vertex Gemini
    对原始文档做"视觉级"理解：看懂图表/图片、把数据型图表转写为 markdown 数据表、
    用视觉忠实转写源表格，并保留源图 URL。凭证复用 cre_image 的 ADC(google.auth.default)。
    """
    # 默认走 flash（快/便宜，适合 iteration 逐文件）；可用环境变量切到 pro。
    FILE_UNDERSTAND_MODEL: str = "gemini-2.5-flash"
    # Vertex 区域；留空则沿用与 cre_image 一致的 global 优先轮询。
    FILE_UNDERSTAND_LOCATION: str = "global"
    # Gemini 3 才支持 media_resolution（low/medium/high）；非 3 系模型会被忽略。
    FILE_UNDERSTAND_MEDIA_RESOLUTION: Optional[str] = None
    # 单次 Gemini 调用(单区域)超时(秒)。元素级输入短，无需给太久；配合 MAX_REGIONS 限制总等待。
    FILE_UNDERSTAND_TIMEOUT_SEC: float = 180.0
    # 单次理解最多尝试的 Vertex 区域数；防止区域轮询把偶发慢/错放大成超长等待(最坏=区域数×单区域超时)。
    # Vertex 现已降为"最后兜底"（主力为 Azure gpt-4o 多区域端点池，见 FILE_UNDERSTAND_VLM_PRIMARY_MODEL）；
    # 此项仅约束兜底链路里 Vertex 自身的区域轮询，保持 1 以避免换区放大单图耗时。
    FILE_UNDERSTAND_MAX_REGIONS: int = 1
    # 鉴权：留空则复用 FILE_PARSE_API_KEY；都留空表示不鉴权。
    FILE_UNDERSTAND_API_KEY: Optional[str] = None
    # 输出 markdown 硬上限，沿用文件解析的口径。
    FILE_UNDERSTAND_MAX_CONTENT_CHARS: int = 200000
    # 生成温度，理解/转写场景取低值更稳。
    FILE_UNDERSTAND_TEMPERATURE: float = 0.2
    # 思考预算：0=关闭扩展思考。转写/校对任务无需思考，关掉可显著提速(仅 gemini-2.5/3 生效)。
    FILE_UNDERSTAND_THINKING_BUDGET: int = 0
    # 异步任务（/file/understand/async + /result/{job_id}）的文件存储目录；
    # 留空则用系统临时目录下 file_understand_jobs。改为提交+轮询可绕开前置网关 ~120s 读超时。
    FILE_UNDERSTAND_JOB_DIR: Optional[str] = None
    # 异步任务记录的保留时长（秒），超时自动清理。
    FILE_UNDERSTAND_JOB_TTL_SEC: int = 86400
    # 跨 worker 的 Vertex 视觉理解全局并发上限（基础解析不受限）。
    # 通过 Redis 租约实现；多 worker 下不会被进程数放大。0/None 表示不限。
    FILE_UNDERSTAND_GLOBAL_CONCURRENCY: int = 3
    FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED: bool = True
    # 租约 TTL 需覆盖单次理解最坏耗时（区域轮询 + 网络抖动）；worker 崩溃后会自动释放。
    FILE_UNDERSTAND_GLOBAL_LEASE_TTL_SEC: int = 600
    FILE_UNDERSTAND_GLOBAL_WAIT_INTERVAL_SEC: float = 0.5
    # Redis 不可用策略：fallback_base=降级基础解析；open=本地开发可放开；fail=抛错。
    FILE_UNDERSTAND_LIMITER_UNAVAILABLE_POLICY: str = "fallback_base"

    # ===== 出网代理、重试退避、视觉总预算 =====
    # Vertex 出网代理：国内服务器直连 googleapis.com 会超时，需走代理。
    # 同时作用于 generateContent 与 oauth2 token 刷新两条链路（都是 Google 域）。
    # 留空则回落 OUTBOUND_PROXY_URL；两者都空=直连（海外机/本地可直连时用）。
    FILE_UNDERSTAND_VERTEX_PROXY_URL: Optional[str] = None
    # Vertex 换区/网络抖动前的退避：指数基数与上限（秒），配合满抖动缓解瞬时限流。
    FILE_UNDERSTAND_RETRY_BACKOFF_BASE_SEC: float = 1.0
    FILE_UNDERSTAND_RETRY_BACKOFF_CAP_SEC: float = 8.0
    # 整个视觉阶段（含排队 + 逐元素视觉）的墙钟总预算（秒）。超出立即兜底。
    # 用于同步/批量场景；异步 iteration 场景由 FILE_UNDERSTAND_ASYNC_BUDGET_SEC 再收敛。
    FILE_UNDERSTAND_VISION_DEADLINE_SEC: float = 480.0
    # 异步理解（/file/understand/async）的默认墙钟总预算（秒），含排队+基础解析+视觉。
    # 调用方(如 Dify 代码节点)沙箱执行上限为 300s，这里留足下载/轮询间隔的安全余量取 250。
    # 到点即把 job 置 succeeded 并返回已完成部分（至少基础解析：全文+全部真实图URL）。
    # 可被请求参数 max_wait 覆盖。
    FILE_UNDERSTAND_ASYNC_BUDGET_SEC: float = 250.0
    # 全局限流：最大排队等待（秒，0=不限）与等待抖动比例（0-1）。
    FILE_UNDERSTAND_GLOBAL_MAX_WAIT_SEC: float = 120.0
    FILE_UNDERSTAND_GLOBAL_WAIT_JITTER: float = 0.3

    # ===== 元素级视觉理解（AST + 锚点定位；替代整份 PDF 内联）=====
    # 单文档内逐元素视觉的并发上限（受全局限流器再收敛）。元素输入小、瓶颈在等网络。
    # 与文档级并发相乘为总在途请求数：需 ≤ 视觉模型多区域池可承载量
    # （gpt-4o 覆盖 8 区域）。默认 12：GLOBAL(3)×12=36，均摊每区域约 4-5 路，安全区间内，
    # 显著缩短大图量文档（教材 300-500 图）总耗时并压进异步预算。
    FILE_UNDERSTAND_ELEMENT_CONCURRENCY: int = 12
    # 单元素（单图/单表）视觉调用的墙钟超时（秒）。元素输入很小，无需给太久；
    # 收敛为 40 让个别卡顿的图更快失败、让位其余图，配合预算兜底。
    FILE_UNDERSTAND_ELEMENT_TIMEOUT_SEC: float = 40.0
    # 一份文档最多做视觉的图片元素数（去重/过滤后仍超则其余标 unresolved，保留原图 URL）。
    # 教材类常 300-500 图，去重+低价值过滤后有效图通常远少于此；配合异步预算与单元素超时兜底。
    FILE_UNDERSTAND_ELEMENT_MAX_IMAGES: int = 300
    # 图片近重复去重（dHash）开关与汉明距离阈值（<=阈值判为重复，复用同一 caption）。
    FILE_UNDERSTAND_IMAGE_DEDUP_ENABLED: bool = True
    FILE_UNDERSTAND_IMAGE_DEDUP_HAMMING: int = 5
    # 重要性过滤：跳过疑似 Logo/装饰图（按最小边像素与最小字节）。
    FILE_UNDERSTAND_IMAGE_FILTER_ENABLED: bool = True
    FILE_UNDERSTAND_IMAGE_FILTER_MIN_DIM: int = 64
    FILE_UNDERSTAND_IMAGE_FILTER_MIN_BYTES: int = 3072
    # 语义级低价值过滤：VLM 描述为泛化无信息量（如"一张展示…示意图"）时不写入正文，
    # 仅保留原图 URL 占位，避免噪声污染。chart 与含具体信息(数字/型号/界面)的描述不受影响。
    FILE_UNDERSTAND_IMAGE_LOWVALUE_FILTER_ENABLED: bool = True
    # Azure 备用单图打标模型（azure_vlm_client 使用；单图，不再拆页）。
    FILE_UNDERSTAND_AZURE_MODEL: str = "FW-Kimi-K2.7-Code"
    # ===== 视觉打标主力模型 + 多区域端点池负载均衡 =====
    # 视觉打标主力模型键名（须在 azure-models.yaml 中定义且支持多模态 image_url）。
    # gpt-4o 覆盖 8 区域，配额池大、质量一致；应急可回退 FW-Kimi-K2.7-Code（仅单区域）。
    FILE_UNDERSTAND_VLM_PRIMARY_MODEL: str = "gpt-4o"
    # 区域级轮询开关：True=跨请求用全局游标把流量分散到该模型的全部 region 端点，
    # 单端点 429/5xx/连接错时自动切下一区域，直接并用多个配额池。排障时可置 False 锁首选区。
    FILE_UNDERSTAND_VLM_REGION_ROTATION: bool = True
    # 表格元素级视觉校对开关、裁剪 DPI、单文档最多校对表数、低置信度空单元格占比阈值。
    FILE_UNDERSTAND_TABLE_VISION_ENABLED: bool = True
    FILE_UNDERSTAND_TABLE_CROP_DPI: int = 150
    FILE_UNDERSTAND_TABLE_VISION_MAX: int = 40
    FILE_UNDERSTAND_TABLE_LOWCONF_EMPTY_RATIO: float = 0.2
    # 单页 PyMuPDF find_tables 的硬超时（秒）：复杂矢量页会退化到近乎无限 CPU，
    # 超时即跳过该页（保留解析层表格），防止冻结进程。<=0 表示不限制。
    FILE_UNDERSTAND_TABLE_FIND_TIMEOUT_SEC: float = 4.0


class DocImportSettings(_Base):
    """对应 api/document_import.py + services/document_manifest_service.py。

    文档结构化解析（manifest，仿 Genspark import_pdf）：把 pdf/office/图片拆成
    "每页高清 PNG + 该页文本层 + 该页内嵌图（带 bbox / VLM 描述）"，每个资产上传为
    公网 URL，供下游多模态 PPT workflow 使用。上传复用 DOC_PARSER_IMAGE_UPLOAD_*
    对象存储配置；VLM 打标复用 Vertex Gemini（与 file_understand 同凭证）。
    """

    # 鉴权：留空则复用 FILE_PARSE_API_KEY；都留空表示不鉴权（与其它 router 一致）。
    DOC_IMPORT_API_KEY: Optional[str] = None
    # 每页 PNG 渲染 DPI（越大越清晰但越慢/越大）；150 兼顾清晰与体积。
    DOC_IMPORT_DEFAULT_DPI: int = 150
    # 最大处理页数（防止超大文档拖垮）。
    DOC_IMPORT_MAX_PAGES: int = 100
    # 内嵌图过滤阈值（与 DOC_PARSER 对齐，避免抽到分隔线/图标噪音）。
    DOC_IMPORT_MIN_IMG_BYTES: int = 5 * 1024
    DOC_IMPORT_MIN_IMG_DIM: int = 50
    # 上传单文件大小上限（MB），复用 file_parse 的口径即可，这里单独留一档更保守。
    DOC_IMPORT_MAX_UPLOAD_MB: int = 80
    # 通过 file_url 拉取源文件的超时与大小上限。
    DOC_IMPORT_FETCH_TIMEOUT_SEC: float = 60.0
    DOC_IMPORT_FETCH_MAX_MB: int = 80

    # ===== VLM 内嵌图打标 =====
    # 打标 provider 现已切换为 Azure（FW-Kimi-K2.7-Code，国内可达）。
    # 以下 Vertex 遗留项不再使用，仅保留避免历史环境变量报错：
    # DOC_IMPORT_VLM_MODEL / DOC_IMPORT_VLM_LOCATION / DOC_IMPORT_VLM_MAX_REGIONS /
    # DOC_IMPORT_VLM_THINKING_BUDGET / DOC_IMPORT_VLM_MAX_OUTPUT_TOKENS。
    DOC_IMPORT_VLM_MODEL: str = "gemini-2.5-flash"  # [遗留-Vertex] 不再使用
    DOC_IMPORT_VLM_LOCATION: str = "global"  # [遗留-Vertex] 不再使用
    DOC_IMPORT_VLM_TIMEOUT_SEC: float = 120.0  # [遗留-Vertex] 不再使用
    DOC_IMPORT_VLM_MAX_REGIONS: int = 2  # [遗留-Vertex] 不再使用
    DOC_IMPORT_VLM_TEMPERATURE: float = 0.2  # 仍用于 Azure 采样温度
    DOC_IMPORT_VLM_MAX_OUTPUT_TOKENS: int = 8192  # [遗留-Vertex] Azure 用 DOC_IMPORT_AZURE_MAX_TOKENS
    DOC_IMPORT_VLM_THINKING_BUDGET: int = 0  # [遗留-Vertex] 不再使用
    # 单次请求内并发打标的图片数（Azure 侧仅受此并发约束，无全局限流器）。
    DOC_IMPORT_VLM_CONCURRENCY: int = 4
    # 单张图片打标时随附所在页整页 PNG 作为上下文（更懂图在讲什么）；关掉可省 token。
    DOC_IMPORT_VLM_WITH_PAGE_CONTEXT: bool = True

    # ===== Azure VLM 打标（FW-Kimi-K2.7-Code，Azure OpenAI 兼容 chat/completions） =====
    # 模型名：运行时由 utils.azure_models 从 secrets/azure-models.yaml 解析出
    # endpoint + api-key + api_version（含 region fallback），不再硬编码密钥。
    DOC_IMPORT_AZURE_MODEL: str = "FW-Kimi-K2.7-Code"
    # 可选显式覆盖：留空=走 yaml；配置后优先用（应急/本地调试）。切勿把明文 key 写进版本库。
    DOC_IMPORT_AZURE_ENDPOINT: Optional[str] = None
    DOC_IMPORT_AZURE_API_KEY: Optional[str] = None
    DOC_IMPORT_AZURE_DEPLOYMENT: Optional[str] = None
    DOC_IMPORT_AZURE_API_VERSION: Optional[str] = None
    # 单次打标最大输出 token。
    DOC_IMPORT_AZURE_MAX_TOKENS: int = 2048
    # 连接超时压到 10s：网络不通时每张图最多等 10s 而非 120s，避免整体雪崩。
    DOC_IMPORT_AZURE_CONNECT_TIMEOUT: float = 10.0
    DOC_IMPORT_AZURE_READ_TIMEOUT: float = 60.0
    DOC_IMPORT_AZURE_WRITE_TIMEOUT: float = 30.0
    # Azure API 出网代理；留空=直连（Azure 国内通常可直连，直连最稳）。
    DOC_IMPORT_AZURE_PROXY_URL: Optional[str] = None
    # 单张图片打标失败重试次数（仅对可重试错误，如 5xx/连接抖动）。
    DOC_IMPORT_AZURE_MAX_RETRIES: int = 2
    # 连续 N 张图因“连接不可达”失败则整批短路跳过剩余图（把雪崩压到秒级）。
    DOC_IMPORT_AZURE_CIRCUIT_CONSECUTIVE_CONN_FAILS: int = 5


class ZhipinSettings(_Base):
    """对应 api/zhipin_job.py / api/pc_drissionpage_new.py"""
    ZHIPIN_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    DRISSION_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    # 智联招聘的开放字典服务（接口契约的一部分，但留出可覆盖的口子，
    # 方便联调/灰度环境替换为镜像/模拟服务）
    ZHAOPIN_DICT_URL: str = "https://dict.zhaopin.cn/dict/dictOpenService/getDict"
    ZHAOPIN_DICT_HTTP_TIMEOUT: float = 10.0


class BossZhipinSettings(_Base):
    """对应 api/boss_zhipin.py + services/boss_zhipin_client.py。"""
    BOSS_ZHIPIN_API_KEY: Optional[str] = None
    BOSS_ZHIPIN_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    BOSS_ZHIPIN_MAX_COMBINATIONS: int = 10
    BOSS_ZHIPIN_MAX_PAGES: int = 3
    BOSS_ZHIPIN_MAX_ITEMS_PER_QUERY: int = 100
    BOSS_ZHIPIN_LISTEN_TIMEOUT_SEC: float = 15.0
    BOSS_ZHIPIN_SYNC_TIMEOUT_SEC: float = 90.0
    BOSS_ZHIPIN_MIN_DELAY_SEC: float = 1.0
    BOSS_ZHIPIN_MAX_DELAY_SEC: float = 2.5
    BOSS_ZHIPIN_DETAIL_MIN_DELAY_SEC: float = 1.0
    BOSS_ZHIPIN_DETAIL_MAX_DELAY_SEC: float = 2.0
    # 直连模式：浏览器只负责铸造 __zp_stoken__ cookie，列表/详情改用 httpx 直接
    # 调用官方 wapi 接口，大幅降低每页/每条详情的耗时（实测列表/详情均 <0.5s）。
    BOSS_ZHIPIN_DIRECT_ENABLED: bool = True
    # 单个 __zp_stoken__ 的安全调用配额；实测一次浏览器导航刷新后恰好支持 5 次
    # 成功调用（列表/详情共享），第 6 次必返回 code=37。留 1 次余量取 5。
    BOSS_ZHIPIN_DIRECT_BUDGET_PER_TOKEN: int = 5
    BOSS_ZHIPIN_MAX_CONCURRENCY: int = 2
    # BOSS 单例客户端允许的并发 scrape 数。1=严格串行（历史行为）；2=实测可真并行且未风控。
    # >1 时：每个并发槽有独立 httpx client + 独立 __zp_stoken__ 配额，互不污染 cookie；
    # 唯一共享的浏览器 tab（铸造 cookie / 浏览器回退）用可重入锁串行，避免 tab 被并发搞崩。
    # 注意：这是把风控暴露给 BOSS，若频繁 access_limited 请改回 1。
    BOSS_ZHIPIN_DIRECT_HTTP_TIMEOUT: float = 15.0
    # 直连列表默认每页条数（summary 快路径用，保持与历史一致=30，不影响外部 workflow）。
    # 官方实测 15/30 均遵守。逐条详情(description)累积路径会按"单轮记录预算"临时用更小
    # 的 pageSize（见 jobs_region._run_boss），让每轮 1 页带详情的串行慢采时间可控。
    BOSS_ZHIPIN_DIRECT_PAGE_SIZE: int = 30
    # 直连模式下两次 httpx 调用（列表翻页 / 逐条详情）之间的随机延时。
    # BOSS 全程串行，这里刻意放慢节奏（"串行慢慢采集"）以显著降低风控概率；
    # 是控制 BOSS 抓取速率的主要旋钮，调大更稳、调小更快。
    BOSS_ZHIPIN_DIRECT_MIN_DELAY_SEC: float = 1.2
    BOSS_ZHIPIN_DIRECT_MAX_DELAY_SEC: float = 3.0
    # 铸造 cookie 时导航后等待 JS 生成 stoken 的时间。实测 <1.2s 偏早会 code=37，
    # 取 1.5s 留安全余量。
    BOSS_ZHIPIN_DIRECT_COOKIE_WAIT_SEC: float = 1.5


class RegionJobsSettings(_Base):
    """对应 api/jobs_region.py（区域岗位统一搜索）。"""
    REGION_JOBS_API_KEY: Optional[str] = None
    REGION_JOBS_MAX_PAGES_PER_SOURCE: int = 3
    REGION_JOBS_MAX_RECORDS_PER_SOURCE: int = 50
    REGION_JOBS_MAX_COMBINATIONS: int = 20
    # BOSS 命中访问受限 / IP 异常 / 验证码 / 连续 code=37 时，进程内熔断的默认冷却分钟数
    # （页面自带「将于 X 恢复正常」时优先用页面时间）。冷却期间 BOSS 来源直接快速返回，
    # 不触碰浏览器和 BOSS 接口，避免越采越封。
    REGION_JOBS_BOSS_COOLDOWN_MINUTES: int = 120
    # BOSS 单来源采集超时后，为保护共享浏览器 tab / httpx 会话（旧线程可能仍在运行），
    # 熔断 BOSS 的秒数；短于风控冷却，仅用于让在跑的同步线程自然收尾。
    REGION_JOBS_BOSS_TIMEOUT_COOLDOWN_SEC: int = 60
    # BOSS 串行 + 慢节奏，逐条拉详情（detail_level=description）耗时随记录数线性增长。
    # 为保证单轮请求能在 timeout_seconds 内收尾（否则超时->熔断->反而拿不到数据），
    # 当 BOSS 需要详情时，对单来源实际抓取的记录数做服务端安全上限：调用方仍可要更多，
    # 由上层应用（如 data_server）分多轮"慢慢"累积。仅在 detail_level=description 时生效，
    # summary 快速路径不受影响。设为 0 表示不限制。
    REGION_JOBS_BOSS_MAX_DETAIL_RECORDS: int = 15
    # 账户需要登录 / 命中风控时，向该管理员邮箱发送提醒邮件，保证接口稳定在线。
    # 复用 JOB_SEARCH_SMTP_* 邮件配置（HOST/PORT/USERNAME/PASSWORD/FROM/USE_SSL/STARTTLS）。
    REGION_JOBS_ADMIN_EMAIL: str = "hx1561958968@gmail.com"
    # 同一来源风控/登录提醒邮件的冷却秒数，避免风控冷却期内重复轰炸管理员。
    REGION_JOBS_NOTIFY_COOLDOWN_SEC: int = 600


class WebSearchSettings(_Base):
    """对应 api/web_search.py + services/web_search/*。

    统一 Web 搜索/抓取后端：
    - 下沉 Tavily / SearchAPI Google 两家 provider，密钥仅在后端持有
    - 复用 api/url_content_fetch.fetch_url_content 做正文抓取
    - 缓存层走 Redis（不可用时静默降级为不缓存）
    """
    # 守卫本服务对外端点的 x-api-key；留空 = 不启用鉴权（行为对齐 REGION_JOBS_API_KEY）
    WEB_SEARCH_API_KEY: Optional[str] = None
    # 两家 provider 的密钥；任一为空时该 provider 视为未配置，自动跳过
    TAVILY_API_KEY: Optional[str] = None
    SEARCHAPI_IO_API_KEY: Optional[str] = None
    # auto 模式下的回退链；按顺序尝试，前者无结果/失败才走下一个
    WEB_SEARCH_DEFAULT_PROVIDERS: List[str] = ["tavily", "searchapi_google"]
    # top_k 默认/上限；取两家 provider 的下界，SearchAPI Google 自 2025-09 起锁 num=10
    WEB_SEARCH_DEFAULT_TOP_K: int = 5
    WEB_SEARCH_MAX_TOP_K: int = 10
    # 单 provider 调用超时（秒）
    WEB_SEARCH_PROVIDER_TIMEOUT_SEC: float = 30.0
    # 正文抓取（HTML/文档）超时
    WEB_SEARCH_FETCH_HTML_TIMEOUT_SEC: float = 15.0
    WEB_SEARCH_DOC_DOWNLOAD_TIMEOUT_SEC: float = 60.0
    # 并发：单次请求内允许的最大并发抓取条数
    WEB_SEARCH_DEFAULT_CONCURRENCY: int = 5
    WEB_SEARCH_MAX_CONCURRENCY: int = 10
    # 单条正文最大字符数
    WEB_SEARCH_DEFAULT_CONTENT_CHARS: int = 8000
    WEB_SEARCH_MAX_CONTENT_CHARS: int = 50000
    # Provider HTTP 基地址（联调/灰度可指向 mock）
    WEB_SEARCH_TAVILY_BASE_URL: str = "https://api.tavily.com"
    WEB_SEARCH_SEARCHAPI_BASE_URL: str = "https://www.searchapi.io/api/v1/search"
    # 缓存开关与 TTL
    WEB_SEARCH_CACHE_ENABLED: bool = True
    WEB_SEARCH_CACHE_TTL_SEARCH_SEC: int = 300
    WEB_SEARCH_CACHE_TTL_FETCH_SEC: int = 1800
    # 顶层请求超时（端到端，包裹 asyncio.wait_for）
    WEB_SEARCH_REQUEST_TIMEOUT_SEARCH_SEC: float = 35.0
    WEB_SEARCH_REQUEST_TIMEOUT_SEARCH_AND_FETCH_SEC: float = 90.0
    WEB_SEARCH_REQUEST_TIMEOUT_FETCH_SEC: float = 120.0
    WEB_SEARCH_REQUEST_TIMEOUT_TAVILY_SITE_SEC: float = 150.0


class TianyanchaSettings(_Base):
    """对应 api/tianyancha.py + services/tianyancha_client.py。"""
    TIANYANCHA_API_KEY: Optional[str] = None
    TIANYANCHA_TOKEN: Optional[str] = None
    TIANYANCHA_SEARCH_URL: str = "http://open.api.tianyancha.com/services/open/searchx"
    TIANYANCHA_BASEINFO_URL: str = (
        "http://open.api.tianyancha.com/services/open/ic/baseinfo/normal"
    )
    TIANYANCHA_AREA_CODE_URL: str = (
        "https://jindi-oss-open.oss-cn-beijing.aliyuncs.com/document/newAreaCodeV2024.json"
    )
    TIANYANCHA_CATEGORY_URL: str = (
        "https://jindi-oss-open.oss-cn-beijing.aliyuncs.com/document/category.json"
    )
    TIANYANCHA_HTTP_TIMEOUT: float = 15.0
    TIANYANCHA_SEARCH_CACHE_TTL_SECONDS: int = 31536000
    TIANYANCHA_BASEINFO_TTL_DAYS: int = 3650
    TIANYANCHA_MAX_PAGE_SIZE: int = 20
    TIANYANCHA_MAX_PAGES_PER_REQUEST: int = 5
    TIANYANCHA_MAX_DETAIL_CALLS_PER_REQUEST: int = 20
    TIANYANCHA_ENABLE_REMOTE: bool = True
    TIANYANCHA_DIFY_DEFAULT_LIMIT: int = 20
    TIANYANCHA_DIFY_MAX_LIMIT: int = 50
    TIANYANCHA_DIFY_MAX_DETAIL_CALLS_PER_REQUEST: int = 50
    TIANYANCHA_ENRICH_NEW_COMPANIES: bool = True


# =====================================================================
# 合成最终 Settings
# =====================================================================

class Settings(
    AppSettings, DBSettings, RabbitSettings, RedisSettings, CommonSettings,
    CreAudioSettings, CreAudioJsonSettings, CreAudioV2Settings,
    CreAudioRefactoredSettings, CreAudioOriginalSpeedSettings,
    TtsSettings, MurfTtsSettings, GoogleTtsSettings, FishAsrSettings,
    CreImageSettings, CreImageAzureSettings, GeminiLiveSettings, CreVideoSettings, FenbiSettings,
    VideoCompressSettings, ConverterSettings, JobSearchSettings,
    JobSearchV2Settings,
    TuoyuSerpSettings, UrlFetchSettings, DocParserSettings, FileParseSettings,
    FileUnderstandSettings, DocImportSettings,
    ZhipinSettings, BossZhipinSettings, RegionJobsSettings,
    WebSearchSettings,
    TianyanchaSettings,
):
    """全局唯一的配置对象。模块中只需 ``from utils.settings import settings`` 后取值。"""

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    @property
    def static_dir_abs(self) -> str:
        """STATIC_DIR 的绝对路径；若是相对路径则相对项目根。"""
        p = Path(self.STATIC_DIR)
        return str(p if p.is_absolute() else _PROJECT_ROOT / p)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
