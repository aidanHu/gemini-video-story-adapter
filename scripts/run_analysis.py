#!/usr/bin/env python3
import argparse
import atexit
import base64
import json
import mimetypes
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = "https://yunwu.ai"
DEFAULT_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.1-pro-preview"
INLINE_LIMIT_BYTES = 20 * 1024 * 1024
SHOT_LABEL_RE = re.compile(r"\b(?:shot|Shot)\s*\d+\b|镜头\s*\d+|片段\s*\d+|s\d+\b")
ASSET_TAG_RE = re.compile(r"@\S+|--ref\s+\S+")

# 智能重试配置
DEFAULT_MAX_RETRIES = 0  # 重型多模态请求默认不自动重试，避免重复计费
RETRY_BACKOFF_SECONDS = [10, 30]  # 指数退避等待时间
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# 心跳间隔（秒）
HEARTBEAT_INTERVAL = 10


class AnalysisTimeoutError(Exception):
    """等待分析结果超时，状态不明确，避免自动重提。"""
    def __init__(self, message: str, request_meta: dict | None = None):
        super().__init__(message)
        self.request_meta = request_meta or {}


class AnalysisRequestError(Exception):
    """请求失败，但尽量保留服务端 request-id 等调试信息。"""
    def __init__(self, message: str, request_meta: dict | None = None):
        super().__init__(message)
        self.request_meta = request_meta or {}


# ---------------------------------------------------------------------------
# Lock File 防重入机制
# ---------------------------------------------------------------------------

_lock_path: str | None = None


def _resolve_lock_path(output_path: str | None) -> Path:
    """根据 --output 参数确定 lock 文件位置。"""
    if output_path:
        return Path(output_path).parent / ".run_analysis.lock"
    return Path.cwd() / ".run_analysis.lock"


def acquire_lock(output_path: str | None) -> None:
    """尝试获取 lock，如果已有同名活跃进程则拒绝启动。"""
    global _lock_path
    lock = _resolve_lock_path(output_path)
    if lock.exists():
        try:
            old_pid = int(lock.read_text().strip())
            # 检查进程是否仍在运行（发送信号 0 不会杀死进程）
            os.kill(old_pid, 0)
            print(
                f"error: 另一个 run_analysis 实例 (PID {old_pid}) 正在运行中。"
                f"如需强制重新运行，请先删除 {lock}",
                file=sys.stderr,
            )
            sys.exit(2)
        except (ProcessLookupError, ValueError):
            # 旧进程已不存在或 lock 内容无效，安全地覆盖
            pass
    lock.write_text(str(os.getpid()))
    _lock_path = str(lock)
    atexit.register(_release_lock)


def _release_lock() -> None:
    """进程退出时自动清理 lock 文件。"""
    if _lock_path:
        try:
            Path(_lock_path).unlink(missing_ok=True)
        except OSError:
            pass


def parse_args() -> argparse.Namespace:
    env_base_url = os.getenv("YUNWU_BASE_URL") or os.getenv("GEMINI_BASE_URL") or DEFAULT_BASE_URL
    parser = argparse.ArgumentParser(
        description="Build and optionally send a Gemini-native video adaptation request."
    )
    parser.add_argument("--video", help="Local path or remote URL to the source video.")
    parser.add_argument(
        "--video-file-uri",
        help="Existing Gemini file URI to use as file_data instead of inline_data.",
    )
    parser.add_argument(
        "--brief",
        help="Adaptation brief text. Use with --brief-file or alone.",
        default="",
    )
    parser.add_argument(
        "--brief-file",
        help="Path to a text file containing the adaptation brief.",
    )
    parser.add_argument(
        "--reference",
        action="append",
        default=[],
        help="Local path or remote URL to a reference image. Repeatable.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--output-profile",
        choices=["compact", "full"],
        default="compact",
        help="compact saves tokens by returning only core fields; full keeps all rich fields.",
    )
    parser.add_argument(
        "--base-url",
        default=env_base_url,
        help="API base URL. Defaults to YUNWU_BASE_URL, GEMINI_BASE_URL, or https://yunwu.ai.",
    )
    parser.add_argument(
        "--token",
        help="API token. Defaults to YUNWU_API_TOKEN or GEMINI_API_TOKEN.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=600,
        help="HTTP request timeout seconds. Use a longer timeout for heavy video analysis.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="Automatic retry count for 429/5xx/network errors. Default 0 to avoid ambiguous duplicate submissions.",
    )
    parser.add_argument(
        "--output",
        help="Path to write the final JSON result.",
    )
    return parser.parse_args()


def is_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"}


def load_bytes(source: str) -> bytes:
    if is_url(source):
        with urllib.request.urlopen(source) as response:
            return response.read()
    return Path(source).read_bytes()


def detect_mime(source: str) -> str:
    mime, _ = mimetypes.guess_type(source)
    if mime:
        return mime
    if is_url(source):
        path = urllib.parse.urlparse(source).path
        mime, _ = mimetypes.guess_type(path)
        if mime:
            return mime
    return "application/octet-stream"


