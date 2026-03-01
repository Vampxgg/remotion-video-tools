# -*- coding: utf-8 -*-
# @File：models.py
# @Time：2025/10/13 11:32
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
# db/models.py

from sqlalchemy import Column, Integer, String, DateTime, func, UniqueConstraint, Index, Boolean, ARRAY
from sqlalchemy.dialects.postgresql import TEXT
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
