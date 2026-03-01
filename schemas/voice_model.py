# schemas/voice_model.py

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# --- 用于创建新模型的基础模型 ---
# API使用者只需要提供这些核心信息
class VoiceModelBase(BaseModel):
    name: str = Field(..., max_length=100, description="模型的显示名称")
    handle: str = Field(..., max_length=100, pattern=r'^[a-zA-Z0-9_-]+$',
                        description="模型的唯一句柄 (只能包含字母、数字、下划线、中划线)")
    description: Optional[str] = Field(None, description="模型的描述")
    avatar_url: Optional[str] = Field(None, description="头像图片的URL")
    preview_audio_url: Optional[str] = Field(None, description="试听语音文件的URL")
    language: Optional[str] = Field(None, description="主要语言")
    gender: Optional[str] = Field(None, description="性别")
    tags: Optional[List[str]] = Field(None, description="标签列表")


# --- 用于 'POST' /voice_models/ 接口的模型 ---
class VoiceModelCreate(VoiceModelBase):
    pass  # 目前和Base一样，未来可以扩展


# --- 用于 'PUT' /voice_models/{id} 接口的模型 ---
# 所有字段都是可选的，因为用户可能只想更新其中一部分
class VoiceModelUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    handle: Optional[str] = Field(None, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    description: Optional[str] = None
    avatar_url: Optional[str] = None
    preview_audio_url: Optional[str] = None
    language: Optional[str] = None
    gender: Optional[str] = None
    tags: Optional[List[str]] = None
    is_public: Optional[bool] = None


# --- 用于从数据库读取并返回给API客户端的模型 ---
# 包含了所有数据库字段，包括自动生成的 id 和时间戳等
class VoiceModelInDB(VoiceModelBase):
    id: int
    provider: str
    play_count: int
    share_count: int
    like_count: int
    save_count: int
    usage_count: int
    is_public: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True  # SQLAlchemy 2.0+ 推荐使用的模式，允许从ORM对象自动映射
        # orm_mode = True # 旧版Pydantic/SQLAlchemy的用法