def inline_part(source: str, expected_prefix: str) -> dict:
    payload = load_bytes(source)
    if len(payload) > INLINE_LIMIT_BYTES:
        raise ValueError(
            f"{source} is {len(payload)} bytes, above the inline limit of {INLINE_LIMIT_BYTES}."
        )
    mime = detect_mime(source)
    if not mime.startswith(expected_prefix):
        raise ValueError(f"{source} has mime type {mime}, expected prefix {expected_prefix}.")
    return {
        "inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(payload).decode("ascii"),
        }
    }


def build_seedance_prompt(brief: str, references: list[str], output_profile: str) -> str:
    """构建 Seedance 分析 prompt，仅支持 single-pass 模式。"""
    reference_lines = "\n".join(
        f"- @图片{idx + 1}: treat this as a user-supplied visual reference anchor"
        for idx, _ in enumerate(references)
    )
    phase_instruction = (
        "Return the full package in one response split into three top-level JSON blocks: "
        "asset_json, storyboard_json, and video_prompts_json."
    )
    compact_instruction = (
        "Output profile is COMPACT: keep every field concise, avoid repetition, and use short practical wording."
        if output_profile == "compact"
        else "Output profile is FULL: include rich details where useful."
    )
    return (
        "Role: Seedance 2.0 image-to-video architect for realistic cinematic remakes.\n"
        "Target platform: Seedance 2.0. Output must obey strict physical logic, stable background logic, and SCELA prompt methodology.\n\n"
        "Output contract:\n"
        "- Return exactly three top-level JSON blocks: asset_json, storyboard_json, video_prompts_json.\n"
        "- Keep JSON keys in English and all string values in Simplified Chinese.\n"
        "- Do not add extra top-level sections.\n\n"
        "Canonical asset rules:\n"
        "- Use compact internal asset tags like @角色A / @角色B / @道具A / @场景A.\n"
        "- One real entity maps to one canonical asset tag.\n"
        "- Do not split the same person, prop, or scene into multiple assets because of temporary state changes.\n"
        "- Temporary state changes such as expression, pose, held/not-held, open/closed, damaged/clean, lighting shift, framing shift, or local room-angle change must stay in shot-level prompts.\n"
        "- Every character, prop, and scene must have an independent asset definition block.\n"
        "- Never reference an @asset in shots unless it is defined in the asset library.\n"
        "- Use explicit source names when the source clearly provides them; otherwise use stable generic labels like 角色A / 角色B / 道具A / 场景A consistently.\n"
        "- Do not expose internal @tags in final storyboard text fields.\n\n"
        "Asset layout rules:\n"
        "- All asset images must be 16:9 landscape layouts.\n"
        "- Character assets: front / side / back full-body three-view.\n"
        "- Prop assets: front / side / back three-view.\n"
        "- Scene assets: panorama / bird's-eye / close-up detail three-view.\n"
        "- Character and prop assets must use pure white background (#FFFFFF) with no environment elements.\n"
        "- Scene assets must not contain any people, body parts, silhouettes, reflections of people, or character-presence cues.\n"
        "- In character/prop asset layout and full_prompt_string, explicitly include 纯白背景/#FFFFFF/无环境元素, 三视图, and 各视图绝对不能重叠.\n"
        "- Character assets must provide concrete wardrobe_design, makeup_design, and accessory_design fields.\n"
        "- Do not describe hair traits in asset prompts or storyboard prompts. Hair-related words are forbidden: 发型, 发色, 长发, 短发, 卷发, 直发, 马尾, 刘海, 紫发, 红发, 黑发, 蓝发, 银发, 粉发.\n"
        "- Do not add gender, age, or body-shape labels.\n"
        "- Global asset baseline: bright lighting and vivid rich colors.\n\n"
        "Storyboard rules:\n"
        "- Every storyboard shot must explicitly include scene context and used props/assets.\n"
        "- used_props must never be empty; use ['无道具'] when needed.\n"
        "- full_prompt_string is required per shot and must start with '(单张全屏，严禁拼图，无边框，电影定格单帧)'.\n"
        "- first_frame_prompt must describe the t=0 visible state, not a mid-action extreme.\n"
        "- Character descriptions in storyboard text must stay consistent with the corresponding asset definitions.\n"
        "- scela_prompt must be an object with subject, camera, effect, and audio.\n"
        "- Do not include shot-number labels like Shot 1 / 镜头1 / s1 in prompt text fields.\n"
        "- Every shot prompt must be standalone and reusable outside the conversation.\n"
        "- Lighting must stay bright, readable, front-lit or side-lit unless the brief explicitly overrides it.\n\n"
        "Video prompt rules:\n"
        "- Output video_prompts_json by shot, not by scene.\n"
        "- Each item must contain shot_id, scela_prompt, dialogue, and audio.\n"
        "- scela_prompt should be a concise standalone natural-language paragraph describing motion from the first frame.\n"
        "- Dialogue and audio stay as separate fields rather than being merged into one paragraph.\n"
        "- Prefer visible acting details over abstract emotion labels.\n"
        "- Explicitly describe facial changes, eye focus shifts, eyebrow movement, mouth changes, jaw tension, posture changes, hesitation, recoil, leaning, freezing, and emotional transition.\n"
        "- Keep camera movement and visual priority inside scela_prompt.\n\n"
        "SCELA rules:\n"
        "- S Subject: identity, wardrobe, pose, action potential.\n"
        "- C Camera: scale, movement, angle, focus.\n"
        "- E Effect: specific visible effect only when needed.\n"
        "- A Audio: ambient and key effects on separate structured fields.\n"
        "- Do not output a separate light/look field.\n\n"
        "Narrative rules:\n"
        "- Identify what the source video is doing now.\n"
        "- Explain how the remake should differ.\n"
        "- Preserve only the source details that still matter.\n"
        "- Expose uncertainty and assumptions explicitly.\n"
        "- Make environments narratively active, not just named.\n"
        "- Make poses reveal intention.\n"
        "- Split shots when multiple action nodes would reduce controllability.\n\n"
        "Reference anchors:\n"
        f"{reference_lines or '- none provided'}\n\n"
        f"{compact_instruction}\n\n"
        f"{phase_instruction}\n\n"
        "User brief:\n"
        f"{brief.strip()}"
    )


