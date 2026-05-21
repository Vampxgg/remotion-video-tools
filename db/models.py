# -*- coding: utf-8 -*-
# @File：models.py
# @Time：2025/10/13 11:32
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
# db/models.py

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    func,
    UniqueConstraint,
    Index,
    ARRAY,
)
from sqlalchemy.dialects.postgresql import JSONB, TEXT
from .database import Base
from datetime import datetime


class VoiceModel(Base):
    __tablename__ = 'voice_models'

    # --- 核心字段 ---
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, comment="模型的显示名称，例如 '蔡徐坤'")
    handle = Column(String, nullable=False, comment="模型的唯一句柄/ID，例如 'xiaotang911991'")
    description = Column(TEXT, nullable=True, comment="模型的描述或试听语音的文本")

    # --- 关联资源URL ---
    avatar_url = Column(String, nullable=True, comment="头像图片的URL")
    preview_audio_url = Column(String, nullable=True, comment="试听语音文件的URL")

    # --- 分类与元数据 ---
    provider = Column(String, default="Official", comment="声音来源或提供方")
    language = Column(String, nullable=True, comment="主要语言")
    gender = Column(String, nullable=True, comment="性别")
    tags = Column(ARRAY(String), nullable=True, comment="标签数组，用于搜索和分类")

    # --- 统计数据 ---
    play_count = Column(Integer, default=0, nullable=False, comment="播放/试听次数")
    share_count = Column(Integer, default=0, nullable=False, comment="分享次数")
    like_count = Column(Integer, default=0, nullable=False, comment="点赞/喜欢次数")
    save_count = Column(Integer, default=0, nullable=False, comment="收藏次数")
    usage_count = Column(Integer, default=0, nullable=False, comment="使用次数")

    # --- 状态与时间戳 ---
    is_public = Column(Boolean, default=True, nullable=False, comment="是否公开可见")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="最后更新时间")

    # --- 约束和索引 ---
    __table_args__ = (
        UniqueConstraint('name', name='uq_voice_models_name'),
        UniqueConstraint('handle', name='uq_voice_models_handle'),
        Index('ix_voice_models_tags', 'tags', postgresql_using='gin'),  # 为tags数组创建GIN索引以加速查询
    )


