# Dify 依赖管理: 请确保已添加 httpx, json-repair, tavily-python
import sys
import io

# 设置标准输出编码为 utf-8，防止在 Windows 控制台下出现 UnicodeEncodeError
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import asyncio
import random
from itertools import cycle

import httpx
import re
import os
import json
import traceback
from typing import Any, Dict, List, Coroutine, Literal, Optional, Union
from abc import ABC, abstractmethod

# 引入新依赖的客户端
from tavily import AsyncTavilyClient
import json_repair
from datetime import datetime, timedelta


# ==============================================================================
# ====================== 时间解析辅助模块 =========================
# ==============================================================================
def _normalize_date(date_input: str) -> Optional[str]:
    """
    尝试将各种格式的日期字符串转换为 YYYY-MM-DD 格式。
    支持: YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD, YYYY年MM月DD日
    """
    if not date_input:
        return None

    date_input = date_input.strip()

    # 常见格式正则
    patterns = [
        (r'(\d{4})-(\d{1,2})-(\d{1,2})', '%Y-%m-%d'),
        (r'(\d{4})/(\d{1,2})/(\d{1,2})', '%Y/%m/%d'),
        (r'(\d{4})\.(\d{1,2})\.(\d{1,2})', '%Y.%m.%d'),
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日', '%Y年%m月%d日'),
        (r'(\d{4})(\d{2})(\d{2})', '%Y%m%d'),
    ]

    for pat, fmt in patterns:
        match = re.search(pat, date_input)
        if match:
            try:
                # 提取匹配的部分进行解析
                date_str = match.group(0)
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime('%Y-%m-%d')
            except ValueError:
                continue
    return None


def _parse_relative_time(text: str) -> Optional[str]:
    """解析相对时间，返回 YYYY-MM-DD"""
    if not text: return None
    today = datetime.now()
    text = text.lower()

    # 几天前 / 近几天
    match = re.search(r'(?:近|最近|)(\d+)\s*(?:天|日)(?:前|内)?', text)
    if not match: match = re.search(r'(\d+)\s*days?\s*ago', text)
    if match:
        days = int(match.group(1))
        return (today - timedelta(days=days)).strftime('%Y-%m-%d')

    # 几周前
    match = re.search(r'(?:近|最近|)(\d+)\s*周(?:前|内)?', text)
    if not match: match = re.search(r'(\d+)\s*weeks?\s*ago', text)
    if match:
        weeks = int(match.group(1))
        return (today - timedelta(weeks=weeks)).strftime('%Y-%m-%d')

    # 几月前 (简单按30天算)
    match = re.search(r'(?:近|最近|)(\d+)\s*月(?:前|内)?', text)
    if not match: match = re.search(r'(\d+)\s*months?\s*ago', text)
    if match:
        months = int(match.group(1))
        return (today - timedelta(days=months * 30)).strftime('%Y-%m-%d')

    # 几年前
    match = re.search(r'(?:近|最近|)(\d+)\s*年(?:前|内)?', text)
    if not match: match = re.search(r'(\d+)\s*years?\s*ago', text)
    if match:
        years = int(match.group(1))
        return (today - timedelta(days=years * 365)).strftime('%Y-%m-%d')

    return None


def _parse_time_filter(time_input: Any) -> Dict[str, str]:
    """
    解析时间输入，返回 {'after': 'YYYY-MM-DD', 'before': 'YYYY-MM-DD'} 字典。
    """
    result = {}
    if not time_input:
        return result

    # 1. 如果是字典
    if isinstance(time_input, dict):
        start = time_input.get('start') or time_input.get('after') or time_input.get('begin') or time_input.get(
            'start_date')
        end = time_input.get('end') or time_input.get('before') or time_input.get('end_date')

        # 尝试解析绝对时间
        norm_start = _normalize_date(str(start)) if start else None
        if not norm_start and start: norm_start = _parse_relative_time(str(start))

        norm_end = _normalize_date(str(end)) if end else None
        if not norm_end and end: norm_end = _parse_relative_time(str(end))

        if norm_start: result['after'] = norm_start
        if norm_end: result['before'] = norm_end
        return result

    # 2. 如果是字符串
    if isinstance(time_input, str):
        # 2.1 尝试解析相对时间 (e.g., "近3天", "last week") -> 默认为 after
        rel_time = _parse_relative_time(time_input)
        if rel_time:
            # 如果是相对时间，通常意味着 "从那时到现在"，即 after
            result['after'] = rel_time
            return result

        # 2.2 尝试提取绝对日期
        matches = re.findall(r'(\d{4}[-/年\.]\d{1,2}[-/月\.]\d{1,2}日?)', time_input)
        normalized_dates = []
        for m in matches:
            norm = _normalize_date(m)
            if norm: normalized_dates.append(norm)

        if len(normalized_dates) >= 2:
            # 假设第一个是 start，第二个是 end
            result['after'] = normalized_dates[0]
            result['before'] = normalized_dates[1]
        elif len(normalized_dates) == 1:
            # 只有一个日期，需要判断是 after 还是 before
            lower_input = time_input.lower()
            if any(kw in lower_input for kw in ['before', 'until', 'end', '之前', '截止']):
                result['before'] = normalized_dates[0]
            else:
                result['after'] = normalized_dates[0]

    return result


# ==============================================================================
# ====================== DIFY 本地调试辅助模块 =========================
# ==============================================================================
import pprint

# --- 本地调试开关 ---
# 在你的 IDE 中进行测试时，将此值设为 True。
# 当你准备将代码复制到 Dify 平台时，请将其改回 False，或直接删除此调试模块。
IS_LOCAL_DEBUG = True


def _dify_debug_return(data: Dict[str, Any], label: str = "Final Return") -> Dict[str, Any]:
    """
    一个用于在 Dify 代码节点中进行本地调试的包装函数。

    当 IS_LOCAL_DEBUG 为 True 时，它会漂亮地打印出最终要返回的数据，
    然后原封不动地返回该数据，以便 Dify 平台能正确接收。

    Args:
        data (Dict[str, Any]): 准备从 Dify 节点返回的数据。
        label (str, optional): 一个标签，用于在控制台输出中标识来源。默认为 "Final Return"。

    Returns:
        Dict[str, Any]: 传入的原始数据。
    """
    if IS_LOCAL_DEBUG:
        # 打印一个清晰的分隔符和标签，方便在终端中识别
        print("\n" + "=" * 40 + f" DIFY DEBUG OUTPUT [{label}] " + "=" * 40)

        # 使用 pprint 模块进行美化输出，对复杂的嵌套字典特别友好
        pprint.pprint(data, indent=2, width=120)

        # 打印结束分隔符
        print("=" * 105 + "\n")

    # 无论是否打印，都必须原封不动地返回原始数据
    return data


def _intelligent_input_parser(raw_input: Any) -> Dict[str, Any]:
    """
    健壮地解析复杂的输入结构，提取web搜索查询、职业查询数据和企业查询名称。
    """
    if isinstance(raw_input, list) and len(raw_input) > 0:
        actual_input = raw_input[0]
    else:
        actual_input = raw_input

    if isinstance(actual_input, str):
        if not actual_input.strip():
            # 【调整】如果输入为空字符串，返回所有字段都为空的结构，避免下游报错。
            return {"comprehensive_queries": [], "career_query_data": {}, "tianyan_enterprise_names": []}
        try:
            data = json_repair.loads(actual_input)
        except Exception as e:
            raise ValueError(f"Failed to parse input string as JSON: {e}\nOriginal input: {str(actual_input)[:500]}...")
    elif isinstance(actual_input, dict):
        data = actual_input
    else:
        raise TypeError(f"Expected input type is str, list, or dict, but received {type(actual_input).__name__}")
    if not isinstance(data, dict):
        raise ValueError("Parsed data is not a valid object (dictionary).")

    # 安全地提取 web_queries 对象
    web_queries_obj = data.get("web_queries", {})
    if not isinstance(web_queries_obj, dict): web_queries_obj = {}
    # 1. 提取 comprehensive_query
    comprehensive_queries = []
    query_list = web_queries_obj.get("comprehensive_query", [])
    if isinstance(query_list, list) and all(isinstance(item, str) for item in query_list):
        comprehensive_queries = query_list

    # 2. 提取 career_query
    career_query_data = web_queries_obj.get("career_query", {})
    if not isinstance(career_query_data, dict): career_query_data = {}
    # 3. 提取 tianyan_check_enterprise
    tianyan_input = web_queries_obj.get("tianyan_check_enterprise", [])  # 默认值改为空列表
    tianyan_enterprise_names: List[str] = []

    if isinstance(tianyan_input, str):
        # 如果是字符串，清理后包装成单元素列表
        cleaned_name = tianyan_input.strip()
        if cleaned_name:
            tianyan_enterprise_names.append(cleaned_name)
    elif isinstance(tianyan_input, list):
        # 如果是列表，清理并过滤掉无效元素
        tianyan_enterprise_names = [
            str(item).strip() for item in tianyan_input
            if isinstance(item, str) and str(item).strip()
        ]

    return {
        "comprehensive_queries": comprehensive_queries,
        "career_query_data": career_query_data,
        "tianyan_enterprise_names": tianyan_enterprise_names
    }


