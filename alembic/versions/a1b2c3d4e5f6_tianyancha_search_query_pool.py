"""Add pool progress fields to tianyancha_search_queries

Revision ID: a1b2c3d4e5f6
Revises: 9c0f1a2b3d4e
Create Date: 2026-07-13 10:00:00.000000

企业池语义改造：page_num/page_size 从"身份"降级为"最近一次翻页"记录，
新增 max_page_fetched（池已翻到的最大页）与 exhausted（池是否翻完），
供 research_region_companies 按三元组指纹断点续翻、累积去重企业池。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9c0f1a2b3d4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tianyancha_search_queries",
        sa.Column(
            "max_page_fetched",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已翻到的最大页码（企业池语义的进度）",
        ),
    )
    op.add_column(
        "tianyancha_search_queries",
        sa.Column(
            "exhausted",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="企业池是否已翻完",
        ),
    )
    # 存量五元组记录：把已知 page_num 视为已翻进度，避免升级后被判为"未翻过"而重复远程。
    op.execute(
        "UPDATE tianyancha_search_queries "
        "SET max_page_fetched = page_num "
        "WHERE max_page_fetched = 0 AND page_num > 0"
    )


def downgrade() -> None:
    op.drop_column("tianyancha_search_queries", "exhausted")
    op.drop_column("tianyancha_search_queries", "max_page_fetched")