def build_schema(output_profile: str) -> dict:
    """构建完整的 single-pass response schema。"""
    scela_item = {
        "type": "OBJECT",
        "required": ["subject", "camera", "effect", "audio"],
        "properties": {
            "subject": {"type": "STRING"},
            "camera": {"type": "STRING"},
            "effect": {"type": "STRING"},
            "audio": {"type": "STRING"},
        },
    }
    video_prompt_item = {
        "type": "OBJECT",
        "required": ["shot_id", "scela_prompt", "dialogue", "audio"],
        "properties": {
            "shot_id": {"type": "STRING"},
            "scela_prompt": {"type": "STRING"},
            "dialogue": {"type": "STRING"},
            "audio": {"type": "STRING"},
        },
    }
    # 资产库 schema
    if output_profile == "compact":
        asset_item = {
            "type": "OBJECT",
            "required": ["asset_tag", "asset_category", "visual_anchor", "layout", "full_prompt_string"],
            "properties": {
                "asset_tag": {"type": "STRING"},
                "asset_category": {"type": "STRING"},
                "visual_anchor": {"type": "STRING"},
                "layout": {"type": "STRING"},
                "full_prompt_string": {"type": "STRING"},
                "wardrobe_design": {"type": "STRING"},
                "makeup_design": {"type": "STRING"},
                "accessory_design": {"type": "STRING"},
            },
        }
        story_outline = {
            "type": "OBJECT",
            "required": ["premise", "beat_outline"],
            "properties": {
                "premise": {"type": "STRING"},
                "beat_outline": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }
        shot_required = [
            "shot_id", "duration_seconds", "scene_tag", "scene_description",
            "used_asset_tags", "used_props",
            "full_prompt_string", "first_frame_prompt", "scela_prompt",
            "dialogue", "audio",
        ]
        shot_properties = {
            "shot_id": {"type": "STRING"},
            "duration_seconds": {"type": "NUMBER"},
            "scene_tag": {"type": "STRING"},
            "scene_description": {"type": "STRING"},
            "used_asset_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "used_props": {"type": "ARRAY", "items": {"type": "STRING"}},
            "full_prompt_string": {"type": "STRING"},
            "first_frame_prompt": {"type": "STRING"},
            "scela_prompt": scela_item,
            "dialogue": {"type": "STRING"},
            "audio": {"type": "STRING"},
            "referenced_assets": {"type": "ARRAY", "items": {"type": "STRING"}},
        }
        asset_json_required = ["story_adaptation_outline", "asset_library", "asset_layout_rules"]
    else:
        asset_item = {
            "type": "OBJECT",
            "required": ["asset_tag", "asset_category", "visual_anchor", "layout", "full_prompt_string"],
            "properties": {
                "asset_tag": {"type": "STRING"},
                "asset_category": {"type": "STRING"},
                "visual_anchor": {"type": "STRING"},
                "motion_potential": {"type": "STRING"},
                "material_details": {"type": "ARRAY", "items": {"type": "STRING"}},
                "environment_details": {"type": "ARRAY", "items": {"type": "STRING"}},
                "layout": {"type": "STRING"},
                "full_prompt_string": {"type": "STRING"},
                "wardrobe_design": {"type": "STRING"},
                "makeup_design": {"type": "STRING"},
                "accessory_design": {"type": "STRING"},
            },
        }
        story_outline = {
            "type": "OBJECT",
            "required": ["premise", "micro_innovation_strategy", "beat_outline"],
            "properties": {
                "premise": {"type": "STRING"},
                "micro_innovation_strategy": {"type": "STRING"},
                "preserve_from_benchmark": {"type": "ARRAY", "items": {"type": "STRING"}},
                "replace_from_benchmark": {"type": "ARRAY", "items": {"type": "STRING"}},
                "beat_outline": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }
        shot_required = [
            "shot_id", "theme", "duration_seconds", "scene_tag", "scene_description",
            "used_asset_tags", "used_props",
            "aspect_ratio", "narrative_mode",
            "full_prompt_string", "first_frame_prompt", "scela_prompt",
            "dialogue", "audio",
        ]
        shot_properties = {
            "shot_id": {"type": "STRING"},
            "theme": {"type": "STRING"},
            "duration_seconds": {"type": "NUMBER"},
            "scene_tag": {"type": "STRING"},
            "scene_description": {"type": "STRING"},
            "used_asset_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "used_props": {"type": "ARRAY", "items": {"type": "STRING"}},
            "aspect_ratio": {"type": "STRING"},
            "narrative_mode": {"type": "STRING"},
            "full_prompt_string": {"type": "STRING"},
            "benchmark_inheritance": {"type": "ARRAY", "items": {"type": "STRING"}},
            "override_notes": {"type": "ARRAY", "items": {"type": "STRING"}},
            "first_frame_prompt": {"type": "STRING"},
            "scela_prompt": scela_item,
            "dialogue": {"type": "STRING"},
            "audio": {"type": "STRING"},
            "referenced_assets": {"type": "ARRAY", "items": {"type": "STRING"}},
        }
        asset_json_required = [
            "global_visual_definition", "story_adaptation_outline", "asset_library", "asset_layout_rules",
        ]

    asset_json_properties = {
        "story_adaptation_outline": story_outline,
        "asset_library": {"type": "ARRAY", "items": asset_item},
        "asset_layout_rules": {"type": "ARRAY", "items": {"type": "STRING"}},
    }
    if output_profile != "compact":
        asset_json_properties["global_visual_definition"] = {
            "type": "OBJECT",
            "required": ["target_platform", "visual_style", "narrative_mode", "runtime_strategy", "continuity_rules"],
            "properties": {
                "target_platform": {"type": "STRING"},
                "visual_style": {"type": "STRING"},
                "narrative_mode": {"type": "STRING"},
                "runtime_strategy": {"type": "STRING"},
                "continuity_rules": {"type": "ARRAY", "items": {"type": "STRING"}},
                "compliance_notes": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }

    top_properties = {
        "asset_json": {
            "type": "OBJECT",
            "required": asset_json_required,
            "properties": asset_json_properties,
        },
        "storyboard_json": {
            "type": "OBJECT",
            "required": ["storyboard_script", "voiceover_script"],
            "properties": {
                "storyboard_script": {
                    "type": "ARRAY",
                    "items": {"type": "OBJECT", "required": shot_required, "properties": shot_properties},
                },
                "voiceover_script": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "required": ["shot_id", "line"],
                        "properties": {"shot_id": {"type": "STRING"}, "line": {"type": "STRING"}},
                    },
                },
            },
        },
        "video_prompts_json": {
            "type": "OBJECT",
            "required": ["video_prompts"],
            "properties": {
                "video_prompts": {
                    "type": "ARRAY",
                    "items": video_prompt_item,
                }
            },
        },
    }
    return {
        "type": "OBJECT",
        "required": ["asset_json", "storyboard_json", "video_prompts_json"],
        "properties": top_properties,
    }


def build_request(args: argparse.Namespace) -> dict:
    brief_parts = []
    if args.brief:
        brief_parts.append(args.brief.strip())
    if args.brief_file:
        brief_parts.append(Path(args.brief_file).read_text(encoding="utf-8").strip())
    brief = "\n\n".join(part for part in brief_parts if part)
    if not brief:
        raise ValueError("Provide --brief or --brief-file.")

    if not args.video and not args.video_file_uri:
        raise ValueError("Provide --video or --video-file-uri.")

    parts = []
    if args.video_file_uri:
        parts.append(
            {
                "file_data": {
                    "mime_type": "video/mp4",
                    "file_uri": args.video_file_uri,
                }
            }
        )
    elif args.video and is_url(args.video):
        parts.append(
            {
                "file_data": {
                    "mime_type": "video/mp4",
                    "file_uri": args.video,
                }
            }
        )
    else:
        parts.append(inline_part(args.video, "video/"))

    for reference in args.reference:
        parts.append(inline_part(reference, "image/"))

    parts.append(
        {
            "text": build_seedance_prompt(brief, args.reference, args.output_profile)
        }
    )

    response_schema = build_schema(args.output_profile)

    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "temperature": 0.6,
        },
    }