# ==============================================================================
# ====================== 全局搜索策略配置 ======================
# ==============================================================================
SEARCH_STRATEGY_CONFIG = {
    # 1. 泛用网页搜索 (General Web Search)
    "web": {
        "excludes": [
            "-filetype:pdf", "-filetype:docx", "-filetype:xlsx", "-filetype:pptx",
            "-inurl:login", "-inurl:register"  # 排除登录注册页面
        ]
    },

    # 2. 视频搜索 (Video Search)
    "video": {
        "includes": [
            "site:douyin.com",  # 抖音
            "site:bilibili.com",  # Bilibili
            "site:ixigua.com",  # 西瓜视频
            "site:youtube.com"  # YouTube (如果网络可达)
        ]
    },

    # 3. 行业报告与分析 (Industry Reports & Analysis)
    "industry_reports": {
        "includes": [
            "site:36kr.com",  # 36氪 - 领先的科技媒体和创投服务
            "site:iresearch.com.cn",  # 艾瑞咨询 - 知名互联网咨询机构
            "site:questmobile.com.cn",  # QuestMobile - 移动互联网数据洞察
            "site:caixin.com",  # 财新网 - 高质量的财经新闻与深度分析
            "site:iyiou.com",  # 亿欧网 - 产业创新服务平台
            "site:pedata.cn",  # 清科研究 - 专注于股权投资市场
            "site:www.deloitte.com/cn/",  # 德勤中国
            "site:www.pwccn.com",  # 普华永道中国
            "site:www.ey.com/zh_cn",  # 安永中国
            "site:home.kpmg/cn/",  # 毕马威中国
            "site:research.cicc.com"  # 中金公司研究部
        ]
    },

    # 4. 政策文件与专利 (Policy Documents & Patents)
    "policy_patents": {
        "includes": [
            "site:gov.cn",  # 中国政府网 (中央)
            "site:ndrc.gov.cn",  # 发改委
            "site:miit.gov.cn",  # 工信部
            "site:most.gov.cn",  # 科技部
            "site:cnipa.gov.cn",  # 国家知识产权局
            "site:patents.google.com",  # 谷歌专利 (覆盖全球，含中国)
            "site:soopat.com"  # Soopat 专利搜索
        ]
    },

    # 5. 专家访谈与专业洞见 (Expert Interviews & Insights)
    "expert_insights": {
        "includes": [
            "site:infoq.cn",  # InfoQ中国 - 技术专家社区
            "site:csdn.net",  # CSDN - 程序员社区
            "site:xueqiu.com"  # 雪球 - 投资者社区
        ]
    },

    # 6. 企业招聘与岗位描述 (Enterprise Recruitment)
    # 注意: 这些网站更适合内部搜索，但site:语法有时能发现被索引的公开页面
    "recruitment": {
        "includes": [
            "site:zhipin.com",  # BOSS直聘
            "site:liepin.com",  # 猎聘
            "site:zhaopin.com",  # 智联招聘
            "site:lagou.com",  # 拉勾网 (偏技术岗)
            "site:maimai.cn"  # 脉脉 (职场社交与招聘)
        ]
    },
    # 7. 托育政策与新闻 (Childcare Policy & News)
    "childcare_policy_and_news": {
        "includes": [
            "site:tuoyu.cpdrc.org.cn",  # 全国托育机构信息公示平台
            "site:zs.kaipuyun.cn",  # 卫健委相关政策搜索 (开普云)
            "site:www.tuoyufuwu.org.cn",  # 中国托育服务网 (政策/新闻/专区)
            "site:www.cpaw.org.cn"  # 中国人口学会 (政策法规)
        ]
    },

    # ========================== 托育五大核心分析维度 ==========================
    # 维度一：政策导向与区域规划 (Policy & Regional Planning)
    # 对应数据源：教育部、卫健委、人社部、中国政府网、地方发改委
    "policy_regional": {
        "includes": [
            "site:moe.gov.cn",  # 教育部 (职业教育/幼教政策)
            "site:nhc.gov.cn",  # 国家卫健委 (托育机构备案/卫生标准)
            "site:mohrss.gov.cn",  # 人社部 (职业资格/技能标准)
            "site:ndrc.gov.cn",  # 发改委 (产业规划/资金投入)
            "site:people.com.cn",  # 人民网 (权威解读)
            "site:tuoyu.cpdrc.org.cn"  # 全国托育机构信息公示平台
        ],
        "regional_patterns": [
            "site:wjw.{scope}.gov.cn",  # 地方卫健委
            "site:{scope}.edu.gov.cn",  # 地方教育局 (注意：很多地方教育局域名不统一，这是通用规则)
            "site:edu.{scope}.gov.cn",  # 另一种常见的教育局域名格式
            "site:{scope}.drc.gov.cn",  # 地方发改委
            "site:{scope}.tjj.gov.cn",  # 地方统计局
            "site:www.{scope}.gov.cn"  # 地方政府门户
        ],
        "_source_ownership": "TuoYu"
    },
    # 维度二：市场供需与产业规模 (Market Supply/Demand & Scale)
    # 对应数据源：艾媒、头豹、统计局、天眼查(公开页)
    "market_supply": {
        "includes": [
            "site:stats.gov.cn",  # 国家统计局 (人口/三产数据)
            "site:iresearch.com.cn",  # 艾瑞咨询 (行业研报)
            "site:leadleo.com",  # 头豹研究院 (深度研报)
            "site:iimedia.cn",  # 艾媒咨询 (市场数据)
            "site:drcnet.com.cn"  # 国研网 (宏观经济)
        ],
        "regional_patterns": [
            "site:{scope}.tjj.gov.cn"  # 地方统计局查看人口数据
        ],
        "_source_ownership": "TuoYu"
    },
    # 维度三：从业人员与人才需求 (Personnel & Talent Demand)
    # 对应数据源：招聘平台、院校招生、人社部
    # 注意：招聘网站通常有反爬，site:语法主要用于搜索公开的岗位分析文章或部分索引页面
    "personnel_talent": {
        "includes": [
            "site:chsi.com.cn",  # 学信网 (专业/院校开设情况)
        ],
        "_source_ownership": "TuoYu"
    },
    # 维度四：产业发展趋势与业态创新 (Trends & Innovation)
    # 对应数据源：36氪、亿欧、行业展会、智慧医疗厂商
    "trends_innovation": {
        "includes": [
            "site:36kr.com",  # 36氪 (投融资/新项目)
            "site:iyiou.com",  # 亿欧网 (产业创新)
            "site:vcbeat.top",  # 动脉网 (医育结合/医疗健康)
            "site:cyzone.cn",  # 创业邦
            "site:woshipm.com"  # 人人都是产品经理 (产品分析/模式拆解)
        ],
        "_source_ownership": "TuoYu"
    },
    # 维度五：行业规范与职业标准 (Standards & Qualifications)
    # 对应数据源：人社部技能鉴定中心、职业资格网、标准化委员会
    "standards_norms": {
        "includes": [
            "site:osta.mohrss.gov.cn",  # 职业技能鉴定中心 (证书查询/标准)
            "site:sac.gov.cn",  # 国家标准化管理委员会 (国标文件)
            "site:chinanews.com"  # 中国新闻网 (行业规范新闻)
        ],
        "_source_ownership": "TuoYu"
    },

    # 8. 专属区域/领域规则
    "exclusive_rules": {
        # 定义了多个查询模板
        "templates": [
            '"{school}" AND "{major}" site:edu.cn',
            '"{major}" AND "{scope}" site:gov.cn'
        ],
        # 对应每个模板所必需的 regional_rules 键
        "requires": [
            ["school", "major"],
            ["major", "scope"]
        ]
    }

}


