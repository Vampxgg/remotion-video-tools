# -*- coding: utf-8 -*-
# @File：zhipin_job.py
# @Time：2025/10/28 15:14
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
# routers/zhaopin.py

from typing import List
from enum import Enum
import httpx
from fastapi import APIRouter, Query, HTTPException, status

from utils.settings import settings as _settings

# 1. 创建一个 APIRouter 实例
#    - prefix: 给这个路由下的所有路径添加前缀，如 /zhaopin
#    - tags: 在API文档中对接口进行分组，便于查看
router = APIRouter()

# 2. 定义外部API的URL（默认与历史硬编码一致；可通过 .env 中 ZHAOPIN_DICT_URL 覆盖）
ZHAOPIN_DICT_URL = _settings.ZHAOPIN_DICT_URL


# 3. 使用枚举(Enum)来定义所有合法的 dictNames 参数值
#    这样做的好处是：
#    - 输入校验：FastAPI会自动验证传入的参数是否合法。
#    - API文档：在自动生成的文档中，会明确展示所有可选的参数值。
class DictName(str, Enum):
    region_relation = "region_relation"  # 地区信息
    education = "education"  # 学历信息
    recruitment = "recruitment"  # 是否统招
    education_specialty = "education_specialty"  # 职业类别 (应为“专业类别”)
    industry_relation = "industry_relation"  # 行业
    careet_status = "careet_status"  # 到岗状态
    job_type_parent = "job_type_parent"  # 职位类别
    job_type_relation = "job_type_relation"  # 职位


# 4. 定义API接口
@router.get(
    "/dict",
    summary="获取智联字典数据",
    description="代理调用智联招聘的开放字典服务，可以一次查询一个或多个字典类型。"
)
async def get_zhaopin_dictionary(
        # 使用 Query 参数接收一个或多个 dict_names
        # 例如: /dict?dict_names=education&dict_names=industry_relation
        dict_names: List[DictName] = Query(
            ...,  # "..." 表示这个参数是必需的
            title="字典名称",
            description="需要查询的字典类型名称，可多选。"
        )
):
    """
    通过代理方式，获取智联招聘的字典数据。

    - **dict_names**: 一个或多个字典名称。
      - `region_relation`: 地区信息
      - `education`: 学历信息
      - `recruitment`: 招聘信息（是否统招）
      - `education_specialty`: 专业类别
      - `industry_relation`: 行业
      - `careet_status`: 到岗状态
      - `job_type_parent`: 职位类别
      - `job_type_relation`: 职位
    """
    # 5. 将接收到的枚举列表转换为逗号分隔的字符串
    #    例如: [DictName.education, DictName.industry_relation] -> "education,industry_relation"
    dict_names_str = ",".join([name.value for name in dict_names])

    # 6. 使用 httpx 异步请求外部 API
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                ZHAOPIN_DICT_URL,
                params={"dictNames": dict_names_str},
                timeout=_settings.ZHAOPIN_DICT_HTTP_TIMEOUT,
            )
            # 检查外部API的响应状态码
            response.raise_for_status()

        except httpx.RequestError as exc:
            # 处理网络请求相关的错误（如DNS、连接错误）
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"请求外部API时发生网络错误: {exc}"
            )
        except httpx.HTTPStatusError as exc:
            # 处理非200的HTTP状态码错误
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"外部API返回错误状态码 {exc.response.status_code}: {exc.response.text}"
            )

    # 7. 解析外部API返回的JSON数据并返回给客户端
    external_api_data = response.json()

    # 简单的检查返回结果是否符合预期
    if external_api_data.get("code") != 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"外部API返回业务错误: {external_api_data.get('message', '未知错误')}"
        )

    return external_api_data
