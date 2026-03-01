# db/crud.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List, Optional, Union

from .models import VoiceModel
from schemas.voice_model import VoiceModelCreate, VoiceModelUpdate


# --- 按 ID (主键) 查找 ---
async def get_voice_model_by_id(db: AsyncSession, model_id: int) -> Optional[VoiceModel]:
    """通过主键 ID 获取单个声音模型。这是给内部或特定场景使用的。"""
    return await db.get(VoiceModel, model_id)


# --- 按 Handle (字符串标识) 查找 ---
async def get_voice_model_by_handle(db: AsyncSession, handle: str) -> Optional[VoiceModel]:
    """通过句柄(handle)获取单个声音模型"""
    query = select(VoiceModel).where(VoiceModel.handle == handle)
    result = await db.execute(query)
    return result.scalar_one_or_none()


# --- 获取列表 ---
async def get_voice_models(db: AsyncSession, skip: int = 0, limit: int = 100) -> List[VoiceModel]:
    """获取声音模型列表（支持分页）"""
    query = select(VoiceModel).offset(skip).limit(limit).order_by(VoiceModel.id)
    result = await db.execute(query)
    return result.scalars().all()


# --- 创建 ---
async def create_voice_model(db: AsyncSession, *, model_in: VoiceModelCreate) -> VoiceModel:
    """创建一个新的声音模型"""
    db_model = VoiceModel(**model_in.model_dump())
    db.add(db_model)
    await db.commit()
    await db.refresh(db_model)
    return db_model


# --- 更新 ---
async def update_voice_model(
        db: AsyncSession, *, db_model: VoiceModel, model_in: VoiceModelUpdate
) -> VoiceModel:
    """更新一个已存在的声音模型 (传入的是已查出的模型对象)"""
    update_data = model_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_model, field, value)
    db.add(db_model)
    await db.commit()
    await db.refresh(db_model)
    return db_model


# --- 按 Handle 删除 ---
async def delete_voice_model_by_handle(db: AsyncSession, *, handle: str) -> Optional[VoiceModel]:
    """通过句柄(handle)删除一个声音模型"""
    db_model = await get_voice_model_by_handle(db, handle=handle)
    if db_model:
        await db.delete(db_model)
        await db.commit()
    return db_model