def _generate_exclusive_queries(
        regional_data: Optional[Dict[str, str]] = None
) -> List[str]:
    """
    根据区域规则数据和全局配置，生成独立的、可直接搜索的查询字符串列表。

    Args:
        regional_data (Optional[Dict[str, str]]): 包含区域规则数据的字典。

    Returns:
        List[str]: 一个由专属规则生成的查询字符串组成的列表。
    """
    if not regional_data:
        return []

    strategy = SEARCH_STRATEGY_CONFIG.get("exclusive_rules", {})
    templates = strategy.get("templates", [])
    requirements = strategy.get("requires", [])

    if not templates:
        return []

    generated_queries = []
    for i, template in enumerate(templates):
        # 检查当前模板的所有必要字段是否都在 regional_data 中且不为空
        if i < len(requirements) and all(regional_data.get(key) for key in requirements[i]):
            try:
                # 格式化模板，填充数据，生成一个完整的查询
                formatted_query = template.format(**regional_data)
                generated_queries.append(formatted_query)
                print(f"  -> [Exclusive Query Generated] \"{formatted_query}\"")
            except KeyError as e:
                # 如果模板中的占位符在 regional_data 中找不到，则跳过
                print(f"  -> [Warning] Skipping rule template due to missing key: {e}")
                pass

    return generated_queries


def _build_filtered_query(original_query: str, search_type: str,
                          regional_data: Optional[Dict[str, str]] = None,
                          use_regional_patterns: bool = False,
                          time_filter: Optional[Dict[str, str]] = None
                          ) -> str:
    """
    根据全局配置，为原始查询构建带有过滤条件的最终查询字符串。

    Args:
        original_query (str): 用户的原始搜索词。
        search_type (str): 搜索类型，如 'web', 'video', 'industry_reports' 等。
        time_filter (Optional[Dict[str, str]]): 时间过滤条件，如 {'after': '2023-01-01'}。


    Returns:
        str: 附加了过滤规则的最终查询字符串。
    """
    strategy = SEARCH_STRATEGY_CONFIG.get(search_type, {})

    # 构建基础查询
    final_query = original_query

    # === 模式 A: 区域性检索 (仅当开关打开且有区域数据时) ===
    if use_regional_patterns and regional_data:
        regional_patterns = strategy.get("regional_patterns", [])
        if regional_patterns:
            valid_sites = []
            for pattern in regional_patterns:
                try:
                    formatted_site = pattern.format(**regional_data)
                    valid_sites.append(formatted_site)
                except KeyError:
                    pass
            if valid_sites:
                sites_query_part = " OR ".join(valid_sites)
                final_query = f"{original_query} ({sites_query_part})".strip()

    # === 模式 B: 标准排除/包含规则 ===
    # 注意：如果已经应用了区域模式，通常不再应用 includes，除非逻辑需要叠加。
    # 这里保持原有逻辑：如果没进区域模式，或者区域模式只是修改了 final_query，下面继续追加 excludes/includes

    if use_regional_patterns and regional_data and strategy.get("regional_patterns"):
        pass  # final_query 已经在上面构建好了
    elif "excludes" in strategy:
        exclusions = strategy["excludes"]
        if exclusions:
            final_query = f"{original_query} {' '.join(exclusions)}".strip()
    elif "includes" in strategy:
        inclusions = strategy["includes"]
        if inclusions:
            sites_query_part = " OR ".join(inclusions)
            final_query = f"{original_query} ({sites_query_part})".strip()

    # === 模式 C: 时间过滤 (新增) ===
    # Google Search 支持 after:YYYY-MM-DD 和 before:YYYY-MM-DD
    # 仅对 web 搜索生效，或者任何支持该语法的引擎
    if time_filter and search_type in ['web', 'industry_reports', 'policy_patents', 'expert_insights', 'recruitment',
                                       'childcare_policy_and_news', 'policy_regional', 'market_supply',
                                       'personnel_talent', 'trends_innovation', 'standards_norms']:
        # 基本上大部分基于 web 的搜索都支持
        if time_filter.get('after'):
            final_query += f" after:{time_filter['after']}"
        if time_filter.get('before'):
            final_query += f" before:{time_filter['before']}"

    print("-" * 20)
    print(f"Original: {original_query} -> Final: {final_query}")
    return final_query


DEFAULT_VIDEO_THUMBNAIL = "https://server.x-pilot.cn/static/meta-doc/png/797ba9dff794925f01d59c47f1248d35.png"


def _parse_video_url(url: str) -> Dict[str, Optional[str]]:
    """
    一个通用的视频URL解析器，用于从URL中提取元数据。
    可轻松扩展以支持更多平台。
    """
    # 抖音 (douyin.com)
    douyin_match = re.search(r'/video/(\d+)', url)
    if douyin_match:
        video_id = douyin_match.group(1)
        return {"video_id": video_id, "embed_url": url, "thumbnail_url": DEFAULT_VIDEO_THUMBNAIL}
    # Bilibili (bilibili.com)
    bilibili_match = re.search(r'bilibili\.com/video/(BV[a-zA-Z0-9]+)', url)
    if bilibili_match:
        video_id = bilibili_match.group(1)
        return {"video_id": video_id, "embed_url": f"//player.bilibili.com/player.html?bvid={video_id}",
                "thumbnail_url": DEFAULT_VIDEO_THUMBNAIL}  # B站封面图需要API获取，暂不处理
    # 如果没有匹配到任何已知平台，返回基本信息
    return {"video_id": None, "embed_url": url, "thumbnail_url": DEFAULT_VIDEO_THUMBNAIL}


# --- 智能解析模块 ---
# def _intelligent_input_parser(raw_input: Any) -> Dict[str, Any]:
#     """
#     健壮地解析复杂的输入结构，提取web搜索查询、职业查询数据和企业查询名称。
#     """
#     if isinstance(raw_input, list) and len(raw_input) > 0:
#         actual_input = raw_input[0]
#     else:
#         actual_input = raw_input
#     if isinstance(actual_input, str):
#         if not actual_input.strip(): raise ValueError("Input string is empty or contains only whitespace.")
#         try:
#             data = json_repair.loads(actual_input)
#         except Exception as e:
#             raise ValueError(f"Failed to parse input string as JSON: {e}\nOriginal input: {str(actual_input)[:500]}...")
#     elif isinstance(actual_input, dict):
#         data = actual_input
#     else:
#         raise TypeError(f"Expected input type is str, list, or dict, but received {type(actual_input).__name__}")
#
#     if not isinstance(data, dict): raise ValueError("Parsed data is not a valid object (dictionary).")
#     # 安全地提取 web_queries 对象
#     web_queries_obj = data.get("web_queries", {})
#     if not isinstance(web_queries_obj, dict): web_queries_obj = {}
#     # 1. 提取 comprehensive_query
#     comprehensive_queries = []
#     query_list = web_queries_obj.get("comprehensive_query", [])
#     if isinstance(query_list, list) and all(isinstance(item, str) for item in query_list):
#         comprehensive_queries = query_list
#     # 2. 提取 career_query
#     career_query_data = web_queries_obj.get("career_query", {})
#     if not isinstance(career_query_data, dict): career_query_data = {}
#
#     # 3. 【调整】提取 tianyan_check_enterprise
#     tianyan_enterprise_name = web_queries_obj.get("tianyan_check_enterprise", "")
#     if not isinstance(tianyan_enterprise_name, str): tianyan_enterprise_name = ""
#     return {
#         "comprehensive_queries": comprehensive_queries,
#         "career_query_data": career_query_data,
#         "tianyan_enterprise_name": tianyan_enterprise_name.strip()
#     }
#

# 【新增】一个可靠的抖音元数据抓取函数
# async def _fetch_douyin_metadata_reliably(
#         url: str,
#         client: httpx.AsyncClient
# ) -> Optional[Dict[str, str]]:
#     """
#     通过访问抖音页面HTML来可靠地获取视频ID和封面图。
#     - 自动处理 v.douyin.com 短链接跳转。
#     - 从页面的 <meta property="og:image"> 标签中解析封面图。
#     - 从最终的URL中解析视频ID。
#     """
#     try:
#         print(f"🔗 [Douyin Scraper] 开始解析 URL: {url}")
#
#         # 步骤 1: 发送 HEAD 请求以处理短链接跳转，获取最终的URL
#         # 使用 HEAD 请求比 GET 更快，因为它只获取头部信息
#         headers = {
#             'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1'
#         }
#         # 【注意】需要设置 allow_redirects=True
#         head_resp = await client.head(url, headers=headers, timeout=15, follow_redirects=True)
#         final_url = str(head_resp.url)
#         print(f"  -> 跳转后最终 URL: {final_url}")
#
#         # 步骤 2: 从最终URL中解析视频ID
#         video_id_match = re.search(r'/video/(\d+)', final_url)
#         if not video_id_match:
#             print(f"  -> ⚠️ 无法从最终URL中解析到 video_id。")
#             return None
#         video_id = video_id_match.group(1)
#
#         # 步骤 3: 访问最终页面，获取HTML内容
#         get_resp = await client.get(final_url, headers=headers, timeout=15)
#         get_resp.raise_for_status()
#         html_content = get_resp.text
#
#         # 步骤 4: 使用正则表达式快速从HTML中解析 og:image 标签内容
#         # 这种方法比完整的BeautifulSoup解析更快，对于目标明确的场景非常高效
#         og_image_match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html_content)
#
#         if og_image_match:
#             thumbnail_url = og_image_match.group(1)
#             print(f"  -> ✅ 成功抓取到封面图: {thumbnail_url}")
#             return {
#                 "video_id": video_id,
#                 "embed_url": final_url,
#                 "thumbnail_url": thumbnail_url
#             }
#         else:
#             print(f"  -> ⚠️ 页面HTML中未找到 og:image 标签。")
#             return {"video_id": video_id, "embed_url": final_url, "thumbnail_url": None}
#
#     except Exception as e:
#         print(f"  -> ❌ 解析抖音URL时发生错误: {e}")
#         return None


