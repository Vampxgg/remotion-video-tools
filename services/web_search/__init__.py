# -*- coding: utf-8 -*-
"""Web 搜索/抓取的服务层。

模块拆分：
- base：抽象基类 + 数据契约 dataclass
- tavily_provider / searchapi_google_provider：两家 provider 的落地实现
- registry：provider 单例注册表 + ``build_chain`` 编排策略
- fetcher：复用 api/url_content_fetch 的正文抓取流水线
- cache：Redis 缓存的薄壳；不可用时静默降级
"""
