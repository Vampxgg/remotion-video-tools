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
    APP_WORKERS: int = 1
    APP_PUBLIC_BASE_URL: str = "http://127.0.0.1:2906"
    CORS_ALLOW_ORIGINS: List[str] = ["*"]
    # 当 CORS_ALLOW_ORIGINS=["*"] 时，CORS 规范要求 allow_credentials 必须为 False
    CORS_ALLOW_CREDENTIALS: bool = False
    CORS_ALLOW_METHODS: List[str] = ["*"]
    CORS_ALLOW_HEADERS: List[str] = ["*"]
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
    GCS_BUCKET_NAME: str = "x-pilot-storage"
    GCS_PUBLIC_URL_PREFIX: str = "https://storage.googleapis.com/x-pilot-storage"


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
    CRE_AUDIO_V2_PROXY_URL: Optional[str] = "http://127.0.0.1:7890"
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
    TTS_PROXY_URL: Optional[str] = "http://127.0.0.1:7890"
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
    MURF_TTS_PROXY_URL: Optional[str] = "http://127.0.0.1:7890"
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
    VIDEO_COMPRESS_MAX_UPLOAD_MB: int = 500
    VIDEO_COMPRESS_MAX_CONCURRENT_FFMPEG: int = 2


class ConverterSettings(_Base):
    """对应 api/converter.py"""
    CONVERTER_GENERATED_FILES_SUBDIR: str = "generated_files"
    CONVERTER_TEMP_IMAGES_SUBDIR: str = "temp_images"
    CONVERTER_CLEANUP_DELAY_SEC: int = 120


class JobSearchSettings(_Base):
    """对应 api/job_search.py（智联）。账号密码必须由 .env 提供，无默认。"""
    ZHILIAN_USERNAME: Optional[str] = None
    ZHILIAN_PASSWORD: Optional[str] = None
    JOB_SEARCH_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    JOB_SEARCH_MAX_CONCURRENT: int = 10
    ZHAOPIN_DETAIL_API_TEMPLATE: str = (
        "https://fe-api.zhaopin.com/c/i/jobs/position-detail-new?number={number}"
    )
    ZHAOPIN_LIST_URL: str = "https://www.zhaopin.com/sou/jl779/kwB4JMAS33DO/p1"


class TuoyuSerpSettings(_Base):
    """对应 api/tuoyu_serp_search.py"""
    TUOYU_SERP_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    TUOYU_SERP_GOOGLE_HOST: str = "www.google.com"
    TUOYU_SERP_INCLUDE_WECHAT: bool = True


class UrlFetchSettings(_Base):
    """对应 api/url_content_fetch.py"""
    URL_FETCH_MAX_DOCUMENT_BYTES: int = 20 * 1024 * 1024


class DocParserSettings(_Base):
    """对应 api/document_parser_service.py
    生产部署必须显式提供 DOC_PARSER_IMAGE_UPLOAD_URL，避免 192.168.x 内网泄漏。"""
    DOC_PARSER_IMAGE_UPLOAD_URL: Optional[str] = None


class ZhipinSettings(_Base):
    """对应 api/zhipin_job.py / api/pc_drissionpage_new.py"""
    ZHIPIN_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    DRISSION_BROWSER_HOST_PORT: str = "127.0.0.1:9527"
    # 智联招聘的开放字典服务（接口契约的一部分，但留出可覆盖的口子，
    # 方便联调/灰度环境替换为镜像/模拟服务）
    ZHAOPIN_DICT_URL: str = "https://dict.zhaopin.cn/dict/dictOpenService/getDict"
    ZHAOPIN_DICT_HTTP_TIMEOUT: float = 10.0


# =====================================================================
# 合成最终 Settings
# =====================================================================

class Settings(
    AppSettings, DBSettings, RabbitSettings, CommonSettings,
    CreAudioSettings, CreAudioJsonSettings, CreAudioV2Settings,
    CreAudioRefactoredSettings, CreAudioOriginalSpeedSettings,
    TtsSettings, MurfTtsSettings, GoogleTtsSettings, FishAsrSettings,
    CreImageSettings, CreVideoSettings, FenbiSettings,
    VideoCompressSettings, ConverterSettings, JobSearchSettings,
    TuoyuSerpSettings, UrlFetchSettings, DocParserSettings,
    ZhipinSettings,
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