class TianyanchaCompany(Base):
    """天眼查企业主表：搜索摘要和详情统一去重后落到这里。"""

    __tablename__ = "tianyancha_companies"

    id = Column(Integer, primary_key=True, index=True)
    tianyancha_id = Column(BigInteger, nullable=True, comment="天眼查企业ID")
    name = Column(String(255), nullable=False, comment="企业名称")
    normalized_name = Column(String(255), nullable=False, comment="规范化企业名称")
    credit_code = Column(String(255), nullable=True, comment="统一社会信用代码")
    reg_number = Column(String(50), nullable=True, comment="注册号")
    org_number = Column(String(50), nullable=True, comment="组织机构代码")
    tax_number = Column(String(255), nullable=True, comment="纳税人识别号")

    reg_status = Column(String(31), nullable=True, comment="经营状态")
    reg_capital = Column(String(50), nullable=True, comment="注册资本")
    actual_capital = Column(String(50), nullable=True, comment="实收资本")
    legal_person_name = Column(String(255), nullable=True, comment="法定代表人")
    company_type = Column(Integer, nullable=True, comment="机构类型")
    company_org_type = Column(String(127), nullable=True, comment="企业类型")
    legal_type = Column(Integer, nullable=True, comment="法人类型")

    base = Column(String(31), nullable=True, comment="省份简称或省份")
    city = Column(String(50), nullable=True, comment="城市")
    district = Column(String(50), nullable=True, comment="区县")
    district_code = Column(String(20), nullable=True, comment="行政区划代码")
    industry = Column(String(255), nullable=True, comment="行业")
    category = Column(String(255), nullable=True, comment="国民经济行业门类")
    category_code_first = Column(String(32), nullable=True, comment="门类代码")
    category_code_second = Column(String(32), nullable=True, comment="大类代码")
    category_code_third = Column(String(32), nullable=True, comment="中类代码")
    category_code_fourth = Column(String(32), nullable=True, comment="小类代码")

    established_at = Column(DateTime(timezone=True), nullable=True, comment="成立日期")
    approved_at = Column(DateTime(timezone=True), nullable=True, comment="核准日期")
    from_time = Column(DateTime(timezone=True), nullable=True, comment="经营开始日期")
    to_time = Column(DateTime(timezone=True), nullable=True, comment="经营结束日期")
    updated_remote_at = Column(DateTime(timezone=True), nullable=True, comment="天眼查更新时间")

    reg_institute = Column(String(255), nullable=True, comment="登记机关")
    reg_location = Column(TEXT, nullable=True, comment="注册地址")
    business_scope = Column(TEXT, nullable=True, comment="经营范围")
    staff_num_range = Column(String(200), nullable=True, comment="人员规模")
    social_staff_num = Column(Integer, nullable=True, comment="参保人数")
    tags = Column(String(255), nullable=True, comment="企业标签")
    history_names = Column(TEXT, nullable=True, comment="曾用名")
    percentile_score = Column(Integer, nullable=True, comment="企业评分")
    is_micro_ent = Column(Integer, nullable=True, comment="是否小微企业")

    raw_search = Column(JSONB, nullable=True, comment="高级搜索原始条目")
    raw_baseinfo = Column(JSONB, nullable=True, comment="基本信息原始结果")
    search_seen_at = Column(DateTime(timezone=True), nullable=True, comment="最近搜索命中时间")
    baseinfo_fetched_at = Column(DateTime(timezone=True), nullable=True, comment="最近详情拉取时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间")

    __table_args__ = (
        Index(
            "uq_tianyancha_companies_tianyancha_id",
            "tianyancha_id",
            unique=True,
            postgresql_where=tianyancha_id.isnot(None),
        ),
        Index(
            "uq_tianyancha_companies_credit_code",
            "credit_code",
            unique=True,
            postgresql_where=credit_code.isnot(None),
        ),
        Index("ix_tianyancha_companies_normalized_name", "normalized_name"),
        Index("ix_tianyancha_companies_reg_number", "reg_number"),
        Index("ix_tianyancha_companies_org_number", "org_number"),
        Index("ix_tianyancha_companies_area", "base", "city", "district"),
        Index("ix_tianyancha_companies_industry", "industry"),
        Index("ix_tianyancha_companies_search_seen_at", "search_seen_at"),
        Index("ix_tianyancha_companies_baseinfo_fetched_at", "baseinfo_fetched_at"),
    )


class TianyanchaSearchQuery(Base):
    """天眼查高级搜索缓存和审计记录。"""

    __tablename__ = "tianyancha_search_queries"

    id = Column(Integer, primary_key=True, index=True)
    fingerprint = Column(String(64), nullable=False, unique=True, comment="搜索参数指纹")
    word = Column(String(255), nullable=True, comment="关键词")
    category_guobiao = Column(String(32), nullable=True, comment="行业代码")
    area_code = Column(String(32), nullable=True, comment="地区代码")
    page_num = Column(Integer, nullable=False, default=1, comment="页码")
    page_size = Column(Integer, nullable=False, default=20, comment="每页条数")
    total = Column(Integer, nullable=True, comment="远程命中总数")
    company_ids = Column(JSONB, nullable=False, default=list, comment="本地企业ID列表")
    request_params = Column(JSONB, nullable=False, default=dict, comment="请求参数")
    response_error_code = Column(Integer, nullable=True, comment="天眼查错误码")
    response_reason = Column(String(255), nullable=True, comment="天眼查错误说明")
    fetched_at = Column(DateTime(timezone=True), nullable=True, comment="最近远程调用时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), comment="更新时间")

    __table_args__ = (
        Index("ix_tianyancha_search_queries_fingerprint", "fingerprint"),
        Index("ix_tianyancha_search_queries_fetched_at", "fetched_at"),
    )