def build_renderer_bridge_payloads(payload: dict) -> tuple[dict, dict, dict] | None:
    if not isinstance(payload, dict):
        return None
    asset_json = payload.get("asset_json")
    storyboard_json = payload.get("storyboard_json")
    video_prompts_json = payload.get("video_prompts_json")
    if not isinstance(asset_json, dict) or not isinstance(storyboard_json, dict) or not isinstance(video_prompts_json, dict):
        return None

    assets_payload = {
        "asset_library": list(asset_json.get("asset_library", [])) if isinstance(asset_json.get("asset_library"), list) else []
    }
    global_visual_definition = asset_json.get("global_visual_definition")
    if isinstance(global_visual_definition, dict):
        visual_style = str(global_visual_definition.get("visual_style", "")).strip()
        if visual_style:
            assets_payload["style_descriptor"] = visual_style

    storyboard_payload = {
        "storyboard_script": list(storyboard_json.get("storyboard_script", [])) if isinstance(storyboard_json.get("storyboard_script"), list) else []
    }
    raw_video_prompts = video_prompts_json.get("video_prompts")
    image_to_video_payload = {
        "video_prompts": list(raw_video_prompts) if isinstance(raw_video_prompts, list) else []
    }
    return assets_payload, storyboard_payload, image_to_video_payload


