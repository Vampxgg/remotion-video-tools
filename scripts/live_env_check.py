# -*- coding: utf-8 -*-
"""Gemini Live API 真实联调前置环境检查。"""

import argparse
import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.settings import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-auth", action="store_true", help="跳过 ADC token 刷新检查")
    args = parser.parse_args()

    ok = True
    checks = []

    checks.append(("GCP_PROJECT_ID", bool(settings.GCP_PROJECT_ID), settings.GCP_PROJECT_ID))
    checks.append(("GCP_LOCATION_ID", bool(settings.GCP_LOCATION_ID), settings.GCP_LOCATION_ID))
    checks.append(("GEMINI_LIVE_MODEL", bool(settings.GEMINI_LIVE_MODEL), settings.GEMINI_LIVE_MODEL))
    checks.append(
        (
            "google-genai",
            importlib.util.find_spec("google.genai") is not None,
            "pip install -r requirements-live.txt",
        )
    )
    checks.append(
        (
            "websockets",
            importlib.util.find_spec("websockets") is not None,
            "pip install -r requirements-live.txt",
        )
    )

    for name, passed, detail in checks:
        ok = ok and passed
        print(f"[{'OK' if passed else 'FAIL'}] {name}: {detail}")

    if not args.skip_auth:
        try:
            import google.auth
            import google.auth.transport.requests

            credentials, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            credentials.refresh(google.auth.transport.requests.Request())
            has_token = bool(credentials.token)
            ok = ok and has_token
            print(f"[{'OK' if has_token else 'FAIL'}] ADC token: project={project}")
        except Exception as exc:
            ok = False
            print(f"[FAIL] ADC token: {exc}")
            print("       请先运行：gcloud auth application-default login")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
