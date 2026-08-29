# -*- coding: utf-8 -*-
"""AWS 会话工厂：本机弱权限用户 AssumeRole 到子账号 Admin 后统一发 boto3 client。

背景(真实验证结论)：
- 本机 CLI 身份 ``arn:aws:iam::703621193912:user/BedrockAPIKey-58cx``(主/Payer 账号)
  权限极小：只有 Bedrock 调用 + ``ce:GetCostAndUsage``，缺 CloudWatch/Logs/CloudTrail。
- 真正的 AdministratorAccess 在**子账号 502225588666** 的 ``OrganizationAccountAccessRole``。
- 且**用量/花费全部发生在子账号**，主账号自身几乎无用量。

因此本模块封装 "本机默认凭据 → sts.assume_role 到子账号 Admin → 用临时凭据建 client"。
临时凭据默认 1 小时过期，这里带**过期缓存 + 自动刷新**(提前 5 分钟续期)，避免长
流水线中途 401。所有 fetch 模块只经由 ``AwsSession.client(service)`` 拿 client，
不直接 boto3.client，保证身份统一、可测试。

独立自检：
    python -m aws_session   # 打印 assume 后的调用者身份 + 各服务 client 是否可建
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 临时凭据到期前多久强制刷新(秒)。AssumeRole 默认 3600s，留 5 分钟缓冲。
_REFRESH_MARGIN_SECONDS = 300


class AwsSessionConfig:
    """AwsSession 所需配置(由上层从 settings 组装)。"""

    def __init__(
        self,
        assume_role_arn: str,
        region: str,
        role_session_name: str = "bedrock-usage-report",
        session_duration_seconds: int = 3600,
        profile_name: Optional[str] = None,
    ) -> None:
        self.assume_role_arn = assume_role_arn
        self.region = region
        self.role_session_name = role_session_name
        self.session_duration_seconds = session_duration_seconds
        self.profile_name = profile_name


class AwsSession:
    """管理一段 AssumeRole 到子账号的临时凭据，并按服务派发 boto3 client。

    线程安全：daily_pipeline 在线程池里跑，同一实例可能被并发访问，故用锁保护
    凭据刷新。client 本身 boto3 保证线程安全(每次按需新建即可，开销小)。
    """

    def __init__(self, cfg: AwsSessionConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._creds: Optional[dict[str, Any]] = None
        self._expiry: Optional[dt.datetime] = None
        self._base_session = None  # 惰性建，避免 import 时就要求装 boto3

    # ------------------------------------------------------------------ #
    #  凭据获取与刷新
    # ------------------------------------------------------------------ #

    def _get_base_session(self):
        """本机默认凭据链的基础 session(用于 sts.assume_role)。"""
        if self._base_session is None:
            import boto3

            self._base_session = boto3.Session(profile_name=self._cfg.profile_name)
        return self._base_session

    def _needs_refresh(self) -> bool:
        if self._creds is None or self._expiry is None:
            return True
        now = dt.datetime.now(dt.timezone.utc)
        return now >= (self._expiry - dt.timedelta(seconds=_REFRESH_MARGIN_SECONDS))

    def _refresh(self) -> None:
        """执行一次 AssumeRole，缓存临时凭据与过期时间。"""
        base = self._get_base_session()
        sts = base.client("sts", region_name=self._cfg.region)
        resp = sts.assume_role(
            RoleArn=self._cfg.assume_role_arn,
            RoleSessionName=self._cfg.role_session_name,
            DurationSeconds=self._cfg.session_duration_seconds,
        )
        c = resp["Credentials"]
        self._creds = {
            "aws_access_key_id": c["AccessKeyId"],
            "aws_secret_access_key": c["SecretAccessKey"],
            "aws_session_token": c["SessionToken"],
        }
        # Expiration 是带 tzinfo 的 datetime。
        self._expiry = c["Expiration"]
        logger.info(
            "AssumeRole 成功: %s (临时凭据到期 %s)",
            self._cfg.assume_role_arn,
            self._expiry.isoformat(),
        )

    def _ensure_creds(self) -> dict[str, Any]:
        with self._lock:
            if self._needs_refresh():
                self._refresh()
            assert self._creds is not None
            return self._creds

    # ------------------------------------------------------------------ #
    #  对外：派发 client
    # ------------------------------------------------------------------ #

    def client(self, service: str, region: Optional[str] = None):
        """返回一个用子账号临时凭据初始化的 boto3 client。

        Parameters
        ----------
        service: AWS 服务名，如 "ce" / "logs" / "cloudtrail" / "bedrock" / "sts"。
        region: 覆盖默认 region(如某服务只在特定 region)；None 用 cfg.region。

        注意：``ce``(Cost Explorer)是全局服务，客户端固定用 us-east-1 端点，
        但账户维度仍由临时凭据身份决定。
        """
        import boto3

        creds = self._ensure_creds()
        use_region = "us-east-1" if service == "ce" else (region or self._cfg.region)
        return boto3.client(service, region_name=use_region, **creds)

    def caller_identity(self) -> dict[str, Any]:
        """当前(assume 后)的调用者身份，用于自检/日志。"""
        return self.client("sts").get_caller_identity()


def _self_test() -> int:
    """独立自检：assume 到子账号后打印身份 + 试建各服务 client。"""
    import json
    import os

    role_arn = os.environ.get(
        "AWS_USAGE_REPORT_ASSUME_ROLE_ARN",
        "arn:aws:iam::502225588666:role/OrganizationAccountAccessRole",
    )
    region = os.environ.get("AWS_USAGE_REPORT_REGION", "us-east-1")
    sess = AwsSession(AwsSessionConfig(assume_role_arn=role_arn, region=region))
    ident = sess.caller_identity()
    print("AssumeRole 后身份:", json.dumps(ident, default=str, ensure_ascii=False))
    for svc in ("ce", "logs", "cloudtrail", "bedrock"):
        sess.client(svc)
        print(f"  client({svc}) OK")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_self_test())
