# -*- coding: utf-8 -*-
# @File：main.py
# @Time：2025/1/21 10:32
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
# 加载.env文件的环境变量
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
# from fastapi.openapi.utils import get_openapi
from api import Online_search, block_generator, tts, cre_audio, converter, cre_audio_json, cre_video, voice_models, \
    google_tts, job_search, fish_asr, cre_image
from fastapi.staticfiles import StaticFiles
from db.database import engine, Base, get_db

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时执行
    logger.info("应用启动...")
    try:
        async with engine.connect() as conn:
            logger.info("数据库连接测试成功。")
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        # 在生产环境中，这里可能需要更复杂的处理，比如退出应用

        # ... (其他启动代码) ...
    yield


description = """Search"""
tags_metadata = [
    {
        "name": "sources",
        "description": "search_sources",
    }
]

app = FastAPI(
    title="联网搜索 Api",
    description=description,
    version="V1.0.1",
    terms_of_service="https://www.msn.cn/zh-cn/news/other/%E5%85%A8%E7%BA%A2%E5%A9%B5%E7%8E%B0%E8%BA%AB%E4%B8%8A%E6"
                     "%B5%B7%E6%B4%BB%E5%8A%A8-%E7%83%AB%E4%BA%86%E5%A4%B4%E5%8F%91%E5%8C%96%E4%BA%86%E5%85%A8%E5%A6"
                     "%86%E7%9A%84%E5%85%A8%E5%A6%B9%E5%A5%BD%E6%BC%82%E4%BA%AE-%E5%90%8C%E6%AC%BE%E7%BE%BD%E7%BB%92"
                     "%E6%9C%8D%E5%BD%93%E6%99%9A%E5%8D%96%E6%96%AD%E8%B4%A7/ar-AA1uLwML?ocid=msedgntp&pc=U531&cvid"
                     "=67458ce9b63440b3b7e34cbe4a0a266f&ei=19",
    contact={
        "name": "X-Pliot Teams",
        "url": "https://www.msn.cn/zh-cn/news/other/%E5%85%A8%E7%BA%A2%E5%A9%B5%E7%8E%B0%E8%BA%AB%E4%B8%8A%E6%B5%B7"
               "%E6%B4%BB%E5%8A%A8-%E7%83%AB%E4%BA%86%E5%A4%B4%E5%8F%91%E5%8C%96%E4%BA%86%E5%85%A8%E5%A6%86%E7%9A%84"
               "%E5%85%A8%E5%A6%B9%E5%A5%BD%E6%BC%82%E4%BA%AE-%E5%90%8C%E6%AC%BE%E7%BE%BD%E7%BB%92%E6%9C%8D%E5%BD%93"
               "%E6%99%9A%E5%8D%96%E6%96%AD%E8%B4%A7/ar-AA1uLwML?ocid=msedgntp&pc=U531&cvid"
               "=67458ce9b63440b3b7e34cbe4a0a266f&ei=19",
        "email": "hx1561958968@gmail.com",
    },
    license_info={
        "name": "Apache 2.0",
        "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
    },
    lifespan=lifespan
    # openapi_tags=tags_metadata
)
# 确保静态目录存在
static_dir = "static"
os.makedirs(static_dir, exist_ok=True)
app.mount(f"/{static_dir}", StaticFiles(directory=static_dir), name="static")
# 配置 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有的源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有的 HTTP 方法
    allow_headers=["*"],  # 允许所有的请求头
)

# app.include_router(process_uploads.router)
app.include_router(Online_search.router, prefix="/api", tags=["Source Parser"])
app.include_router(block_generator.router, prefix="/api", tags=["block_generator"])
app.include_router(tts.router, prefix="/api", tags=["tts"])
app.include_router(cre_audio.router, prefix="/api", tags=["create_audio"])
app.include_router(cre_audio_json.router, prefix="/api", tags=["create_audio"])
app.include_router(google_tts.router, prefix="/api", tags=["create_audio"])
app.include_router(converter.router, prefix="/api", tags=["Converter"])
app.include_router(cre_video.router, prefix="/api", tags=["create_veo_video"])
app.include_router(voice_models.router, prefix="/api", tags=["voice_models"])
app.include_router(job_search.router, prefix="/api", tags=["jobs_datas"])
app.include_router(fish_asr.router_asr, prefix="/api", tags=["asr"])
app.include_router(cre_image.router, prefix="/api", tags=["create_image"])


# app.include_router(auth.router)


@app.get("/")
async def root():
    return {"message": "Hello API!"}


if __name__ == '__main__':
    uvicorn.run('main:app', host="0.0.0.0", port=2906, workers=4)