def write_renderer_bridge_files(path: str, payload: dict) -> None:
    bridge = build_renderer_bridge_payloads(payload)
    if not bridge:
        return
    assets_payload, storyboard_payload, image_to_video_payload = bridge
    output_path = Path(path)
    output_dir = output_path.parent if output_path.suffix else output_path
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "assets.json").write_text(json.dumps(assets_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "storyboard.json").write_text(json.dumps(storyboard_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "video_prompts.json").write_text(json.dumps(image_to_video_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_output(path: str | None, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        write_renderer_bridge_files(path, payload)
    else:
        print(text)


def build_result_envelope(
    *,
    status: str,
    success: bool,
    failure_reason: str = "",
    should_retry: bool | None = None,
    retry_guidance: str = "",
    result: dict | None = None,
    request_meta: dict | None = None,
) -> dict:
    payload = dict(result or {})
    payload["analysis_status"] = status
    payload["success"] = success
    payload["failure_reason"] = failure_reason
    payload["should_retry"] = should_retry
    payload["retry_guidance"] = retry_guidance
    payload["server_request_ids"] = dict(request_meta or {})
    return payload


def extract_request_meta(headers) -> dict:
    if not headers:
        return {}
    candidates = [
        "request-id",
        "x-request-id",
        "x-requestid",
        "trace-id",
        "x-trace-id",
        "traceparent",
        "cf-ray",
        "x-b3-traceid",
    ]
    out = {}
    for key in candidates:
        value = headers.get(key)
        if value:
            out[key] = value
    return out


def send_request(args: argparse.Namespace, payload: dict) -> tuple[dict, dict]:
    """发送 API 请求，带心跳输出和基于 HTTP 状态码的智能重试。"""
    token = args.token or os.getenv("YUNWU_API_TOKEN") or os.getenv("GEMINI_API_TOKEN")
    if not token:
        raise ValueError("Missing token. Set --token or YUNWU_API_TOKEN.")

    url = f"{args.base_url.rstrip('/')}/v1beta/models/{args.model}:generateContent"
    data_bytes = json.dumps(payload).encode("utf-8")
    max_retries = max(0, int(getattr(args, "max_retries", DEFAULT_MAX_RETRIES)))

    for attempt in range(1 + max_retries):
        req = urllib.request.Request(
            url=url,
            data=data_bytes,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

        # ---- 心跳线程：每 HEARTBEAT_INTERVAL 秒输出一行状态 ----
        stop_heartbeat = threading.Event()
        start_time = time.time()

        def _heartbeat_loop():
            while not stop_heartbeat.is_set():
                stop_heartbeat.wait(HEARTBEAT_INTERVAL)
                if not stop_heartbeat.is_set():
                    elapsed = int(time.time() - start_time)
                    print(
                        f"[heartbeat] 正在等待 API 响应... (已等待 {elapsed}s)",
                        file=sys.stderr,
                        flush=True,
                    )

        heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        try:
            with urllib.request.urlopen(req, timeout=args.request_timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                request_meta = extract_request_meta(response.headers)
                elapsed = int(time.time() - start_time)
                print(
                    f"[info] API 请求成功 (耗时 {elapsed}s)",
                    file=sys.stderr,
                    flush=True,
                )
                return result, request_meta

        except urllib.error.HTTPError as exc:
            status = exc.code
            elapsed = int(time.time() - start_time)
            request_meta = extract_request_meta(exc.headers)

            # 不可重试的客户端错误
            if status not in RETRYABLE_STATUS_CODES:
                print(
                    f"[error] API 返回 HTTP {status}，属于不可重试错误，立即终止。(耗时 {elapsed}s)",
                    file=sys.stderr,
                    flush=True,
                )
                raise AnalysisRequestError(
                    f"API request failed with HTTP {status}: {exc.read().decode('utf-8', errors='replace')}",
                    request_meta=request_meta,
                ) from exc

            # 可重试的服务端错误 / 频率限制
            if attempt < max_retries:
                # 优先使用 Retry-After 头（429 场景）
                retry_after = exc.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                else:
                    wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                print(
                    f"[warn] API 返回 HTTP {status}，将在 {wait}s 后重试 "
                    f"(第 {attempt + 1}/{max_retries} 次重试，已等待 {elapsed}s)",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait)
            else:
                print(
                    f"[error] API 返回 HTTP {status}，已达到最大重试次数 ({max_retries})，终止。",
                    file=sys.stderr,
                    flush=True,
                )
                raise AnalysisRequestError(
                    f"API request failed after {max_retries} retries with HTTP {status}",
                    request_meta=request_meta,
                ) from exc

        except urllib.error.URLError as exc:
            elapsed = int(time.time() - start_time)
            reason = exc.reason
            ambiguous = isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()
            if attempt < max_retries and not ambiguous:
                wait = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
                print(
                    f"[warn] 网络错误: {exc.reason}，将在 {wait}s 后重试 "
                    f"(第 {attempt + 1}/{max_retries} 次重试，已等待 {elapsed}s)",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait)
            else:
                if ambiguous:
                    raise AnalysisTimeoutError(
                        "Request timed out before a response was received. The server may still be processing it.",
                        request_meta={},
                    ) from exc
                raise AnalysisRequestError(
                    f"Network error after {max_retries} retries: {exc.reason}",
                    request_meta={},
                ) from exc

        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2)

    # 理论上不会执行到这里
    raise ValueError("Unexpected: exhausted all retry attempts.")


def parse_json_text(text: str) -> dict | None:
    raw = text.strip()
    try:
        return json.loads(raw)
    except Exception:
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except Exception:
            return None
    return None


def unwrap_response_json(response: dict) -> dict:
    if not isinstance(response, dict) or "candidates" not in response:
        return response
    try:
        candidates = response.get("candidates") or []
        if not candidates:
            return response
        parts = candidates[0].get("content", {}).get("parts", [])
        for part in parts:
            text = part.get("text")
            if not isinstance(text, str):
                continue
            parsed = parse_json_text(text)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        return response
    return response



def normalize_structured_output(result: dict) -> dict:
    if not isinstance(result, dict):
        return result

    # Backward compatibility: fold legacy flat output into the new three-block structure.
    if "asset_json" not in result:
        asset_json = {}
        for key in ["global_visual_definition", "story_adaptation_outline", "asset_library", "asset_layout_rules"]:
            if key in result:
                asset_json[key] = result.get(key)
        result["asset_json"] = asset_json
    if "storyboard_json" not in result:
        storyboard_json = {}
        for key in ["storyboard_script", "voiceover_script"]:
            if key in result:
                storyboard_json[key] = result.get(key)
        result["storyboard_json"] = storyboard_json
    if "video_prompts_json" not in result:
        result["video_prompts_json"] = {}

    asset_json = result.get("asset_json")
    if not isinstance(asset_json, dict):
        asset_json = {}
        result["asset_json"] = asset_json
    storyboard_json = result.get("storyboard_json")
    if not isinstance(storyboard_json, dict):
        storyboard_json = {}
        result["storyboard_json"] = storyboard_json
    video_prompts_json = result.get("video_prompts_json")
    if not isinstance(video_prompts_json, dict):
        video_prompts_json = {}
        result["video_prompts_json"] = video_prompts_json

    assets = asset_json.get("asset_library")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            tag = str(asset.get("asset_tag", ""))
            category = str(asset.get("asset_category", ""))
            layout = str(asset.get("layout", ""))
            full_prompt = str(asset.get("full_prompt_string", ""))

            is_role = tag.startswith("@角色") or ("角色" in category)
            is_prop = tag.startswith("@道具") or ("道具" in category)
            is_scene = tag.startswith("@场景") or ("场景" in category)

            # Ensure role/prop assets keep clean white-background sheets for stable downstream generation.
            if is_role or is_prop:
                white_bg_phrase = "纯白背景（#FFFFFF），无环境元素"
                three_view_phrase = (
                    "三视图（正面全身、侧面全身、背面全身），各视图绝对不能重叠"
                    if is_role
                    else "三视图（正面、侧面、背面），各视图绝对不能重叠"
                )
                if white_bg_phrase not in layout:
                    asset["layout"] = f"{layout}。{white_bg_phrase}".strip("。")
                if three_view_phrase not in asset["layout"]:
                    asset["layout"] = f"{asset['layout']}。{three_view_phrase}".strip("。")
                if white_bg_phrase not in full_prompt:
                    asset["full_prompt_string"] = f"{full_prompt}，{white_bg_phrase}".strip("，")
                if three_view_phrase not in asset["full_prompt_string"]:
                    asset["full_prompt_string"] = f"{asset['full_prompt_string']}，{three_view_phrase}".strip("，")
                # Strip scene-rich wording from role/prop prompts to avoid conflicts with white-background assets.
                cleaned = str(asset.get("full_prompt_string", ""))
                for phrase in ["环境细节丰富", "丰富的环境细节", "场景内容丰富", "丰富场景细节", "场景细节丰富"]:
                    cleaned = cleaned.replace(f"，{phrase}", "").replace(f"。{phrase}", "")
                    cleaned = cleaned.replace(phrase, "")
                asset["full_prompt_string"] = cleaned.strip("，。 ")

            if is_scene:
                no_people_phrase = "空场景，无人物，无人体局部，无人物倒影"
                if no_people_phrase not in layout:
                    asset["layout"] = f"{layout}。{no_people_phrase}".strip("。")
                if no_people_phrase not in str(asset.get("full_prompt_string", "")):
                    asset["full_prompt_string"] = f"{str(asset.get('full_prompt_string', ''))}，{no_people_phrase}".strip("，")

            if is_prop:
                wearable_markers = ["泳衣", "服装", "裙", "衣", "裤", "鞋", "帽", "手套", "盔甲", "尾巴"]
                is_wearable_prop = any(marker in tag or marker in str(asset.get("visual_anchor", "")) for marker in wearable_markers)
                if is_wearable_prop:
                    no_human_phrase = "无人体模特，无手持展示，无衣架"
                    for key in ["layout", "full_prompt_string"]:
                        value = str(asset.get(key, ""))
                        value = value.replace("三视图（正面全身、侧面全身、背面全身），各视图绝对不能重叠", "三视图（正面、侧面、背面），各视图绝对不能重叠")
                        value = value.replace("正面全身", "正面").replace("侧面全身", "侧面").replace("背面全身", "背面")
                        if no_human_phrase not in value:
                            value = f"{value}。{no_human_phrase}".strip("。")
                        asset[key] = value

            if tag.startswith("@角色"):
                if not str(asset.get("wardrobe_design", "")).strip():
                    anchor = str(asset.get("visual_anchor", "")).strip()
                    asset["wardrobe_design"] = (
                        f"基于参考角色重构的写实服装方案，核心外观锚点：{anchor}。"
                        if anchor
                        else "基于参考角色重构的写实服装方案，强调层次与材质细节。"
                    )
                if not str(asset.get("makeup_design", "")).strip():
                    asset["makeup_design"] = "写实自然妆面，强调干净肤质、立体轮廓与镜头可读性。"
                if not str(asset.get("accessory_design", "")).strip():
                    asset["accessory_design"] = "简洁写实配饰方案，材质与服装统一，服务角色身份表达。"

    shots = storyboard_json.get("storyboard_script")
    if isinstance(shots, list):
        # Build deterministic role name mapping used in storyboard text.
        role_name_map: dict[str, str] = {}
        for asset in assets if isinstance(assets, list) else []:
            if not isinstance(asset, dict):
                continue
            tag = str(asset.get("asset_tag", ""))
            if tag.startswith("@角色") and tag not in role_name_map:
                role_name_map[tag] = tag[1:].split("_", 1)[0]

        def sanitize_story_text(value: str) -> str:
            text = str(value or "")
            if not text:
                return text
            # Remove shot numbering labels.
            text = SHOT_LABEL_RE.sub("", text)
            # Replace role tags with original character names.
            for tag, name in role_name_map.items():
                text = text.replace(tag, name)
            # Remove any residual @tags/--ref directives.
            text = ASSET_TAG_RE.sub("", text)
            # Clean duplicated punctuation and spaces.
            text = re.sub(r"\s{2,}", " ", text).strip(" ，。;；")
            return text

        def sanitize_scela(value) -> dict[str, str]:
            if isinstance(value, dict):
                raw = value
            else:
                raw_text = sanitize_story_text(str(value or ""))
                raw = {
                    "subject": raw_text,
                    "camera": "",
                    "effect": "",
                    "audio": "",
                }
            return {
                "subject": sanitize_story_text(str(raw.get("subject", ""))),
                "camera": sanitize_story_text(str(raw.get("camera", ""))),
                "effect": sanitize_story_text(str(raw.get("effect", ""))),
                "audio": sanitize_story_text(str(raw.get("audio", ""))),
            }

        def dedupe_keep_order(values: list[str]) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for value in values:
                key = value.strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(key)
            return out

        for idx, shot in enumerate(shots):
            if not isinstance(shot, dict):
                continue
            if not str(shot.get("scene_tag", "")).strip():
                shot["scene_tag"] = "@场景A"
            if not str(shot.get("scene_description", "")).strip():
                shot["scene_description"] = "场景描述缺失，需补充空间结构、光线状态与环境细节。"

            used_assets = shot.get("used_asset_tags")
            if not isinstance(used_assets, list):
                shot["used_asset_tags"] = []

            used_props = shot.get("used_props")
            if not isinstance(used_props, list) or len(used_props) == 0:
                shot["used_props"] = ["无道具"]

            # Enforce standalone single-frame prompt with fixed prefix.
            frame_prefix = "(单张全屏，严禁拼图，无边框，电影定格单帧)"
            fp = str(shot.get("full_prompt_string", "")).strip()
            if not fp:
                fp = str(shot.get("first_frame_prompt", "")).strip()
            fp = sanitize_story_text(fp)
            if fp.startswith(frame_prefix):
                shot["full_prompt_string"] = fp
            else:
                shot["full_prompt_string"] = f"{frame_prefix}{fp}"

            for key in [
                "scene_description",
                "first_frame_prompt",
                "dialogue",
                "audio",
            ]:
                shot[key] = sanitize_story_text(str(shot.get(key, "")))
            shot["scela_prompt"] = sanitize_scela(shot.get("scela_prompt"))

    video_prompts = video_prompts_json.get("video_prompts")
    if not isinstance(video_prompts, list):
        video_prompts = []
    if not video_prompts and isinstance(shots, list):
        generated_video_prompts = []
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            shot_id = str(shot.get("shot_id", "")).strip()
            if not shot_id:
                continue
            scela = sanitize_scela(shot.get("scela_prompt"))
            scela_text = "，".join(
                value for value in [
                    scela.get("subject", ""),
                    scela.get("camera", ""),
                    scela.get("effect", ""),
                ] if value
            ).strip("，。 ")
            if not scela_text:
                scela_text = sanitize_story_text(str(shot.get("first_frame_prompt", "")))
            generated_video_prompts.append(
                {
                    "shot_id": shot_id,
                    "scela_prompt": scela_text,
                    "dialogue": sanitize_story_text(str(shot.get("dialogue", ""))),
                    "audio": sanitize_story_text(str(shot.get("audio", ""))),
                }
            )
        video_prompts = generated_video_prompts
    else:
        normalized_video_prompts = []
        for item in video_prompts:
            if not isinstance(item, dict):
                continue
            shot_id = str(item.get("shot_id", "")).strip()
            if not shot_id:
                continue
            normalized_video_prompts.append(
                {
                    "shot_id": shot_id,
                    "scela_prompt": sanitize_story_text(str(item.get("scela_prompt", ""))),
                    "dialogue": sanitize_story_text(str(item.get("dialogue", ""))),
                    "audio": sanitize_story_text(str(item.get("audio", ""))),
                }
            )
        video_prompts = normalized_video_prompts
    video_prompts_json["video_prompts"] = video_prompts

    # Ensure voiceover lines satisfy zero-tag constraint while preserving original character names.
    voice_lines = storyboard_json.get("voiceover_script")
    if isinstance(voice_lines, list):
        for item in voice_lines:
            if not isinstance(item, dict):
                continue
            line = str(item.get("line", ""))
            line = SHOT_LABEL_RE.sub("", line)
            line = ASSET_TAG_RE.sub("", line)
            item["line"] = re.sub(r"\s{2,}", " ", line).strip(" ，。;；")

    return result


def main() -> int:
    args: argparse.Namespace | None = None
    try:
        args = parse_args()
        acquire_lock(args.output)
        payload = build_request(args)
        response, request_meta = send_request(args, payload)
        parsed = unwrap_response_json(response)
        # 仅使用本地后处理，不再发起二次 API 调用做中文修复
        parsed = normalize_structured_output(parsed)
        write_output(
            args.output,
            build_result_envelope(
                status="ok",
                success=True,
                should_retry=False,
                retry_guidance="",
                result=parsed,
                request_meta=request_meta,
            ),
        )
        return 0
    except AnalysisTimeoutError as exc:
        write_output(
            args.output if args else None,
            build_result_envelope(
                status="timeout",
                success=False,
                failure_reason=str(exc),
                should_retry=None,
                retry_guidance=(
                    "本次请求长时间未返回，服务端可能仍在处理中。先检查 provider 侧状态或稍后人工确认，"
                    "再决定是否重试，避免重复扣费。"
                ),
                request_meta=exc.request_meta,
            ),
        )
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except AnalysisRequestError as exc:
        write_output(
            args.output if args else None,
            build_result_envelope(
                status="failed",
                success=False,
                failure_reason=str(exc),
                should_retry=False,
                retry_guidance="先检查错误原因和服务端 request id，再决定是否重试。",
                request_meta=exc.request_meta,
            ),
        )
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        write_output(
            args.output if args else None,
            build_result_envelope(
                status="failed",
                success=False,
                failure_reason=str(exc),
                should_retry=False,
                retry_guidance="先修复错误原因后再重试；HTTP 4xx 或输入问题通常不应直接重试。",
                request_meta={},
            ),
        )
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