# --- 检索策略模块 (保持不变) ---
class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video']) -> List[Dict[str, Any]]: pass

    def _prefix_keys(self, result: Dict[str, Any], prefix: str) -> Dict[str, Any]: return {f"{prefix}_{key}": value for
                                                                                           key, value in result.items()}


class SearchApiIoProvider(SearchProvider):
    # 省略实现...
    def __init__(self):
        self.api_key = os.environ.get("SEARCHAPI_IO_API_KEY", "7MPC8616NewB263CoB1NMcgS")
        if not self.api_key: raise ValueError("SearchAPI.io API Key is not provided.")
        self.base_url = "https://www.searchapi.io/api/v1/search"
        self.prefix = "searchapi"

    # @staticmethod
    # def _extract_youtube_info(url: str) -> Dict[str, Optional[str]]:
    #     match = re.search(r"(?:v=|youtu\.be/|embed/)([a-zA-Z0-9_-]{11})", url)
    #     if not match: return {"video_id": None, "embed_url": None, "thumbnail_url": None}
    #     video_id = match.group(1)
    #     return {"video_id": video_id, "embed_url": f"https://www.youtube.com/embed/{video_id}",
    #             "thumbnail_url": f"https://img.youtube.com/vi/{video_id}/0.jpg"}
    #
    # def _extract_douyin_info(self, url: str) -> Dict[str, Optional[str]]:
    #     """
    #     从抖音的分享链接中提取视频ID，并返回固定的占位缩略图。
    #     """
    #     video_id = None
    #     # 正则表达式匹配抖音视频ID (通常是一长串数字)
    #     match = re.search(r'/video/(\d+)', url)
    #     if match:
    #         video_id = match.group(1)
    #     # 【调整】无论是否提取到 video_id，都返回固定的占位图URL
    #     return {
    #         "video_id": video_id,
    #         "embed_url": url,  # 直接使用原始URL作为嵌入链接
    #         "thumbnail_url": self.DEFAULT_DOUYIN_THUMBNAIL
    #     }

    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video'] = 'web') -> List[Dict[str, Any]]:
        try:
            params = {"q": query, "engine": "google", "gl": "cn", "hl": "zh-cn", "num": num_results,
                      "api_key": self.api_key}
            resp = await client.get(self.base_url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json().get("organic_results", [])

            results = []
            for item in data:
                if not item.get("link"): continue
                base_info = {"type": search_type, "url": item.get("link"), "title": item.get("title"),
                             "source": item.get("source", ""), "snippet": item.get("snippet", "")}
                if search_type == 'video': base_info.update(_parse_video_url(item.get("link")))
                results.append(self._prefix_keys(base_info, self.prefix))
            return results

            # tasks = []
            # original_items = []
            # # 【调整】对于视频搜索，我们先收集所有链接，然后并发地去抓取元数据
            # if search_type == 'video':
            #     for item in data:
            #         link = item.get("link")
            #         if link:
            #             # 创建并发任务
            #             tasks.append(_fetch_douyin_metadata_reliably(link, client))
            #             original_items.append(item)
            #
            #     # 并发执行所有抖音元数据抓取任务
            #     metadata_results = await asyncio.gather(*tasks)
            #     # 将抓取结果与原始搜索结果合并
            #     results = []
            #     for i, item in enumerate(original_items):
            #         metadata = metadata_results[i]
            #         base_info = {
            #             "type": search_type,
            #             "url": item.get("link"),
            #             "title": item.get("title"),
            #             "source": item.get("source", "抖音"),
            #             "snippet": item.get("snippet", "")
            #         }
            #         if metadata:
            #             base_info.update(metadata)
            #         else:
            #             # 如果抓取失败，则填充空值
            #             base_info.update({"video_id": None, "embed_url": item.get("link"), "thumbnail_url": None})
            #
            #         results.append(self._prefix_keys(base_info, self.prefix))
            #     return results
            # # 对于Web搜索，逻辑保持不变
            # else:
            #     results = []
            #     for item in data:
            #         if not item.get("link"): continue
            #         base_info = {
            #             "type": search_type,
            #             "url": item.get("link"),
            #             "title": item.get("title"),
            #             "source": item.get("source", ""),
            #             "snippet": item.get("snippet", "")
            #         }
            #         results.append(self._prefix_keys(base_info, self.prefix))
            #     return results

        except Exception as e:
            return [self._prefix_keys({"type": search_type, "error": f"SearchAPI.io request failed for '{query}': {e}"},
                                      self.prefix)]


class JinaSearchProvider(SearchProvider):
    def __init__(self):
        self.api_key = os.environ.get("JINA_API_KEY",
                                      "jina_b4348ffc39ca47bfbe753b95f59428c7i6ifkOFXRPdF3dRa5Rwb6T8FvrLH")
        if not self.api_key: raise ValueError("Jina.ai API Key is not provided.")
        self.base_url = "https://s.jina.ai/"
        # **关键修正 1: 更新请求头，与官方文档保持一致**
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Respond-With": "no-content"
        }
        self.prefix = "jina"

    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video'] = 'web') -> List[Dict[str, Any]]:
        # if search_type == 'video': return []
        try:
            # **关键修正 2: 更新请求体，加入本地化参数**
            data = {
                "q": query,
                "gl": "CN",
                "hl": "zh-cn"
            }
            resp = await client.post(self.base_url, headers=self.headers, json=data, timeout=30)  # 增加超时时间
            resp.raise_for_status()

            # **关键修正 3: 直接解析响应文本，避免潜在的编码问题**
            # Jina API 返回的可能是非标准JSON，直接用 .text
            # httpx 的 .json() 可能会严格检查 content-type
            api_response = json.loads(resp.text)

            api_results = api_response.get("data", [])
            results = []
            for item in api_results[:num_results]:
                if not item.get("url"): continue
                base_info = {"type": search_type, "url": item.get("url"), "title": item.get("title"),
                             "snippet": item.get("description"), "content": item.get("content", "")}
                if search_type == 'video': base_info.update(_parse_video_url(item.get("url")))
                results.append(self._prefix_keys(base_info, self.prefix))
            return results
        except Exception as e:
            return [
                self._prefix_keys({"type": search_type, "error": f"Jina.ai request failed for '{query}': {e}"},
                                  self.prefix)]


class FirecrawlSearchProvider(SearchProvider):
    # 省略实现...
    def __init__(self):
        self.api_key = os.environ.get("FIRECRAWL_API_KEY", "fc-a36b7d2fb273485680d0fe6abd686935")
        if not self.api_key: raise ValueError("Firecrawl API Key is not provided.")
        self.base_url = "https://api.firecrawl.dev/v2/search"
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        self.prefix = "firecrawl"

    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video'] = 'web') -> List[Dict[str, Any]]:
        # if search_type == 'video': return []
        try:
            data = {"query": query, "limit": num_results}
            resp = await client.post(self.base_url, headers=self.headers, json=data, timeout=30)
            resp.raise_for_status()
            api_results = resp.json().get("data", {}).get("web", [])
            results = []
            for item in api_results:
                if not item.get("url"): continue
                base_info = {"type": search_type, "url": item.get("url"), "title": item.get("title"),
                             "snippet": item.get("description"), "markdown": item.get("markdown", "")}
                if search_type == 'video': base_info.update(_parse_video_url(item.get("url")))
                results.append(self._prefix_keys(base_info, self.prefix))
            return results
        except Exception as e:
            return [self._prefix_keys({"type": search_type, "error": f"Firecrawl request failed for '{query}': {e}"},
                                      self.prefix)]


class TavilySearchProvider(SearchProvider):
    # 省略实现...
    def __init__(self):
        self.api_key = os.environ.get("TAVILY_API_KEY", "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")
        if not self.api_key: raise ValueError("Tavily API Key is not provided.")
        self.async_client = AsyncTavilyClient(self.api_key)
        self.prefix = "tavily"

    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video'] = 'web') -> List[Dict[str, Any]]:
        # if search_type == 'video': return []
        try:
            api_results = await self.async_client.search(query=query, search_depth="basic", max_results=num_results)
            results = []
            for item in api_results.get("results", []):
                if not item.get("url"): continue
                base_info = {"type": search_type, "url": item.get("url"), "title": item.get("title"),
                             "snippet": item.get("content"), "score": item.get("score")}
                if search_type == 'video': base_info.update(_parse_video_url(item.get("url")))
                results.append(self._prefix_keys(base_info, self.prefix))
            return results
        except Exception as e:
            return [
                self._prefix_keys({"type": search_type, "error": f"Tavily request failed for '{query}': {e}"},
                                  self.prefix)]


