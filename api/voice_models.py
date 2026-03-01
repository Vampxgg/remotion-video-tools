# api/voice_models.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from db import crud, database
from schemas import voice_model as schemas

router = APIRouter()


async def get_db() -> AsyncSession:
    async with database.AsyncSessionLocal() as session:
        yield session


# --- CREATE ---
@router.post(
    "/create_model",
    response_model=schemas.VoiceModelInDB,
    status_code=status.HTTP_201_CREATED,
    summary="创建新的声音模型"
)
async def create_voice_model(
        *,
        db: AsyncSession = Depends(get_db),
        model_in: schemas.VoiceModelCreate
):
    existing_model = await crud.get_voice_model_by_handle(db, handle=model_in.handle)
    if existing_model:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Handle '{model_in.handle}' already exists.",
        )
    return await crud.create_voice_model(db=db, model_in=model_in)


# --- READ (Multiple) ---
@router.get(
    "/search_models",
    response_model=List[schemas.VoiceModelInDB],
    summary="获取声音模型列表（分页）"
)
async def read_voice_models(
        db: AsyncSession = Depends(get_db),
        skip: int = 0,
        limit: int = 100
):
    return await crud.get_voice_models(db=db, skip=skip, limit=limit)


# --- READ (Single by Handle) ---
@router.get(
    "/search_model/{handle}",  # <--- 改为 handle
    response_model=schemas.VoiceModelInDB,
    summary="获取指定Handle的声音模型"
)
async def read_voice_model_by_handle(  # <--- 函数名也改一下，更清晰
        *,
        db: AsyncSession = Depends(get_db),
        handle: str  # <--- 参数名改为 handle
):
    db_model = await crud.get_voice_model_by_handle(db=db, handle=handle)  # <--- 调用正确的 crud 函数
    if db_model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Voice model with handle '{handle}' not found"
        )
    return db_model


# --- UPDATE by Handle ---
@router.put(
    "/update_model/{handle}",  # <--- 改为 handle
    response_model=schemas.VoiceModelInDB,
    summary="更新指定Handle的声音模型"
)
async def update_voice_model_by_handle(  # <--- 函数名也改一下
        *,
        db: AsyncSession = Depends(get_db),
        handle: str,  # <--- 参数名改为 handle
        model_in: schemas.VoiceModelUpdate
):
    db_model = await crud.get_voice_model_by_handle(db=db, handle=handle)  # <--- 先用 handle 查出来
    if db_model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Voice model with handle '{handle}' not found"
        )

    if model_in.handle and model_in.handle != db_model.handle:
        existing_model = await crud.get_voice_model_by_handle(db, handle=model_in.handle)
        if existing_model:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Handle '{model_in.handle}' already exists.",
            )

    return await crud.update_voice_model(db=db, db_model=db_model, model_in=model_in)  # <--- 再传入对象去更新


# --- DELETE by Handle ---
@router.delete(
    "/delete_model/{handle}",
    response_model=schemas.VoiceModelInDB,
    summary="删除指定Handle的声音模型"
)
async def delete_voice_model_by_handle(  # <--- 函数名也改一下
        *,
        db: AsyncSession = Depends(get_db),
        handle: str  # <--- 参数名改为 handle
):
    deleted_model = await crud.delete_voice_model_by_handle(db=db, handle=handle)  # <--- 调用正确的 crud 函数
    if deleted_model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Voice model with handle '{handle}' not found"
        )
    return deleted_model
