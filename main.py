# -*- coding: utf-8 -*-
# @File：main.py
# @Time：2025/1/21 10:32
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
# 加载.env文件的环境变量
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api import Online_search, block_generator, tts, cre_audio, converter, cre_video, cre_image, voice_models, \
    job_search, fish_asr, fenbi_gateway
from fastapi.staticfiles import StaticFiles
from db.database import engine, Base, get_db
from utils.settings import settings

# from lifespan import lifespan
# import auth
# 清空任何已注册的 handler，防止某些库自己跑 basicConfig
# for h in logging.root.handlers[:]:
#     logging.root.removeHandler(h)
#
# # 只看 Warning 及以上到 stderr（也就是 nohup 的 output.log）
# logging.root.setLevel(logging.WARNING)
#
# # 同时开一个文件，用来专门记录我们想要的 INFO+ 日志
# logging.basicConfig(
#     level=logging.INFO,  # file handler 的 level
#     format="%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
#     datefmt="%Y-%m-%d %H:%M:%S",
#     handlers=[
#         logging.FileHandler("app.log", encoding="utf-8")
#     ]
# )

from utils.logger import setup_module_logger

# ======================================================================================
# [第2步: 新增] 配置主应用的日志记录器
# ======================================================================================
# logger 的名称将是 "__main__" (因为这是主执行文件)
# 我们将它的日志保存在一个专门的文件 "logs/main/app.log" 中
logger = setup_module_logger(__name__, "logs/main/app.log")
# ======================================================================================
logger.info("主应用日志系统已启动。")
# ======================================================================================
# [第2步: 核心改动] --- 控制第三方库的日志级别 ---
# ======================================================================================
# 提高常用 HTTP 客户端库的日志级别，以减少不必要的输出
# 只记录 WARNING 及以上级别的日志，忽略 INFO 和 DEBUG 级别的日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger.info("第三方库日志级别已设置为 WARNING，以减少噪音。")
# ======================================================================================
logger.info("正在加载路由模块...")


# ─────────────── 各 router 的 lifespan_resources 接入清单 ───────────────
# 设计原则：
#   - 每个 router 文件都暴露一个 ``lifespan_resources(app)`` 异步上下文管理器，
#     等价替代旧的 ``@router.on_event("startup"/"shutdown")``。
#   - 这里 _LIFESPAN_MODULES 仅纳入「当前实际通过 app.include_router(...) 挂载」
#     的 router；未挂载的（如 cre_audio_json / cre_audio_refactored / cre_audioV2 /
#     google_tts）已经在自身文件内同步改造完毕，未来需要启用时只需：
#         1) 在上方 from api import ... 列表加入对应模块
#         2) 在下方 _LIFESPAN_MODULES 列表加入该模块
#         3) app.include_router(xxx.router, ...) 注册
#     即可一键生效，无需再回头改 router 文件本身。
#   - 资源乘数说明：每个 worker 进程（uvicorn --workers N）都会独立执行一遍
#     lifespan，即每个 router 的资源（线程池/AsyncClient/Session 池等）都会被
#     ×N 倍创建。这一行为与历史的 @router.on_event 完全一致，没有变化。
_LIFESPAN_MODULES = [
    tts,
    cre_audio,
    cre_video,
    cre_image,
    fish_asr,
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """统一 lifespan：先做主应用层启动检查，再用 AsyncExitStack 接入各 router 资源。"""
    logger.info("应用启动...")
    try:
        async with engine.connect() as conn:
            logger.info("数据库连接测试成功。")
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        # 在生产环境中，这里可能需要更复杂的处理，比如退出应用

    async with AsyncExitStack() as stack:
        for mod in _LIFESPAN_MODULES:
            try:
                await stack.enter_async_context(mod.lifespan_resources(app))
                logger.info(f"router 资源就绪: {mod.__name__}")
            except Exception as e:
                logger.error(f"router 资源初始化失败 [{mod.__name__}]: {e}", exc_info=True)
                raise
        yield
        logger.info("应用关闭：开始释放各 router 资源...")
    logger.info("应用关闭完成。")


description = """Search"""
tags_metadata = [
    {
        "name": "sources",
        "description": "search_sources",
    }
]

app = FastAPI(
    title="X-Pilot Api",
    description=description,
    version="V1.0.1",
    contact={
        "name": "X-Pliot Teams",
        "email": "hx1561958968@gmail.com",
    },
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
    lifespan=lifespan
)
# 静态目录统一走项目根 + settings.STATIC_DIR，避免 api/static 与 ./static 不一致
static_dir = settings.static_dir_abs
os.makedirs(static_dir, exist_ok=True)
app.mount(f"/{settings.STATIC_DIR}", StaticFiles(directory=static_dir), name="static")
# 配置 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)

# app.include_router(process_uploads.router)
app.include_router(Online_search.router, prefix="/api", tags=["Source Parser"])
app.include_router(block_generator.router, prefix="/api", tags=["block_generator"])
app.include_router(tts.router, prefix="/api", tags=["tts"])
app.include_router(cre_audio.router, prefix="/api", tags=["create_audio"])
# app.include_router(google_tts.router, prefix="/api", tags=["create_audio"])
app.include_router(converter.router, prefix="/api", tags=["Converter"])
app.include_router(cre_video.router, prefix="/api", tags=["create_veo_video"])
app.include_router(cre_image.router, prefix="/api", tags=["create_gemini_image"])
app.include_router(voice_models.router, prefix="/api", tags=["voice_models"])
app.include_router(job_search.router, prefix="/api", tags=["jobs_datas"])
app.include_router(fish_asr.router_asr, prefix="/api", tags=["fish_asr"])
app.include_router(fenbi_gateway.router, prefix="/api", tags=["fenbi_requestes"])


# app.include_router(auth.router)


@app.get("/")
async def root():
    return {"message": "Hello API!"}


if __name__ == '__main__':
    uvicorn.run(
        'main:app',
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        workers=settings.APP_WORKERS,
    )