class ZhiLianJobProvider:
    """一个“虚拟”提供商，仅用于提取和传递职业数据。"""

    def get_data(self, career_query_input: Dict) -> Dict:
        print("💼 [ZhiLianJobProvider] 正在提取职业数据...")
        return career_query_input


class TianyanCheckProvider:
    def get_data(self, tianyan_input: Any) -> Any:
        print("🔭 [TianyanCheckProvider] 正在提取企业数据...")
        return tianyan_input


# --- 核心检索控制器 (保持不变) ---
# class MultiSourceSearcher:
#     # 省略实现...
#     def __init__(self):
#         self.providers: Dict[str, SearchProvider] = {"searchapi_io": SearchApiIoProvider(),
#                                                      "jina": JinaSearchProvider(),
#                                                      "firecrawl": FirecrawlSearchProvider(),
#                                                      "tavily": TavilySearchProvider()}
#
#     async def _search_single_provider(self, queries, provider_name, client, web_count, video_count):
#         provider = self.providers[provider_name]
#         tasks = []
#         for q in queries:
#             if web_count > 0: tasks.append(provider.search(q, client, web_count, 'web'))
#             if video_count > 0: tasks.append(provider.search(q, client, video_count, 'video'))
#         task_results = await asyncio.gather(*tasks, return_exceptions=True)
#         urls_info, task_idx = [], 0
#         for query in queries:
#             res = {"query": query, "web_results": [], "video_results": [], "errors": []}
#             for stype, count, key in [('web', web_count, 'web_results'), ('video', video_count, 'video_results')]:
#                 if count > 0:
#                     result = task_results[task_idx]
#                     task_idx += 1
#                     if isinstance(result, Exception):
#                         res["errors"].append(f"[{provider.prefix}] {stype.capitalize()} search task failed: {result}")
#                     elif result and result[0].get(f'{provider.prefix}_error'):
#                         res["errors"].append(result[0][f'{provider.prefix}_error'])
#                     else:
#                         res[key] = result
#             urls_info.append(res)
#         return urls_info
#
#     async def _search_all_providers(self, queries, client, web_count, video_count):
#         async def search_and_tag(p_name, provider, query, num, stype):
#             try:
#                 data = await provider.search(query, client, num, stype)
#                 return {"query": query, "provider": p_name, "type": stype, "data": data, "exception": None}
#             except Exception as e:
#                 return {"query": query, "provider": p_name, "type": stype, "data": [], "exception": e}
#
#         tasks = []
#         for q in queries:
#             for name, provider in self.providers.items():
#                 if web_count > 0: tasks.append(search_and_tag(name, provider, q, web_count, 'web'))
#                 if video_count > 0: tasks.append(search_and_tag(name, provider, q, video_count, 'video'))
#         task_results = await asyncio.gather(*tasks)
#         agg = {q: {"query": q, "web_results": [], "video_results": [], "errors": []} for q in queries}
#         for res in task_results:
#             query, p_name, stype, data, exc = res["query"], res["provider"], res["type"], res["data"], res["exception"]
#             provider_prefix = self.providers[p_name].prefix
#             if exc:
#                 agg[query]["errors"].append(
#                     f"[{provider_prefix}] {stype.capitalize()} search task threw an exception: {exc}")
#                 continue
#             if data and data[0].get(f'{provider_prefix}_error'):
#                 agg[query]["errors"].append(data[0][f'{provider_prefix}_error'])
#             elif stype == 'web':
#                 agg[query]["web_results"].extend(data)
#             elif stype == 'video':
#                 agg[query]["video_results"].extend(data)
#         return list(agg.values())
#
#     async def search(self, queries, provider_name, web_count, video_count):
#         async with httpx.AsyncClient() as client:
#             if provider_name.lower() == "all":
#                 return await self._search_all_providers(queries, client, web_count, video_count)
#             else:
#                 if provider_name.lower() not in self.providers: raise ValueError(
#                     f"Provider '{provider_name}' not found.")
#                 return await self._search_single_provider(queries, provider_name.lower(), client, web_count,
#                                                           video_count)
#
#
# # --- Dify 异步主函数 ---
# async def main_async(raw_input: Any, provider_name: str, web_results_count: int, video_results_count: int) -> Dict[
#     str, Any]:
#     # 这个函数现在只处理成功路径，所有异常由调用者 main 捕获
#     parsed_input = _intelligent_input_parser(raw_input)
#     queries = parsed_input["queries"]
#     searcher = MultiSourceSearcher()
#     urls_info = await searcher.search(
#         queries,
#         provider_name=provider_name,
#         web_count=web_results_count,
#         video_count=video_results_count
#     )
#     return {
#         "urls_info": urls_info,
#         "urls_info_str": json.dumps(urls_info, ensure_ascii=False, indent=2)
#     }
#
#
# # --- Dify 同步入口 (已彻底重构错误处理和输入验证) ---
# def main(
#         raw_input: Any,
#         provider: str = "tavily",
#         web_results: Any = 3,
#         video_results: Any = 0
# ) -> Dict[str, Any]:
#     """
#     Dify同步入口。任何情况下都返回 {"urls_info": [], "urls_info_str": ""} 结构。
#     """
#     try:
#         # **关键修正 1: 健壮的输入处理**
#         # Dify 可能传入 None 或 ""，在这里将其转换为有效的整数
#         try:
#             web_count = int(web_results) if web_results not in [None, ""] else 3
#         except (ValueError, TypeError):
#             web_count = 3  # 如果传入无法转换的字符串，则使用默认值
#
#         try:
#             video_count = int(video_results) if video_results not in [None, ""] else 0
#         except (ValueError, TypeError):
#             video_count = 0  # 如果传入无法转换的字符串，则使用默认值
#
#         # 运行核心异步逻辑
#         return asyncio.run(main_async(
#             raw_input=raw_input,
#             provider_name=provider,
#             web_results_count=web_count,
#             video_results_count=video_count
#         ))
#
#     except Exception as e:
#         # **关键修正 2: 统一的错误输出结构**
#         # 捕获所有异常，并将其打包到符合输出变量定义的字典中
#         error_message = f"An error occurred in the node: {str(e)}"
#         # 创建一个包含错误信息的 payload，其结构与正常输出的 urls_info 一致
#         error_payload = [
#             {
#                 "query": "NODE_EXECUTION_ERROR",
#                 "web_results": [],
#                 "video_results": [],
#                 "errors": [error_message, traceback.format_exc()]  # 包含简短和详细的错误
#             }
#         ]
#
#         # 返回与成功时完全相同的 key
#         return {
#             "urls_info": error_payload,
#             "urls_info_str": json.dumps(error_payload, ensure_ascii=False, indent=2)
#         }
class MultiSourceSearcher:
    def __init__(self):
        # 【调整】仅从类引用映射开始，初始化时不创建任何实例
        # 1. Web 搜索类映射
        self.web_provider_classes: Dict[str, Any] = {
            "searchapi_io": SearchApiIoProvider,
            "jina": JinaSearchProvider,
            "firecrawl": FirecrawlSearchProvider,
            "tavily": TavilySearchProvider
        }

        # 【调整】2. 增加：非Web (辅助) 服务的懒加载映射
        # 这样可以将 ZhiLian 和 Tianyan 也纳入懒加载管理
        self.auxiliary_provider_classes: Dict[str, Any] = {
            "zhilian_job": ZhiLianJobProvider,
            "tianyan_check_enterprises": TianyanCheckProvider
        }
        # 【调整】3. 统一的实例缓存池
        self.active_instances: Dict[str, Any] = {}

    def get_web_provider_names(self) -> List[str]:
        """获取支持的 web provider 名称列表"""
        return list(self.web_provider_classes.keys())

    def _get_provider_instance(self, p_name: str) -> Any:
        # 【调整】通用懒加载工厂方法
        # 如果实例已存在缓存中，直接返回
        if p_name in self.active_instances:
            return self.active_instances[p_name]
        # 检查是否是 Web Provider
        if p_name in self.web_provider_classes:
            print(f"🔌 [System] Initializing WEB provider: {p_name}...")
            instance = self.web_provider_classes[p_name]()
            self.active_instances[p_name] = instance
            return instance

        # 检查是否是 辅助 Provider
        elif p_name in self.auxiliary_provider_classes:
            print(f"🔌 [System] Initializing AUX provider: {p_name}...")
            instance = self.auxiliary_provider_classes[p_name]()
            self.active_instances[p_name] = instance
            return instance

        else:
            raise ValueError(f"Provider '{p_name}' not supported.")

    # 暴露给外部调用的特定 getter，确保类型安全和懒加载触发
    def get_zhilian_provider(self) -> ZhiLianJobProvider:
        return self._get_provider_instance("zhilian_job")

    def get_tianyan_provider(self) -> TianyanCheckProvider:
        return self._get_provider_instance("tianyan_check_enterprises")

    async def web_search(self, queries: List[str], providers_to_use: List[str], client: httpx.AsyncClient,
                         search_types: List[str], web_results_per_type: int, video_results_count: int,
                         regional_data: Optional[Dict[str, str]] = None,
                         time_filter: Optional[Dict[str, str]] = None
                         ) -> List[Dict[str, Any]]:
        # ... [这里的轮询调度逻辑保持不变] ...
        async def search_and_tag(p_name: str, original_query: str, num: int, stype: str, is_regional: bool):
            provider = self._get_provider_instance(p_name)
            filtered_query = _build_filtered_query(original_query, stype, regional_data=regional_data,
                                                   use_regional_patterns=is_regional,
                                                   time_filter=time_filter)
            # 打不同的日志 tag 方便区分
            log_tag = "Regional" if is_regional else "Standard"
            print(
                f"  -> [Task Scheduled] Provider: {p_name}, [Task: {stype} | {log_tag}], Results: {num}, Query: \"{filtered_query}\"")
            try:
                data = await provider.search(filtered_query, client, num, stype)
                # 在返回结果中明确标记原始查询和搜索类型
                return {"original_query": original_query, "search_type": stype, "provider": p_name, "data": data}
            except Exception as e:
                error_data = [
                    provider._prefix_keys({"type": stype, "error": f"Task failed for '{original_query}': {e}"},
                                          provider.prefix)]
                return {"original_query": original_query, "search_type": stype, "provider": p_name, "data": error_data}

        provider_cycle = cycle(providers_to_use)
        tasks = []
        # 为每个 query 和每个 search_type 创建任务
        for query in queries:
            for stype in search_types:
                assigned_provider = next(provider_cycle)

                # 【解耦】&【优化】逻辑
                num_results_for_task = 0
                if stype == 'video':
                    num_results_for_task = video_results_count
                else:  # 'web', 'industry_reports', etc.
                    num_results_for_task = web_results_per_type

                # 【优化】如果请求的结果数为0，则直接跳过，不创建任务
                if num_results_for_task <= 0:
                    print(f"  -> [Task Skipped] Type: {stype} requested 0 results for query: \"{query}\"")
                    continue

                strategy_config = SEARCH_STRATEGY_CONFIG.get(stype, {})

                # --- 任务 A: 标准检索 (永远执行) ---
                tasks.append(search_and_tag(
                    assigned_provider,
                    query,
                    num_results_for_task,
                    stype,
                    is_regional=False
                ))
                # --- 任务 B: 区域性检索 (条件触发) ---
                # 只有当：不是视频搜索 + 提供了区域数据 + 该类型配置了 regional_patterns 时才执行
                if (stype != 'video' and
                        regional_data and
                        "regional_patterns" in strategy_config):
                    print(f"  -> [System] Detected regional data for {stype}, spawning extra regional task.")
                    tasks.append(search_and_tag(
                        assigned_provider,
                        query,
                        num_results_for_task,
                        stype,
                        is_regional=True
                    ))

        if not tasks: return []  # 如果没有任务，直接返回
        task_results = await asyncio.gather(*tasks)

        # 聚合结果
        # 新的聚合结构: { "query_string": { "type_A_results": [], "type_B_results": [], "errors": [] } }
        agg_by_query = {q: {"query": q, "errors": []} for q in queries}
        for res in task_results:
            original_query = res["original_query"]
            stype = res["search_type"]
            p_name = res["provider"]
            data = res["data"]

            provider_instance = self._get_provider_instance(p_name)
            provider_prefix = getattr(provider_instance, 'prefix', p_name)

            # 初始化该类型的结果列表 (例如: "industry_reports_results")
            results_key = f"{stype}_results"
            if results_key not in agg_by_query[original_query]:
                agg_by_query[original_query][results_key] = []
            for item in data:
                if item.get(f'{provider_prefix}_error'):
                    agg_by_query[original_query]["errors"].append(item[f'{provider_prefix}_error'])
                else:
                    agg_by_query[original_query][results_key].append(item)

        return list(agg_by_query.values())


