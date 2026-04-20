# -*- coding: utf-8 -*-
# @File：zhilian_scraper_router.py
# @Time：2025/08/07 11:30
# @Author：_不咬闰土的猹丶 (Refactored by Senior Software Engineer)
# @email：hx1561958968@gmail.com

# --- 导入模块 ---
import json
import logging
import random
import sys
import time
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import aiohttp
# FastAPI 相关导入
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# DrissionPage 相关导入
try:
    from DrissionPage import ChromiumPage, SessionPage
    from DrissionPage.common import By, Keys
except ImportError:
    ChromiumPage, SessionPage, By, Keys = None, None, None, None,
    print("CRITICAL: DrissionPage library not found. The scraper endpoint will be disabled.")

# ======================================================================================
# --- 工业级日志配置 ---
# ======================================================================================
# utils.logger 是仓库内必需模块，删除冗余 fallback；导入失败应直接报错暴露问题
from utils.logger import setup_module_logger

logger = setup_module_logger(__name__, "logs/jobs/zhilian.log")
# ======================================================================================

router = APIRouter()

# --- 全局配置 ---
from utils.settings import settings as _settings  # noqa: E402  (settings 单点入口)
BROWSER_HOST_PORT = _settings.JOB_SEARCH_BROWSER_HOST_PORT
MAX_CONCURRENT_TASKS = _settings.JOB_SEARCH_MAX_CONCURRENT  # 合理的并发任务数，避免对目标网站造成过大压力
# 智联招聘登录凭证：必须由 .env 提供（ZHILIAN_USERNAME / ZHILIAN_PASSWORD），不再保留代码内默认值
ZHILIAN_USERNAME = _settings.ZHILIAN_USERNAME or ""
ZHILIAN_PASSWORD = _settings.ZHILIAN_PASSWORD or ""


# ======================================================================================
# --- API 响应模型与工具函数 ---
# ======================================================================================

# 统一从 utils.responses 引入，避免 10 处重复定义；行为完全一致
from utils.responses import StandardResponse, create_standard_response  # noqa: F401


# ======================================================================================
# --- 【重构】核心爬虫业务逻辑 ---
# ======================================================================================

def handle_login_if_needed(page):
    """
    【新增】
    检查并处理登录弹窗的函数。
    - 如果检测到登录弹窗，则自动输入预设的账号密码完成登录。
    """
    try:
        # 检查登录弹窗是否存在 (使用一个足够独特的元素)
        login_popup = page.ele('//div[@class="pass-login-container"]', timeout=3)
        if login_popup and login_popup.is_displayed():
            logger.info("检测到登录弹窗，正在尝试自动登录...")
            # 点击“密码登录”
            page.ele('//div[@class="pass-login-tab-item pass-login-tab-item__password"]').click()
            time.sleep(0.5)
            # 输入账号
            page.ele('//input[@placeholder="请输入手机号或邮箱"]').input(ZHILIAN_USERNAME)
            time.sleep(0.5)
            # 输入密码
            page.ele('//input[@placeholder="请输入密码"]').input(ZHILIAN_PASSWORD)
            time.sleep(0.5)
            # 点击登录按钮
            page.ele('//button[contains(@class, "pass-login-submit")]').click()
            logger.info("已提交登录信息，等待页面跳转...")
            time.sleep(3)  # 等待登录完成
            logger.info("自动登录完成。")
    except Exception:
        # 如果没有找到登录弹窗，或者登录过程中出错，则静默处理，因为可能页面已经处于登录状态
        logger.info("未检测到登录弹窗或已登录，继续执行...")
        pass


# 不需要 re 模块了

async def scrape_job_details_async(
        session: aiohttp.ClientSession,
        position_number: str
) -> Optional[Dict[str, Any]]:
    """
    【异步重构版】
    使用 aiohttp 异步请求职位详情API，实现高并发。
    """
    if not position_number:
        logger.warning("传入的 position_number 为空，跳过 API 请求。")
        return None
    api_url = _settings.ZHAOPIN_DETAIL_API_TEMPLATE.format(number=position_number)
    # 日志移到外面统一管理，避免刷屏
    try:
        async with session.get(api_url, timeout=10) as response:
            if response.status == 200:
                json_data = await response.json()
                if json_data.get('code') == 200 and 'data' in json_data:
                    return json_data['data']
                else:
                    logger.warning(
                        f"API for {position_number} 返回业务错误: "
                        f"code={json_data.get('code')}, message={json_data.get('message')}"
                    )
                    return None
            else:
                logger.warning(
                    f"请求详情API {api_url} 失败, HTTP状态码: {response.status}"
                )
                return None
    except asyncio.TimeoutError:
        logger.error(f"请求详情API {api_url} 超时。")
        return None
    except aiohttp.ClientError as e:
        logger.error(f"请求详情API {api_url} 期间发生客户端错误: {e}")
        return None
    except Exception as e:
        logger.error(f"请求详情API {api_url} 期间发生未知异常: {e}", exc_info=False)
        return None


