"""真实调用当前 ASR，通过项目路由验证标准响应及分段时间戳。

用法：python -m scripts.probe_asr --audio sample.wav --output runtime/asr_probe.json
会向配置的 ASR 提供商上传指定音频；使用提供商当前计费规则。
"""

import argparse
import json
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import fish_asr


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--language', default='zh')
    args = parser.parse_args()
    audio = args.audio.read_bytes()
    # 只启动 ASR 资源，避免探测时触发主应用其它模块的后台任务。
    app = FastAPI(lifespan=fish_asr.lifespan_resources)
    app.include_router(fish_asr.router_asr, prefix='/api')
    started = time.monotonic()
    with TestClient(app) as client:
        response = client.post(
            '/api/asr/transcribe',
            data={'workflow_id': 'asr-live-probe', 'language': args.language, 'ignore_timestamps': 'false'},
            files={'audio': (args.audio.name, audio, mimetypes.guess_type(args.audio.name)[0] or 'application/octet-stream')},
        )
    body = response.json()
    report = {
        'tested_at_utc': datetime.now(timezone.utc).isoformat(),
        'provider': fish_asr.ASR_PROVIDER,
        'model': fish_asr.SILICONFLOW_ASR_MODEL if fish_asr.ASR_PROVIDER == 'siliconflow' else None,
        'audio': str(args.audio.resolve()),
        'elapsed_sec': round(time.monotonic() - started, 3),
        'http_status': response.status_code,
        'response': body,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    response.raise_for_status()
    if body.get('code') != 200:
        raise SystemExit('ASR failed; inspect response in report.')
    data = body.get('data') or {}
    if not data.get('timestamps_available'):
        raise SystemExit('ASR returned no complete timestamps; inspect report.')
    print('PASS: real ASR request through /api/asr/transcribe returned timestamps.')


if __name__ == '__main__':
    main()
