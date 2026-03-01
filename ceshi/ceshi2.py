"""Dify Python node helper: push generated Remotion code to x-pilot-e2b-server and get previewUrl.

Usage (in Dify Python sandbox):
- Call main(json_string, code_array, ...optional args)

Notes:
- Calls the Player endpoint (Stage 2/3): POST /api/projects/{userId}/preview
- Legacy endpoints have been removed.
- Request body uses the Dify bundle format: { dify: { json_string, code_array } }

Return shape (kept minimal for Dify workflows):
- ok: bool
- preview_url: str | None (for backward-compat; equals player_url when use_player=True)
- player_url: str | None
- project_id: str | None
- error: str | None
- next_step: str | None (English, actionable)

Optional return fields (disabled by default):
- sandbox_id: str | None (enable via include_sandbox_id=True; useful for sandbox reuse)

"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


def _truncate(s: Any, max_len: int = 400) -> str:
    text = "" if s is None else str(s)
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"...<truncated:{len(text) - max_len}>"


def _sleep_backoff(attempt: int, base_delay_sec: float) -> None:
    # attempt: 1..N
    base = max(0.05, float(base_delay_sec or 0.8))
    # exponential backoff with small jitter
    delay = min(20.0, base * (2 ** max(0, attempt - 1)))
    delay = delay + random.random() * min(0.8, base)
    time.sleep(delay)


def _is_retryable_http_status(status: Optional[int]) -> bool:
    if status is None:
        return False
    return int(status) in (408, 425, 429, 500, 502, 503, 504)


def _should_retry_response(resp: Dict[str, Any]) -> bool:
    # Do NOT retry on fatal preflight (user code error) or bad request.
    if resp.get("previewAllowed") is False:
        return False

    st = resp.get("_http_status")
    if resp.get("_http_error"):
        if resp.get("_error_type") in ("network", "exception"):
            return True
        return _is_retryable_http_status(st)

    # Non-HTTPError responses: if server explicitly says upstream_error, treat as retryable.
    if resp.get("error") == "upstream_error":
        return True

    return False


def _post_json(
        url: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        timeout_sec: int = 60,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {"raw": raw}
            data["_http_status"] = getattr(resp, "status", None)
            data["_http_error"] = False
            return data
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {"raw": raw}
        data["_http_status"] = e.code
        data["_http_error"] = True
        data["_error_type"] = "http"
        return data
    except urllib.error.URLError as e:
        # DNS / refused / timeout / TLS errors will land here.
        return {
            "_http_status": None,
            "_http_error": True,
            "_error_type": "network",
            "_error": str(getattr(e, "reason", e)),
        }
    except Exception as e:
        return {
            "_http_status": None,
            "_http_error": True,
            "_error_type": "exception",
            "_error": str(e),
        }


def _extract_first_preflight_issue(resp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    report = resp.get("scenePreflightReport")
    if isinstance(report, dict):
        issues = report.get("issues")
        if isinstance(issues, list) and issues:
            first = issues[0]
            return first if isinstance(first, dict) else None

    issues2 = resp.get("scenePreflightIssues")
    if isinstance(issues2, list) and issues2:
        first = issues2[0]
        return first if isinstance(first, dict) else None

    return None


def _extract_first_structured_error(resp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    errs = resp.get("errors")
    if isinstance(errs, list) and errs:
        first = errs[0]
        return first if isinstance(first, dict) else None
    return None


def _scene_label(issue: Optional[Dict[str, Any]]) -> str:
    if not issue:
        return "(unknown scene)"
    sid = issue.get("sceneId")
    sname = issue.get("sceneName")
    if sname and sid:
        return f"{sname} ({sid})"
    if sname:
        return str(sname)
    if sid:
        return str(sid)
    comp = issue.get("component") or issue.get("componentPath")
    if comp:
        return str(comp)
    return "(unknown scene)"


def _format_location(issue: Dict[str, Any]) -> str:
    file_ = issue.get("file") or issue.get("componentPath") or issue.get("component")
    line = issue.get("line")
    col = issue.get("column")
    if file_ and isinstance(line, int) and isinstance(col, int):
        return f"{file_}:{line}:{col}"
    if file_ and isinstance(line, int):
        return f"{file_}:{line}"
    if file_:
        return str(file_)
    return ""


def _issue_code(issue: Dict[str, Any]) -> str:
    iss = issue.get("issue")
    if isinstance(iss, dict) and iss.get("code"):
        return str(iss.get("code"))
    if issue.get("code"):
        return str(issue.get("code"))
    return "UNKNOWN"


def _suggest_fix_for_issue(code: str) -> str:
    c = (code or "").upper()
    if c == "SCENE_EXPORT_ERROR":
        return "Ensure the scene TSX contains `export default function SceneName()` (a named default function export)."
    if c == "SCENE_SYNTAX_ERROR":
        return "Fix TypeScript/TSX syntax errors in that scene (unclosed JSX tags, missing commas, invalid imports)."
    if c in ("SCENE_LOAD_ERROR", "SCENE_RUNTIME_ERROR"):
        return "Check imports and runtime dependencies used by this scene (wrong relative paths, missing exports, missing files)."
    if c == "SCENE_MISSING_FILE":
        return "Ensure the referenced component file exists in the sandbox (path is correct and file is included in your bundle)."
    if c == "INVALID_COMPONENT_PATH":
        return "Ensure the generated component path is relative and points under `src/scenes/` (no `..` segments)."
    if c == "MANIFEST_READ_ERROR":
        return "Ensure `json_string` is valid JSON and contains a non-empty `scenes` array with required fields."
    if c == "SCENE_NOT_REGISTERED":
        return "Ensure the scene is included in the generated manifest and the file is written under `src/scenes/`."
    return "Review the issue detail and fix the scene code or its referenced files accordingly."


def _build_scene_focused_guidance(resp: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    issue = _extract_first_preflight_issue(resp)
    if issue:
        code = _issue_code(issue)
        loc = _format_location(issue)
        detail = _truncate(issue.get("detail") or issue.get("message"))
        label = _scene_label(issue)

        error = f"Scene preflight failed: {code} in {label}"
        if loc:
            error += f" @ {loc}"

        next_step_lines = [
            f"Failing scene: {label}",
            f"Issue code: {code}",
        ]
        if loc:
            next_step_lines.append(f"Location: {loc}")
        if detail:
            next_step_lines.append(f"Detail: {detail}")
        next_step_lines.append(f"Suggested fix: {_suggest_fix_for_issue(code)}")
        next_step_lines.append(
            "If your code is wrapped in Markdown fences, remove the fences before sending, or ensure each code_array item is pure TSX."
        )

        return error, "\n".join(next_step_lines)

    # If no preflight issue is present, fall back to structured errors.
    serr = _extract_first_structured_error(resp)
    if serr:
        phase = serr.get("phase")
        msg = _truncate(serr.get("message"))
        code2 = serr.get("code")
        cmd = _truncate(serr.get("command"), 240)
        stderr_tail = _truncate(serr.get("stderrTail"), 600)

        error = f"Server error during {phase or 'unknown'}: {msg or 'unknown'}"
        next_step_lines = [
            f"Phase: {phase or 'unknown'}",
        ]
        if code2:
            next_step_lines.append(f"Error code: {code2}")
        if msg:
            next_step_lines.append(f"Message: {msg}")
        if cmd:
            next_step_lines.append(f"Command: {cmd}")
        if stderr_tail:
            next_step_lines.append(f"Stderr (tail): {stderr_tail}")
        next_step_lines.append(
            "If this is an install/dev error, try reusing a clean sandbox (omit sandbox_id) or set wait_for_ready=True and retry."
        )

        return error, "\n".join(next_step_lines)

    return None, None


def main(
        json_string: str,
        code_array: List[str],
        server_base_url: str = "http://35.232.154.66:7780",
        api_token: Optional[str] = None,
        user_id: Optional[str] = None,
        job_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
        template_id: Optional[str] = None,
        template_name: Optional[str] = None,
        start_dev: bool = True,
        wait_for_ready: bool = False,
        timeout_sec: int = 90,
        include_sandbox_id: bool = False,
        reuse: bool = True,
        retry_attempts: int = 3,
        retry_base_delay_sec: float = 0.8,
        use_player: bool = True,
) -> Dict[str, Any]:
    """Entry for Dify.

    Tip: To get stable "2nd push is fast" behavior (like /admin), enable include_sandbox_id=True,
    store the returned sandbox_id in your workflow state, then pass it back as sandbox_id on subsequent calls.
    """

    if not isinstance(json_string, str) or not json_string.strip():
        return {
            "ok": False,
            "preview_url": None,
            "player_url": None,
            "project_id": None,
            "error": "invalid input: json_string is empty or not a string",
            "next_step": "Pass a non-empty json_string. It must be valid JSON and contain a non-empty `scenes` array.",
        }

    if not isinstance(code_array, list) or len(code_array) == 0:
        return {
            "ok": False,
            "preview_url": None,
            "player_url": None,
            "project_id": None,
            "error": "invalid input: code_array is empty or not a list",
            "next_step": "Pass a non-empty list of strings. Each item in code_array must be one TSX scene code string.",
        }

    for i, item in enumerate(code_array):
        if not isinstance(item, str) or not item.strip():
            return {
                "ok": False,
                "preview_url": None,
                "player_url": None,
                "project_id": None,
                "error": f"invalid input: code_array[{i}] is empty or not a string",
                "next_step": "Ensure every code_array item is a non-empty string (no null/objects/empty strings).",
            }

    base = (server_base_url or "http://35.232.154.66:7780").rstrip("/")

    # Stage 2/3: Player URL mode
    uid = str(user_id).strip() if user_id else "u_anon"
    url = f"{base}/api/projects/{uid}/preview"
    payload: Dict[str, Any] = {
        "dify": {
            "json_string": json_string,
            "code_array": code_array,
        },
    }

    # NOTE: legacy args are kept for backward compatibility with older Dify workflows,
    # but the legacy endpoints are removed on the server.
    _ = (use_player, job_id, sandbox_id, template_id, template_name, start_dev, reuse)

    # wait_for_ready historically affected Studio readiness probing; kept only for client timeout tuning.

    headers: Dict[str, str] = {}
    if api_token and str(api_token).strip():
        tok = str(api_token).strip()
        # Accept either raw token or a full "Bearer xxx" string.
        headers["Authorization"] = tok if tok.lower().startswith("bearer ") else f"Bearer {tok}"

    try:
        timeout_sec_int = int(timeout_sec)
    except Exception:
        timeout_sec_int = 90

    # If wait_for_ready=True, the server may wait longer (cold start / edge readiness).
    # Ensure the client timeout is not shorter than the server-side wait window.
    if wait_for_ready and timeout_sec_int < 210:
        timeout_sec_int = 210

    # Retry on transient errors (network / 502 / 429...).
    try:
        attempts = int(retry_attempts)
    except Exception:
        attempts = 3
    attempts = max(1, min(8, attempts))

    resp: Dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        resp = _post_json(url, payload, headers=headers, timeout_sec=timeout_sec_int)
        if not _should_retry_response(resp) or attempt == attempts:
            break
        _sleep_backoff(attempt, retry_base_delay_sec)

    player_url = resp.get("playerUrl") or resp.get("player_url")
    legacy_preview_url = resp.get("previewUrl") or resp.get("preview_url")

    # Backward-compat: keep preview_url field, but prefer player_url when present.
    preview_url = player_url or legacy_preview_url

    preview_allowed = resp.get("previewAllowed")
    preview_blocked_reason = resp.get("previewBlockedReason")

    # Base success criteria.
    # Prefer server's explicit ok/success field when present (Player endpoints return ok).
    server_ok = resp.get("ok")
    if server_ok is None:
        server_ok = resp.get("success")
    if isinstance(server_ok, bool):
        base_ok = bool(server_ok) and bool(preview_url) and not resp.get("_http_error")
    else:
        base_ok = bool(preview_url) and not resp.get("_http_error") and (preview_allowed is not False)

    # Studio mode has extra reachability probes; Player mode does not.
    if player_url:
        dev_ok = True
    else:
        dev_ok = not (resp.get("devServerReachable") is False or resp.get("devBundleReachable") is False)

    ok = bool(base_ok and dev_ok)

    error: Optional[str] = None
    next_step: Optional[str] = None

    # 1) Request-level errors
    if resp.get("_http_error"):
        http_status = resp.get("_http_status")
        err_type = resp.get("_error_type") or "http"
        err_detail = _truncate(resp.get("_error") or resp.get("error") or resp.get("message") or resp.get("raw"))

        if http_status == 401:
            error = "unauthorized (401)"
            next_step = "The server has API_TOKEN enabled. Pass api_token (raw token or 'Bearer <token>') and retry."
        elif err_type == "network":
            error = f"network error: {err_detail or 'unknown'}"
            next_step = (
                "Check server_base_url reachability (network/firewall/allowlist), port 7780, and whether the server expects http vs https."
            )
        else:
            error = f"request failed ({err_type}): HTTP {http_status}"
            next_step = (
                "Verify request payload: json_string must be valid JSON containing a non-empty `scenes` array; code_array must be a non-empty list of TSX strings. "
                f"Server detail: {err_detail or 'n/a'}"
            )

        out = {
            "ok": False,
            "preview_url": None,
            "player_url": None,
            "project_id": resp.get("projectId") or resp.get("project_id"),
            "bundle_status": resp.get("bundleStatus") or resp.get("bundle_status"),
            "mode": resp.get("mode"),
            "error": error,
            "next_step": next_step,
        }
        if include_sandbox_id:
            out["sandbox_id"] = resp.get("sandboxId")
        return out

    # 2) Preview blocked by fatal preflight
    if preview_allowed is False:
        scene_error, scene_next = _build_scene_focused_guidance(resp)
        error = scene_error or f"preview blocked: {preview_blocked_reason or 'unknown'}"
        next_step = (
                scene_next
                or "Fix the failing scene based on preflight output (common: missing `export default function`, wrong import path, missing component). Then push again."
        )
        out = {
            "ok": False,
            "preview_url": None,
            "player_url": None,
            "project_id": resp.get("projectId") or resp.get("project_id"),
            "bundle_status": resp.get("bundleStatus") or resp.get("bundle_status"),
            "mode": resp.get("mode"),
            "error": error,
            "next_step": next_step,
        }
        if include_sandbox_id:
            out["sandbox_id"] = resp.get("sandboxId")
        return out

    # 3) No preview url
    if not preview_url:
        scene_error, scene_next = _build_scene_focused_guidance(resp)
        error = scene_error or "missing previewUrl in response"
        next_step = (
                scene_next
                or "Try wait_for_ready=True. If still missing, validate the same payload via the /admin panel and inspect server logs for details."
        )
        out = {
            "ok": False,
            "preview_url": None,
            "player_url": None,
            "project_id": resp.get("projectId") or resp.get("project_id"),
            "bundle_status": resp.get("bundleStatus") or resp.get("bundle_status"),
            "mode": resp.get("mode"),
            "error": error,
            "next_step": next_step,
        }
        if include_sandbox_id:
            out["sandbox_id"] = resp.get("sandboxId")
        return out

    # 4) previewUrl returned but not reachable yet (white screen cases)
    if not dev_ok:
        s1 = resp.get("devServerStatus")
        s2 = resp.get("devBundleStatus")
        error = "previewUrl returned but not reachable yet"
        next_step = (
            "The server returned preview_url, but the public endpoint is not ready. "
            f"Probe status: /={s1}, /bundle.js={s2}. "
            "Set wait_for_ready=True and retry, or wait a few seconds then open the preview_url again. "
            "If it keeps happening, omit sandbox_id to create a fresh sandbox."
        )

    out = {
        "ok": bool(ok),
        "preview_url": preview_url,
        "player_url": player_url,
        "project_id": resp.get("projectId") or resp.get("project_id"),
        "bundle_status": resp.get("bundleStatus") or resp.get("bundle_status"),
        "mode": resp.get("mode"),
        "error": error,
        "next_step": next_step,
    }
    if include_sandbox_id:
        out["sandbox_id"] = resp.get("sandboxId")
    return out
