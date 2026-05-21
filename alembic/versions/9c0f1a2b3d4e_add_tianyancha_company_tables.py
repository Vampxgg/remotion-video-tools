"""Add tianyancha company tables

Revision ID: 9c0f1a2b3d4e
Revises: 25332d4efd14
Create Date: 2026-05-21 15:08:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "9c0f1a2b3d4e"
down_revision: Union[str, Sequence[str], None] = "25332d4efd14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tianyancha_companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tianyancha_id", sa.BigInteger(), nullable=True, comment="天眼查企业ID"),
        sa.Column("name", sa.String(length=255), nullable=False, comment="企业名称"),
        sa.Column("normalized_name", sa.String(length=255), nullable=False, comment="规范化企业名称"),
        sa.Column("credit_code", sa.String(length=255), nullable=True, comment="统一社会信用代码"),
        sa.Column("reg_number", sa.String(length=50), nullable=True, comment="注册号"),
        sa.Column("org_number", sa.String(length=50), nullable=True, comment="组织机构代码"),
        sa.Column("tax_number", sa.String(length=255), nullable=True, comment="纳税人识别号"),
        sa.Column("reg_status", sa.String(length=31), nullable=True, comment="经营状态"),
        sa.Column("reg_capital", sa.String(length=50), nullable=True, comment="注册资本"),
        sa.Column("actual_capital", sa.String(length=50), nullable=True, comment="实收资本"),
        sa.Column("legal_person_name", sa.String(length=255), nullable=True, comment="法定代表人"),
        sa.Column("company_type", sa.Integer(), nullable=True, comment="机构类型"),
        sa.Column("company_org_type", sa.String(length=127), nullable=True, comment="企业类型"),
        sa.Column("legal_type", sa.Integer(), nullable=True, comment="法人类型"),
        sa.Column("base", sa.String(length=31), nullable=True, comment="省份简称或省份"),
        sa.Column("city", sa.String(length=50), nullable=True, comment="城市"),
        sa.Column("district", sa.String(length=50), nullable=True, comment="区县"),
        sa.Column("district_code", sa.String(length=20), nullable=True, comment="行政区划代码"),
        sa.Column("industry", sa.String(length=255), nullable=True, comment="行业"),
        sa.Column("category", sa.String(length=255), nullable=True, comment="国民经济行业门类"),
        sa.Column("category_code_first", sa.String(length=32), nullable=True, comment="门类代码"),
        sa.Column("category_code_second", sa.String(length=32), nullable=True, comment="大类代码"),
        sa.Column("category_code_third", sa.String(length=32), nullable=True, comment="中类代码"),
        sa.Column("category_code_fourth", sa.String(length=32), nullable=True, comment="小类代码"),
        sa.Column("established_at", sa.DateTime(timezone=True), nullable=True, comment="成立日期"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True, comment="核准日期"),
        sa.Column("from_time", sa.DateTime(timezone=True), nullable=True, comment="经营开始日期"),
        sa.Column("to_time", sa.DateTime(timezone=True), nullable=True, comment="经营结束日期"),
        sa.Column("updated_remote_at", sa.DateTime(timezone=True), nullable=True, comment="天眼查更新时间"),
        sa.Column("reg_institute", sa.String(length=255), nullable=True, comment="登记机关"),
        sa.Column("reg_location", sa.TEXT(), nullable=True, comment="注册地址"),
        sa.Column("business_scope", sa.TEXT(), nullable=True, comment="经营范围"),
        sa.Column("staff_num_range", sa.String(length=200), nullable=True, comment="人员规模"),
        sa.Column("social_staff_num", sa.Integer(), nullable=True, comment="参保人数"),
        sa.Column("tags", sa.String(length=255), nullable=True, comment="企业标签"),
        sa.Column("history_names", sa.TEXT(), nullable=True, comment="曾用名"),
        sa.Column("percentile_score", sa.Integer(), nullable=True, comment="企业评分"),
        sa.Column("is_micro_ent", sa.Integer(), nullable=True, comment="是否小微企业"),
        sa.Column("raw_search", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="高级搜索原始条目"),
        sa.Column("raw_baseinfo", postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment="基本信息原始结果"),
        sa.Column("search_seen_at", sa.DateTime(timezone=True), nullable=True, comment="最近搜索命中时间"),
        sa.Column("baseinfo_fetched_at", sa.DateTime(timezone=True), nullable=True, comment="最近详情拉取时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tianyancha_companies_id"), "tianyancha_companies", ["id"], unique=False)
    op.create_index(
        "uq_tianyancha_companies_tianyancha_id",
        "tianyancha_companies",
        ["tianyancha_id"],
        unique=True,
        postgresql_where=sa.text("tianyancha_id IS NOT NULL"),
    )
    op.create_index(
        "uq_tianyancha_companies_credit_code",
        "tianyancha_companies",
        ["credit_code"],
        unique=True,
        postgresql_where=sa.text("credit_code IS NOT NULL"),
    )
    op.create_index("ix_tianyancha_companies_normalized_name", "tianyancha_companies", ["normalized_name"], unique=False)
    op.create_index("ix_tianyancha_companies_reg_number", "tianyancha_companies", ["reg_number"], unique=False)
    op.create_index("ix_tianyancha_companies_org_number", "tianyancha_companies", ["org_number"], unique=False)
    op.create_index("ix_tianyancha_companies_area", "tianyancha_companies", ["base", "city", "district"], unique=False)
    op.create_index("ix_tianyancha_companies_industry", "tianyancha_companies", ["industry"], unique=False)
    op.create_index("ix_tianyancha_companies_search_seen_at", "tianyancha_companies", ["search_seen_at"], unique=False)
    op.create_index("ix_tianyancha_companies_baseinfo_fetched_at", "tianyancha_companies", ["baseinfo_fetched_at"], unique=False)

    op.create_table(
        "tianyancha_search_queries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False, comment="搜索参数指纹"),
        sa.Column("word", sa.String(length=255), nullable=True, comment="关键词"),
        sa.Column("category_guobiao", sa.String(length=32), nullable=True, comment="行业代码"),
        sa.Column("area_code", sa.String(length=32), nullable=True, comment="地区代码"),
        sa.Column("page_num", sa.Integer(), nullable=False, comment="页码"),
        sa.Column("page_size", sa.Integer(), nullable=False, comment="每页条数"),
        sa.Column("total", sa.Integer(), nullable=True, comment="远程命中总数"),
        sa.Column(
            "company_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="本地企业ID列表",
        ),
        sa.Column(
            "request_params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="请求参数",
        ),
        sa.Column("response_error_code", sa.Integer(), nullable=True, comment="天眼查错误码"),
        sa.Column("response_reason", sa.String(length=255), nullable=True, comment="天眼查错误说明"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True, comment="最近远程调用时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True, comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True, comment="更新时间"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    op.create_index(op.f("ix_tianyancha_search_queries_id"), "tianyancha_search_queries", ["id"], unique=False)
    op.create_index("ix_tianyancha_search_queries_fingerprint", "tianyancha_search_queries", ["fingerprint"], unique=False)
    op.create_index("ix_tianyancha_search_queries_fetched_at", "tianyancha_search_queries", ["fetched_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tianyancha_search_queries_fetched_at", table_name="tianyancha_search_queries")
    op.drop_index("ix_tianyancha_search_queries_fingerprint", table_name="tianyancha_search_queries")
    op.drop_index(op.f("ix_tianyancha_search_queries_id"), table_name="tianyancha_search_queries")
    op.drop_table("tianyancha_search_queries")

    op.drop_index("ix_tianyancha_companies_baseinfo_fetched_at", table_name="tianyancha_companies")
    op.drop_index("ix_tianyancha_companies_search_seen_at", table_name="tianyancha_companies")
    op.drop_index("ix_tianyancha_companies_industry", table_name="tianyancha_companies")
    op.drop_index("ix_tianyancha_companies_area", table_name="tianyancha_companies")
    op.drop_index("ix_tianyancha_companies_org_number", table_name="tianyancha_companies")
    op.drop_index("ix_tianyancha_companies_reg_number", table_name="tianyancha_companies")
    op.drop_index("ix_tianyancha_companies_normalized_name", table_name="tianyancha_companies")
    op.drop_index("uq_tianyancha_companies_credit_code", table_name="tianyancha_companies")
    op.drop_index("uq_tianyancha_companies_tianyancha_id", table_name="tianyancha_companies")
    op.drop_index(op.f("ix_tianyancha_companies_id"), table_name="tianyancha_companies")
    op.drop_table("tianyancha_companies")