# --- 5. Dify 异步主函数 (总指挥) ---
EXCLUSIVE_SEARCH_RESULTS_COUNT = 10


async def _process_single_item(
        raw_item_input: Any,
        searcher: MultiSourceSearcher,
        client: httpx.AsyncClient,
        web_search_providers_to_use: List[str],
        effective_search_types: List[str],
        is_exclusive_requested: bool,
        exclusive_queries: List[str],
        web_results_per_type: int,
        video_results_count: int,
        is_zhilian_requested: bool,
        is_tianyan_requested: bool,
        regional_data: Optional[Dict[str, str]] = None,
        time_filter: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """
    处理单个输入项的搜索任务
    """
    # 1. 解析输入
    parsed_data = _intelligent_input_parser(raw_item_input)
    comprehensive_queries = parsed_data["comprehensive_queries"]
    career_query_data = parsed_data["career_query_data"]
    tianyan_enterprise_names = parsed_data["tianyan_enterprise_names"]

    # 2. 初始化结果容器
    comprehensive_results = []
    career_results = {}
    tianyan_results: List[str] = []

    # 3. 执行Web搜索
    if web_search_providers_to_use:
        async_tasks = []
        # 3.1 创建普通查询任务
        if comprehensive_queries:
            normal_task = searcher.web_search(
                queries=comprehensive_queries,
                providers_to_use=web_search_providers_to_use,
                client=client,
                search_types=effective_search_types,
                web_results_per_type=web_results_per_type,
                video_results_count=video_results_count,
                regional_data=regional_data,
                time_filter=time_filter
            )
            async_tasks.append(normal_task)

        # 3.2 创建专属查询任务
        if is_exclusive_requested and exclusive_queries:
            exclusive_task = searcher.web_search(
                queries=exclusive_queries,
                providers_to_use=web_search_providers_to_use,
                client=client,
                search_types=["exclusive_rules"],
                web_results_per_type=EXCLUSIVE_SEARCH_RESULTS_COUNT,
                video_results_count=0,
                time_filter=time_filter
            )
            async_tasks.append(exclusive_task)

        # 3.3 并发执行
        if async_tasks:
            all_results_groups = await asyncio.gather(*async_tasks)
            for result_group in all_results_groups:
                comprehensive_results.extend(result_group)

    # 4. 执行ZhiLian数据提取
    if is_zhilian_requested:
        career_results = searcher.get_zhilian_provider().get_data(career_query_data)

    # 5. 执行Tianyan数据提取
    if is_tianyan_requested:
        tianyan_results = searcher.get_tianyan_provider().get_data(tianyan_enterprise_names)

    return {
        "comprehensive_data": comprehensive_results,
        "career_data": career_results,
        "tianyan_check_data": tianyan_results
    }


async def main_async(raw_input: Any, provider_selection: Union[str, List[str]], search_types: List[str],
                     web_results_per_type: int, video_results_count: int,
                     regional_data: Optional[Dict[str, str]] = None,
                     time_filter_input: Any = None) -> Dict[str, Any]:
    # 1. 确定输入列表
    items_to_process = []
    if isinstance(raw_input, list):
        items_to_process = raw_input
    elif isinstance(raw_input, str):
        try:
            parsed = json_repair.loads(raw_input)
            if isinstance(parsed, list):
                items_to_process = parsed
            else:
                items_to_process = [parsed]
        except:
            items_to_process = [raw_input]  # 可能是普通字符串或无法解析的字符串，作为单项处理
    else:
        items_to_process = [raw_input]

    if not items_to_process:
        return {"datas": [], "datas_str": "[]"}

    # 1.1 解析时间过滤
    time_filter = _parse_time_filter(time_filter_input)
    if time_filter:
        print(f"[Time Filter] Applied: {time_filter}")

    # 2. 预处理公共配置 (Provider, Search Types, Rules)
    # 这些配置对所有 items 都是通用的

    # 2.1 处理专属规则
    exclusive_queries = []
    is_exclusive_requested = "exclusive_rules" in search_types
    effective_search_types = [t for t in search_types if t != "exclusive_rules"]
    if not effective_search_types: effective_search_types = ["web"]

    if is_exclusive_requested:
        exclusive_queries = _generate_exclusive_queries(regional_data)

    has_web_work = True  # 简化逻辑，总是有可能需要web search，具体看query是否为空

    # 2.2 处理 Provider 选择
    selected_providers = []
    web_search_providers_to_use = []

    if isinstance(provider_selection, str):
        selected_providers = [p.strip().lower() for p in provider_selection.split(',')]
    elif isinstance(provider_selection, list):
        selected_providers = [str(p).lower() for p in provider_selection]

    is_zhilian_requested = "zhilian_job" in selected_providers
    is_tianyan_requested = "tianyan_check_enterprises" in selected_providers

    searcher = MultiSourceSearcher()
    all_web_provider_names = searcher.get_web_provider_names()

    if "all" in selected_providers:
        web_search_providers_to_use = all_web_provider_names
    else:
        web_search_providers_to_use = [p for p in selected_providers if p in all_web_provider_names]

    # 3. 并发处理所有 Items
    print(f"🚀 [Main] Starting batch processing for {len(items_to_process)} items...")

    # 限制并发数为 10，避免触发 API Rate Limit
    semaphore = asyncio.Semaphore(10)

    async def sem_process_item(item, client):
        async with semaphore:
            return await _process_single_item(
                raw_item_input=item,
                searcher=searcher,
                client=client,
                web_search_providers_to_use=web_search_providers_to_use,
                effective_search_types=effective_search_types,
                is_exclusive_requested=is_exclusive_requested,
                exclusive_queries=exclusive_queries,
                web_results_per_type=web_results_per_type,
                video_results_count=video_results_count,
                is_zhilian_requested=is_zhilian_requested,
                is_tianyan_requested=is_tianyan_requested,
                regional_data=regional_data,
                time_filter=time_filter
            )

    # http2=True 需要安装 h2 库，如果没装会报错。为保险起见，这里先关掉 http2，或者改为 try-except 自动降级
    # async with httpx.AsyncClient(http2=True, verify=False) as client:
    async with httpx.AsyncClient(http2=False, verify=False) as client:
        tasks = []
        for item in items_to_process:
            tasks.append(sem_process_item(item, client))

        results = await asyncio.gather(*tasks)

    # 4. 返回结果列表
    final_output = {
        "datas": results
    }

    return {
        "datas": final_output["datas"],
        "datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
    }


# --- Dify 同步入口 ---
# 【调整】重构 main 函数以适应新的异步逻辑和更复杂的 provider 输入
def main(
        raw_input: Any,
        provider: Union[str, List[str]] = "tavily",
        search_types: Union[str, List[str]] = "web",
        web_results_per_type: Any = 3,
        video_results_count: Any = 2,
        regional_rules: Any = None,
        time_filter: Any = None
) -> Dict[str, Any]:
    # 定义一个标准的空/错误返回结构
    error_datas_structure = {"comprehensive_data": [], "career_data": {}, "tianyan_check_data": []}

    def construct_error_payload(e, trace):
        error_message = f"An error occurred in the node: {str(e)}"
        error_datas_structure["comprehensive_data"] = [
            {"query": "NODE_EXECUTION_ERROR", "web_results": [], "video_results": [], "errors": [error_message, trace]}
        ]
        return {
            "datas": error_datas_structure,
            "datas_str": json.dumps({"datas": error_datas_structure}, ensure_ascii=False, indent=2)
        }

    try:
        # 1. 健壮地处理 provider 输入
        provider_selection = provider
        # if isinstance(provider, str) and provider.strip().startswith('[') and provider.strip().endswith(']'):
        #     try:
        #         # 尝试将字符串形式的列表解析为真实的 Python 列表
        #         provider_selection = json.loads(provider)
        #     except json.JSONDecodeError:
        #         raise ValueError(f"Provider input '{provider}' looks like a list but is not valid JSON.")
        if isinstance(provider, str):
            cleaned_provider = provider.strip()

            # --- 检测是否是列表格式 "[...]" ---
            if cleaned_provider.startswith('[') and cleaned_provider.endswith(']'):
                try:
                    # 尝试1: 标准 JSON 解析 (要求双引号)
                    provider_selection = json.loads(cleaned_provider)
                except json.JSONDecodeError:
                    try:
                        # 尝试2: 使用 json_repair (可以自动把单引号修成双引号，完美解决您的问题)
                        provider_selection = json_repair.loads(cleaned_provider)
                    except Exception:
                        # 尝试3: 最后的暴力兜底 (手动去除括号和引号)
                        # 逻辑：去掉首尾括号 -> 按逗号分割 -> 去掉每一项周围的空格和单/双引号
                        inner_content = cleaned_provider[1:-1]
                        provider_selection = [
                            item.strip().strip("'").strip('"')
                            for item in inner_content.split(',')
                            if item.strip()
                        ]

            # --- 检测是否是逗号分隔字符串 "a, b" (非列表格式) ---
            elif ',' in cleaned_provider:
                provider_selection = [p.strip() for p in cleaned_provider.split(',') if p.strip()]

        # 2. 【新增】健壮地处理 search_types 输入
        search_types_list = []
        if isinstance(search_types, str):
            try:
                # 尝试解析 JSON 字符串 (e.g., '["web", "industry_reports"]')
                parsed_list = json.loads(search_types)
                if isinstance(parsed_list, list):
                    search_types_list = parsed_list
                else:
                    raise ValueError("Input is not a list.")
            except (json.JSONDecodeError, ValueError):
                # 如果失败，则按逗号分割 (e.g., "web,video")
                search_types_list = [t.strip() for t in search_types.split(',') if t.strip()]
        elif isinstance(search_types, list):
            search_types_list = search_types

        # 3. 健壮地处理 regional_rules 输入
        regional_data_dict = {}
        if isinstance(regional_rules, dict):
            regional_data_dict = regional_rules
        elif isinstance(regional_rules, str) and regional_rules.strip():
            try:
                # 尝试解析JSON字符串
                parsed_data = json_repair.loads(regional_rules)
                if isinstance(parsed_data, dict):
                    regional_data_dict = parsed_data
            except Exception as e:
                print(f"  -> [Warning] Failed to parse regional_rules as JSON: {e}. It will be ignored.")

        # 4. 健壮地处理数字输入
        try:
            web_count = int(web_results_per_type) if web_results_per_type not in [None, ""] else 3
        except (ValueError, TypeError):
            web_count = 3
        try:
            video_count = int(video_results_count) if video_results_count not in [None, ""] else 2
        except (ValueError, TypeError):
            video_count = 2

        # 4. 运行核心异步逻辑
        res = asyncio.run(main_async(
            raw_input=raw_input,
            provider_selection=provider_selection,
            search_types=search_types_list,
            web_results_per_type=web_count,
            video_results_count=video_count,
            regional_data=regional_data_dict,
            time_filter_input=time_filter
        ))

        return _dify_debug_return(res, label='Success')
    except Exception as e:
        trace = traceback.format_exc()
        print(f"‼️ 节点执行时发生顶层错误: {e}\n{trace}")
        error_payload = construct_error_payload(e, trace)
        return _dify_debug_return(error_payload, label='Exception')


# main({
#     "component_id": "comp_001",
#     "web_queries": {
#         "comprehensive_query": ["保育员"],
#         "career_query": {},
#         "tianyan_check_enterprise": ""
#     }
# }, "['searchapi_io']", [
#     "web"
# ], web_results_per_type="3", time_filter="7"

# )

# async def main_async(raw_input: Any, provider_selection: Union[str, List[str]], web_results_count: int,
#                      video_results_count: int) -> Dict[str, Any]:
#     # 1. 解析输入
#     parsed_data = _intelligent_input_parser(raw_input)
#     comprehensive_queries = parsed_data["comprehensive_queries"]
#     career_query_data = parsed_data["career_query_data"]
#     tianyan_enterprise_name = parsed_data["tianyan_enterprise_name"]  # 【调整】获取新数据
#
#     # 2. 初始化结果容器
#     comprehensive_results = []
#     career_results = {}
#     tianyan_results = ""  # 【调整】初始化新结果容器
#
#     # 3. 解析和分派任务
#     searcher = MultiSourceSearcher()
#     all_web_provider_names = list(searcher.web_providers.keys())
#
#     selected_providers = []
#     if isinstance(provider_selection, str):
#         if provider_selection.lower() == "all":
#             selected_providers = all_web_provider_names
#         else:
#             selected_providers = [provider_selection.lower()]
#     elif isinstance(provider_selection, list):
#         selected_providers = [str(p).lower() for p in provider_selection]
#     # 【调整】任务分派逻辑
#     web_search_providers_to_use = [p for p in selected_providers if p in all_web_provider_names]
#     is_zhilian_requested = "zhilian_job" in selected_providers
#     is_tianyan_requested = "tianyan_check_enterprises" in selected_providers  # 【新增】
#     # 3.2 执行Web搜索
#     if web_search_providers_to_use and comprehensive_queries:
#         print(f"🌐 [Web Search] 使用 {web_search_providers_to_use} 搜索 {len(comprehensive_queries)} 个查询...")
#         async with httpx.AsyncClient() as client:
#             comprehensive_results = await searcher.web_search(queries=comprehensive_queries,
#                                                               providers_to_use=web_search_providers_to_use,
#                                                               client=client, web_count=web_results_count,
#                                                               video_count=video_results_count)
#     else:
#         print("🟡 [Web Search] 无需执行Web搜索。")
#     # 3.3 执行ZhiLian数据提取
#     if is_zhilian_requested:
#         career_results = searcher.zhilian_provider.get_data(career_query_data)
#
#     # 【新增】3.4 执行Tianyan数据提取
#     if is_tianyan_requested:
#         tianyan_results = searcher.tianyan_provider.get_data(tianyan_enterprise_name)
#     # 4. 【调整】组装最终输出
#     final_output = {
#         "datas": {
#             "comprehensive_data": comprehensive_results,
#             "career_data": career_results,
#             "tianyan_check_data": tianyan_results  # 【新增】
#         }
#     }
#     return {
#         "datas": final_output["datas"],  # 【调整】直接返回datas对象
#         "datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
#     }
#
#
# # --- Dify 同步入口 ---
# def main(raw_input: Any, provider: Union[str, List[str]] = "tavily", web_results: Any = 3, video_results: Any = 0) -> \
# Dict[str, Any]:
#     # 【调整】统一的错误输出结构
#     error_datas_structure = {"comprehensive_data": [], "career_data": {}, "tianyan_check_data": ""}
#     error_payload = {
#         "datas": error_datas_structure,
#         "datas_str": json.dumps({"datas": error_datas_structure}, ensure_ascii=False, indent=2)
#     }
#     try:
#         provider_selection = provider
#         if isinstance(provider, str) and provider.strip().startswith('[') and provider.strip().endswith(']'):
#             try:
#                 provider_selection = json.loads(provider)
#             except json.JSONDecodeError:
#                 raise ValueError(f"Provider input '{provider}' looks like a list but is not valid JSON.")
#
#         try:
#             web_count = int(web_results) if web_results not in [None, ""] else 3
#         except (ValueError, TypeError):
#             web_count = 3
#         try:
#             video_count = int(video_results) if video_results not in [None, ""] else 0
#         except (ValueError, TypeError):
#             video_count = 0
#
#         res = asyncio.run(
#             main_async(raw_input=raw_input, provider_selection=provider_selection, web_results_count=web_count,
#                        video_results_count=video_count))
#         return _dify_debug_return(res, label='Success')
#
#     except Exception as e:
#         error_message = f"An error occurred in the node: {str(e)}"
#         trace = traceback.format_exc()
#
#         # 【调整】将错误信息放入 comprehensive_data 中
#         error_datas_structure["comprehensive_data"] = [
#             {"query": "NODE_EXECUTION_ERROR", "web_results": [], "video_results": [], "errors": [error_message, trace]}
#         ]
#         error_payload["datas"] = error_datas_structure
#         error_payload["datas_str"] = json.dumps({"datas": error_datas_structure}, ensure_ascii=False, indent=2)
#         print(f"‼️ 节点执行时发生顶层错误: {e}\n{trace}")
#         return _dify_debug_return(error_payload, label='Exception')

# class MultiSourceSearcher:
#     def __init__(self):
#         self.providers: Dict[str, SearchProvider] = {"searchapi_io": SearchApiIoProvider(),
#                                                      "jina": JinaSearchProvider(),
#                                                      "firecrawl": FirecrawlSearchProvider(),
#                                                      "tavily": TavilySearchProvider()}
#
#     async def _search_with_providers(self, queries: List[str], providers_to_use: List[str], client: httpx.AsyncClient,
#                                      web_count: int, video_count: int) -> List[Dict[str, Any]]:
#         async def search_and_tag(p_name: str, query: str, num: int, stype: str):
#             provider = self.providers[p_name]
#             try:
#                 data = await provider.search(query, client, num, stype)
#                 return {"query": query, "provider": p_name, "type": stype, "data": data}
#             except Exception as e:
#                 error_data = [
#                     provider._prefix_keys({"type": stype, "error": f"Task execution failed for '{query}': {e}"},
#                                           provider.prefix)]
#                 return {"query": query, "provider": p_name, "type": stype, "data": error_data}
#
#         provider_cycle = cycle(providers_to_use)
#         tasks_by_query = {q: [] for q in queries}
#         for query in queries:
#             assigned_provider = next(provider_cycle)
#             if web_count > 0:
#                 tasks_by_query[query].append(search_and_tag(assigned_provider, query, web_count, 'web'))
#             if video_count > 0 and assigned_provider == "searchapi_io":  # Only searchapi supports video
#                 tasks_by_query[query].append(search_and_tag(assigned_provider, query, video_count, 'video'))
#         all_tasks = [task for task_list in tasks_by_query.values() for task in task_list]
#         task_results = await asyncio.gather(*all_tasks)
#         # 聚合结果
#         agg = {q: {"query": q, "web_results": [], "video_results": [], "errors": []} for q in queries}
#         for res in task_results:
#             query, p_name, stype, data = res["query"], res["provider"], res["type"], res["data"]
#             provider_prefix = self.providers[p_name].prefix
#
#             clean_data = []
#             for item in data:
#                 if item.get(f'{provider_prefix}_error'):
#                     agg[query]["errors"].append(item[f'{provider_prefix}_error'])
#                 else:
#                     clean_data.append(item)
#
#             if stype == 'web':
#                 agg[query]["web_results"].extend(clean_data)
#             elif stype == 'video':
#                 agg[query]["video_results"].extend(clean_data)
#
#         return list(agg.values())
#
#     async def search(self, queries: List[str], provider_selection: Union[str, List[str]], web_count: int,
#                      video_count: int) -> List[Dict[str, Any]]:
#         providers_to_use = []
#         if isinstance(provider_selection, str):
#             if provider_selection.lower() == "all":
#                 providers_to_use = list(self.providers.keys())
#             else:
#                 if provider_selection.lower() not in self.providers: raise ValueError(
#                     f"Provider '{provider_selection}' not found.")
#                 providers_to_use = [provider_selection.lower()]
#         elif isinstance(provider_selection, list):
#             providers_to_use = [p.lower() for p in provider_selection if p.lower() in self.providers]
#             if not providers_to_use: raise ValueError("None of the specified providers are valid.")
#
#         if not providers_to_use: raise ValueError("No valid providers selected for search.")
#         async with httpx.AsyncClient() as client:
#             return await self._search_with_providers(queries, providers_to_use, client, web_count, video_count)
#
#
# # --- Dify 异步主函数 ---
# async def main_async(raw_input: Any, provider_selection: Union[str, List[str]], web_results_count: int,
#                      video_results_count: int) -> Dict[str, Any]:
#     parsed_input = _intelligent_input_parser(raw_input)
#     queries = parsed_input["queries"]
#     searcher = MultiSourceSearcher()
#     urls_info = await searcher.search(queries, provider_selection=provider_selection, web_count=web_results_count,
#                                       video_count=video_results_count)
#     return {"urls_info": urls_info, "urls_info_str": json.dumps(urls_info, ensure_ascii=False, indent=2)}
#
#
# # --- Dify 同步入口 ---
# def main(raw_input: Any, provider: Union[str, List[str]] = "tavily", web_results: Any = 3, video_results: Any = 0) -> \
#         Dict[str, Any]:
#     try:
#         provider_selection = provider
#         if isinstance(provider, str) and provider.strip().startswith('[') and provider.strip().endswith(']'):
#             try:
#                 provider_selection = json.loads(provider)
#             except json.JSONDecodeError:
#                 raise ValueError(f"Provider input '{provider}' looks like a list but is not valid JSON.")
#         try:
#             web_count = int(web_results) if web_results not in [None, ""] else 3
#         except (ValueError, TypeError):
#             web_count = 3
#         try:
#             video_count = int(video_results) if video_results not in [None, ""] else 0
#         except (ValueError, TypeError):
#             video_count = 0
#         res = asyncio.run(
#             main_async(raw_input=raw_input, provider_selection=provider_selection, web_results_count=web_count,
#                        video_results_count=video_count))
#         return _dify_debug_return(res)
#     except Exception as e:
#         error_payload = [{"query": "NODE_EXECUTION_ERROR", "web_results": [], "video_results": [],
#                           "errors": [f"An error occurred in the node: {str(e)}", traceback.format_exc()]}]
#         err = {"urls_info": error_payload, "urls_info_str": json.dumps(error_payload, ensure_ascii=False, indent=2)}
#         return _dify_debug_return(err, label='exc')