async def fetch_all_details_concurrently(position_numbers: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    【新增】
    并发获取多个职位详情，并以字典形式返回结果。
    """
    if not position_numbers:
        return {}

    logger.info(f"准备并发获取 {len(position_numbers)} 个职位详情...")
    async with aiohttp.ClientSession() as session:
        tasks = [
            scrape_job_details_async(session, number) for number in position_numbers
        ]
        results = await asyncio.gather(*tasks)

    # 将结果与 position_number 对应起来，方便后续合并
    return {num: res for num, res in zip(position_numbers, results)}


# --------------------------------------------------------------------------------------


def scrape_single_combination(keyword: str, province: str, page_size: int) -> List[Dict[str, Any]]:
    """
    为单个 "关键词-省份" 组合执行爬虫任务的函数。
    - 每个任务使用一个独立的浏览器 Tab 来实现隔离。
    - 包含完整的页面导航、数据提取、翻页和异常处理逻辑。
    """

    # --- 内部辅助函数 ---
    def goto_html(page, url):
        page.get(url)

    def input_keyword(page, kw):
        search_input_obj = page.ele((By.XPATH, '//div[@class="query-search__content-input__wrap"]/input'))
        search_input_obj.clear()
        time.sleep(random.uniform(0.2, 0.5))
        search_input_obj.input(kw)
        time.sleep(random.uniform(0.2, 0.5))
        search_input_obj.input(Keys.ENTER)

    def get_province(page, prov):
        page.listen.clear()
        area_obj = page.ele((By.XPATH, '//div[@class="content-s"]/div[1]'))
        area_obj.click()
        time.sleep(random.uniform(0.2, 0.5))
        area_input_obj = page.ele((By.XPATH, '//div[@class="query-other-city"]/input'))
        area_input_obj.input(prov)
        if prov in ['吉林', '海南']:
            page.ele((By.XPATH, f'//ul[@class="query-other-city__list"]/li[contains(.,"{prov}")]'), timeout=10).click()
        else:
            page.ele((By.XPATH, '//ul[@class="query-other-city__list"]/li[1]'), timeout=10).click()

    def drop_down(page):
        for x in range(1, 10, 3):
            j = x / 9
            js = 'document.documentElement.scrollTop = document.documentElement.scrollHeight * %f' % j
            page.run_js(js)
            time.sleep(random.uniform(0.5, 1))

    def get_data(item, prov) -> List[Dict[str, Any]]:
        extracted_data = []
        json_data = item.response.body
        print(json_data)
        if json_data:
            try:
                for data in json_data['data']['list']:
                    dic = {
                        'name': data.get('name'),
                        'salary': data.get('salary60'),
                        'jobSkillTags': [i.get('name') for i in data.get('jobSkillTags')] if data.get(
                            'jobSkillTags') else [],
                        'jobKnowledgeWelfareFeatures': data.get('jobKnowledgeWelfareFeatures'),
                        'province': prov,
                        'address': json.loads(data.get('cardCustomJson')).get('address'),
                        'workingExp': data.get('workingExp'),
                        'education': data.get('education'),
                        'companyName': data.get('companyName'),
                        "companyLogo": data.get('companyLogo'),
                        "companyUrl": data.get('companyUrl'),
                        "positionURL": data.get('positionURL'),
                        'positionNumber': data.get('number'),
                        'companySize': data.get('companySize'),
                        'propertyName': data.get('propertyName'),
                        'industryName': data.get('industryName'),
                    }
                    extracted_data.append(dic)
                logger.info(f"[{keyword} | {prov}] 成功解析 {len(extracted_data)} 条岗位数据。")
            except Exception as e:
                logger.error(f"[{keyword} | {prov}] JSON解析或数据提取时发生错误: {e}", exc_info=True)
        return extracted_data

    def get_next(page):
        next_flag = page.wait.eles_loaded((By.XPATH, '//a[@class="btn soupager__btn"]'), timeout=2)
        if next_flag:
            page.ele((By.XPATH, '//a[@class="btn soupager__btn"]')).click()
            time.sleep(random.uniform(0.5, 1))
            return True
        else:
            return False

    # --- 爬虫主流程 ---
    page = None
    # jobdetail_page = None
    task_results = []
    try:
        page = ChromiumPage(BROWSER_HOST_PORT).new_tab()
        api_url = '/c/i/search/positions?'
        page.listen.start(api_url)
        initial_url = _settings.ZHAOPIN_LIST_URL
        goto_html(page, initial_url)
        time.sleep(1)

        # 【核心新增】在开始操作前，检查并处理登录
        handle_login_if_needed(page)

        logger.info(f"开始处理组合: '{keyword}' - '{province}'")
        input_keyword(page, keyword)
        time.sleep(0.5)
        get_province(page, province)

        # jobdetail_page = ChromiumPage(BROWSER_HOST_PORT).new_tab()
        current_page_num = 1
        while True:
            logger.info(f"等待采集 -> {keyword} | {province} | 第 {current_page_num} 页")
            drop_down(page)
            packet = page.listen.wait(timeout=15)  # 【优化】延长超时时间

            if not packet:
                logger.warning(f"在第 {current_page_num} 页等待数据超时，可能已无更多内容。")
                break

            if api_url in packet.url:
                page_results = get_data(packet, province)

                # 【整合修改】核心变更点：从串行同步请求改为批量异步并发请求
                if page_results:
                    # 1. 提取本页所有职位编号
                    position_numbers = [
                        job.get("positionNumber") for job in page_results if job.get("positionNumber")
                    ]

                    # 2. 使用 asyncio.run 在当前线程中执行异步并发任务
                    details_map = asyncio.run(fetch_all_details_concurrently(position_numbers))

                    # 3. 将获取到的详情数据合并回原始列表
                    for job_data in page_results:
                        p_number = job_data.get("positionNumber")
                        job_data['job_details'] = details_map.get(p_number)  # 如果获取失败，会自动为None

                    logger.info(
                        f"[{keyword} | {province}] 第 {current_page_num} 页详情获取完成，成功 {len([d for d in details_map.values() if d])} / {len(details_map)}。")
                task_results.extend(page_results)


            else:
                logger.warning(f"捕获到非预期的数据包: {packet.url}, 已跳过。")

            if 0 < page_size <= current_page_num:
                logger.info(f"已达到设定的爬取页数限制 ({page_size})，停止采集当前组合。")
                break

            if not get_next(page):
                logger.info(f"组合 '{keyword}'-'{province}' 已无下一页，完成采集。")
                break

            current_page_num += 1

        return task_results

    except Exception as e:
        logger.critical(f"爬虫任务 '{keyword}'-'{province}' 执行期间发生严重错误: {e}", exc_info=True)
        raise
    finally:
        if page:
            logger.info(f"正在关闭任务 '{keyword}'-'{province}' 的浏览器页面...")
            page.close()
            logger.info(f"任务 '{keyword}'-'{province}' 的浏览器页面已关闭。")


def run_zhilian_scraper_concurrent(keywords: List[str], provinces: List[str], page_size: int) -> List[Dict[str, Any]]:
    """
    使用线程池并发执行多个爬虫任务的函数。
    - 将 "关键词-省份" 列表分解为多个独立的任务。
    - 使用 ThreadPoolExecutor 并发执行 `scrape_single_combination`。
    - 汇总所有并发任务的结果并返回。
    """
    all_results = []
    tasks_to_run = [(kw, prov) for kw in keywords for prov in provinces]

    logger.info(f"准备启动 {len(tasks_to_run)} 个并发爬虫任务，最大并发数: {MAX_CONCURRENT_TASKS}...")

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS) as executor:
        future_to_task = {
            executor.submit(scrape_single_combination, kw, prov, page_size): (kw, prov)
            for kw, prov in tasks_to_run
        }

        for future in as_completed(future_to_task):
            task_id = future_to_task[future]
            try:
                result_data = future.result()
                all_results.extend(result_data)
                logger.info(f"任务 {task_id} 成功完成，获得 {len(result_data)} 条数据。")
            except Exception as exc:
                logger.error(f"任务 {task_id} 执行失败: {exc}", exc_info=True)

    logger.info(f"所有并发任务完成，共采集到 {len(all_results)} 条数据。")
    return all_results


# ======================================================================================
# --- API 端点 ---
# ======================================================================================

class ScraperPayload(BaseModel):
    keywords: List[str] = Field(..., description="要搜索的岗位关键词列表", example=["大数据", "Java工程师"])
    provinces: List[str] = Field(..., description="要搜索的省份或城市列表", example=["深圳", "北京"])
    page_size: int = Field(
        default=3,
        description="为每个“关键词-省份”组合爬取的最大页数。设置为 0 则表示爬取所有可用的页数。",
        ge=0
    )


@router.post("/scrape/zhilian", summary="启动智联招聘并发爬虫任务")
async def scrape_zhilian_jobs(payload: ScraperPayload):
    """
    【重构】
    接收关键词、省份列表和爬取页数，以**并发模式**启动一个后台爬虫任务来抓取智联招聘的岗位数据。

    - **这是一个长时任务**，但服务器会保持响应。
    - **并发执行**: 多个“关键词-省份”组合将并行抓取，以提高效率。
    - **page_size**: 控制每个组合爬取的最大页数，`0` 代表不限制。
    """
    if not ChromiumPage:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scraper service is disabled due to missing 'DrissionPage' dependency."
        )

    try:
        logger.info(
            f"接收到并发爬虫请求: keywords={payload.keywords}, provinces={payload.provinces}, page_size={payload.page_size}")

        loop = asyncio.get_running_loop()

        # 【核心变更】调用并发版本的爬虫函数
        scraped_data = await loop.run_in_executor(
            None,  # 使用默认的 ThreadPoolExecutor
            run_zhilian_scraper_concurrent,
            payload.keywords,
            payload.provinces,
            payload.page_size
        )

        message = f"并发爬虫任务成功完成，共找到 {len(scraped_data)} 个岗位。"
        logger.info(message)
        return create_standard_response(data=scraped_data, message=message)

    except Exception as e:
        error_message = f"执行并发爬虫任务期间发生错误: {str(e)}"
        logger.error(error_message, exc_info=True)
        return create_standard_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=error_message
        )
