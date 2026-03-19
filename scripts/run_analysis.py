#!/usr/bin/env python3
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_BASE_URL = "https://yunwu.ai"
DEFAULT_MODEL = "gemini-3.1-pro-preview"
INLINE_LIMIT_BYTES = 20 * 1024 * 1024
ENGLISH_WORD_RE = re.compile(r"[A-Za-z]{2,}")
SHOT_LABEL_RE = re.compile(r"\b(?:shot|Shot)\s*\d+\b|镜头\s*\d+|片段\s*\d+|s\d+\b")
ASSET_TAG_RE = re.compile(r"@\S+|--ref\s+\S+")


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
        "--proposal-file",
        help="Path to approved proposal JSON from phase 1. Used only when mode=two-phase and phase=execution.",
    )
    parser.add_argument(
        "--proposal-json",
        help="Approved proposal JSON string from phase 1. Alternative to --proposal-file.",
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
        "--mode",
        choices=["single", "two-phase"],
        default="single",
        help="single returns the full package in one response; two-phase splits proposal and execution.",
    )
    parser.add_argument(
        "--phase",
        choices=["full", "proposal", "execution"],
        default="full",
        help="full returns all structured content. proposal/execution are used with mode=two-phase.",
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
        "--output",
        help="Path to write the JSON request or response.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only build and print/write the request JSON.",
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


def build_prompt(brief: str, references: list[str]) -> str:
    reference_lines = "\n".join(
        f"- reference_image_{idx + 1}: use this to anchor visual continuity and style"
        for idx, _ in enumerate(references)
    )
    return (
        "You are a film development analyst and remake planner.\n"
        "Study the source video and produce a structured adaptation package.\n"
        "Respect the user's remake brief, preserve only the source details that still matter, "
        "and clearly label inferred assumptions.\n\n"
        "Required deliverable:\n"
        "- source_summary: core premise, emotional engine, current narrative shape\n"
        "- adaptation_strategy: what changes, what stays, why the remake works\n"
        "- characters: role, goals, conflicts, arc, visual design notes\n"
        "- scenes: ordered dramatic units with purpose, beats, and dependencies\n"
        "- assets: props, sets, wardrobe, vehicles, graphics, VFX, or environment elements\n"
        "- storyboard: shot-by-shot prompts for image generation and continuity\n"
        "- production_notes: assumptions, risks, open questions, style bible\n\n"
        "Storyboard prompts must be image-generation-ready and mention subject, environment, "
        "wardrobe or props, framing, camera language, lighting, mood, and continuity anchors.\n\n"
        f"Reference images:\n{reference_lines or '- none provided'}\n\n"
        f"User brief:\n{brief.strip()}"
    )


def load_proposal_context(args: argparse.Namespace) -> str:
    if args.proposal_json:
        return args.proposal_json.strip()
    if args.proposal_file:
        return Path(args.proposal_file).read_text(encoding="utf-8").strip()
    return ""


def build_seedance_prompt(
    brief: str, references: list[str], mode: str, phase: str, approved_proposal: str, output_profile: str
) -> str:
    reference_lines = "\n".join(
        f"- @图片{idx + 1}: treat this as a user-supplied visual reference anchor"
        for idx, _ in enumerate(references)
    )
    if mode == "single":
        phase_instruction = (
            "Current mode is SINGLE PASS. Return the full package in one response: "
            "global visual definition, story adaptation outline, asset library, asset layout rules, storyboard script, voiceover script, and validation report."
        )
    elif phase == "proposal":
        phase_instruction = (
            "Current phase is PHASE 1 PROPOSAL. Return only the proposal package. "
            "Do not output storyboard shots, dialogue script, or execution prose."
        )
    else:
        phase_instruction = (
            "Current phase is PHASE 2 EXECUTION. Return only storyboard and voiceover output. "
            "Treat the approved proposal JSON below as binding context. Do not contradict its asset tags, wording, or approved direction. "
            "Do not repeat the full proposal except where required for standalone shot prompts."
        )
    proposal_block = (
        ""
        if mode == "single" or phase == "proposal"
        else "\n\nApproved proposal JSON:\n"
        + (approved_proposal if approved_proposal else "{}")
    )
    compact_instruction = (
        "Output profile is COMPACT: keep every field concise, avoid repetition, and use short practical wording."
        if output_profile == "compact"
        else "Output profile is FULL: include rich details where useful."
    )
    return (
        "Role: Seedance 2.0 image-to-video architect for realistic cinematic remakes.\n"
        "Target platform: Seedance 2.0. Output must obey strict physical logic, stable background continuity, "
        "and SCELA prompt methodology.\n\n"
        "Non-negotiable rules:\n"
        "- Enforce two-phase workflow strictly.\n"
        "- Character mapping table (fixed visual identifiers): "
        "Rumi->紫发女人 | Mira->红发女人 | Zoey->黑发女人 | Jinu->黑发男人 | "
        "Abby->红发男人 | Baby saja->蓝发男人 | Mystery->银发男人 | Romance->粉发男人.\n"
        "- The mapping labels above are stable identifiers for asset anchoring, not literal hair or gender descriptions.\n"
        "- When user requests role replacement, resolve names via the mapping table first.\n"
        "- Use @角色_<原始名> as internal asset tags for library linkage; do not expose these tags in final storyboard text fields.\n"
        "- If benchmark video and micro-innovation notes are both provided, internally detect cuts and preserve shot count, shot scale, camera motion, angle, rhythm, and transitions unless the brief explicitly overrides a shot.\n"
        "- If benchmark first-frame screenshots are provided, use screenshot count as the final shot count authority.\n"
        "- Character names in final storyboard text output must be anonymized as 角色A/角色B... (no real names).\n"
        "- Do not describe hair traits in any output text fields, including asset visual anchors, full_prompt_string, first_frame_prompt, and scela_prompt.\n"
        "- Hair-related words are forbidden: 发型, 发色, 长发, 短发, 卷发, 直发, 马尾, 刘海, 紫发, 红发, 黑发, 蓝发, 银发, 粉发.\n"
        "- If a mapped identifier contains hair wording, treat it as internal alias only and do not render it literally in prompts.\n"
        "- Do not add gender, age, or body-shape labels.\n"
        "- For character assets, include explicit wardrobe/makeup/accessory design derived from the reference identity.\n"
        "- Character assets must provide wardrobe_design, makeup_design, and accessory_design as concrete standalone fields.\n"
        "- Every character, prop, and scene must have an independent asset definition block.\n"
        "- Never reference an @asset in shots unless it is defined in the asset library.\n"
        "- Every storyboard shot must explicitly include scene context and used props/assets.\n"
        "- Every storyboard shot must include continuity notes from previous shot state.\n"
        "- used_props must never be empty. If no prop is used in a shot, set used_props to ['无道具'].\n"
        "- continuity_from_prev must never be empty. For the first shot, write explicit start-state continuity text.\n"
        "- In every shot, both first_frame_prompt and scela_prompt must explicitly mention scene context and used props/assets.\n"
        "- Zero Tags: in final storyboard text fields, do not output any tag references like @角色_X, @Reference, --ref.\n"
        "- Zero Names: in final storyboard text fields, do not output concrete names; use 角色A/角色B... instead.\n"
        "- Zero Shot Labels: do not include Shot 1 / 镜头1 / s1 type labels in prompt text fields.\n"
        "- Storyboard full_prompt_string is required per shot and must start with '(单张全屏，严禁拼图，无边框，电影定格单帧)'.\n"
        "- All asset images must be 16:9 landscape layouts.\n"
        "- Character assets must be four-view sheets: front full-body, side full-body, back full-body, plus close-up of key visual feature.\n"
        "- Prop assets must be four-view sheets: front, side, back, plus close-up of key visual feature.\n"
        "- Scene assets must be three-view sheets: panorama, bird's-eye view, close-up detail.\n"
        "- Character and prop assets must use pure white background (#FFFFFF) with no environment elements.\n"
        "- In character/prop asset `layout` and `full_prompt_string`, explicitly include '纯白背景/#FFFFFF/无环境元素' and '四视图' and '各视图绝对不能重叠'.\n"
        "- Global look baseline for all assets: bright lighting and vivid rich colors.\n"
        "- Scene assets must emphasize visually rich scene content and rich environmental details.\n"
        "- Character/prop assets must keep pure white background with no scene/environment details.\n"
        "- In asset visual_anchor/layout/full_prompt_string, apply scene richness only to scene assets.\n"
        "- Perform full-coverage extraction before shots: enumerate every character (including extras), scene, and key prop from script text.\n"
        "- Single-entity constraint: each character asset block can define only one character entity.\n"
        "- Reference consistency redline: do not use undefined asset tags; run a full integrity self-check and add missing definitions immediately.\n"
        "- In every storyboard shot, inherit the same bright-and-vivid visual baseline unless the user explicitly overrides it.\n"
        "- Asset labels must be placed at top-left and must not overlap with the subject.\n"
        "- In each asset's `layout` and `full_prompt_string`, explicitly include the matching required view specification (角色/道具四视图，场景三视图).\n"
        "- First-frame prompts must describe t=0 state, not mid-action extremes.\n"
        "- Lighting must be bright, readable, front-lit or side-lit. Avoid backlight and rim-dominant setups.\n"
        "- Every shot prompt must be standalone and reusable outside the conversation. No shorthand like 'same as above'.\n"
        "- All output string values must be in Simplified Chinese, including dialogue, audio notes, and voiceover lines.\n"
        "- Keep JSON schema keys exactly as defined in English; only translate values.\n\n"
        "SCELA requirements:\n"
        "- S Subject: identity, wardrobe, pose, action potential\n"
        "- C Camera: scale, movement, angle, focus\n"
        "- E Effect: specific visible effect only when needed\n"
        "- L Light/Look: realistic cinematic lighting, color, texture\n"
        "- A Audio: ambient and key effects, on separate fields in structured output\n\n"
        "Narrative writing rules:\n"
        "- Make environments narratively active, not just named.\n"
        "- Make poses reveal intention.\n"
        "- Track prop state changes across shots.\n"
        "- Split shots when multiple action nodes would reduce controllability.\n\n"
        "Reference anchors:\n"
        f"{reference_lines or '- none provided'}\n\n"
        f"{compact_instruction}\n\n"
        f"{phase_instruction}\n\n"
        "User brief:\n"
        f"{brief.strip()}"
        f"{proposal_block}"
    )


def build_proposal_schema() -> dict:
    return {
        "type": "OBJECT",
        "required": [
            "phase",
            "global_visual_definition",
            "story_adaptation_outline",
            "asset_library",
            "asset_layout_rules",
            "approval_checkpoint",
        ],
        "properties": {
            "phase": {"type": "STRING", "enum": ["proposal"]},
            "global_visual_definition": {
                "type": "OBJECT",
                "required": [
                    "target_platform",
                    "visual_style",
                    "narrative_mode",
                    "runtime_strategy",
                    "continuity_rules",
                ],
                "properties": {
                    "target_platform": {"type": "STRING"},
                    "visual_style": {"type": "STRING"},
                    "narrative_mode": {"type": "STRING"},
                    "runtime_strategy": {"type": "STRING"},
                    "continuity_rules": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "compliance_notes": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
            "story_adaptation_outline": {
                "type": "OBJECT",
                "required": ["premise", "micro_innovation_strategy", "beat_outline"],
                "properties": {
                    "premise": {"type": "STRING"},
                    "micro_innovation_strategy": {"type": "STRING"},
                    "preserve_from_benchmark": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "replace_from_benchmark": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "beat_outline": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
            "asset_library": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "required": ["asset_tag", "asset_category", "visual_anchor", "layout", "full_prompt_string"],
                    "properties": {
                        "asset_tag": {"type": "STRING"},
                        "asset_category": {"type": "STRING"},
                        "visual_anchor": {"type": "STRING"},
                        "motion_potential": {"type": "STRING"},
                        "material_details": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                        },
                        "environment_details": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                        },
                        "layout": {"type": "STRING"},
                        "full_prompt_string": {"type": "STRING"},
                        "wardrobe_design": {"type": "STRING"},
                        "makeup_design": {"type": "STRING"},
                        "accessory_design": {"type": "STRING"},
                    },
                },
            },
            "asset_layout_rules": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "approval_checkpoint": {
                "type": "OBJECT",
                "required": ["status", "next_action"],
                "properties": {
                    "status": {"type": "STRING"},
                    "next_action": {"type": "STRING"},
                    "questions_for_user": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
        },
    }


def build_proposal_schema_compact() -> dict:
    return {
        "type": "OBJECT",
        "required": ["phase", "story_adaptation_outline", "asset_library", "asset_layout_rules", "approval_checkpoint"],
        "properties": {
            "phase": {"type": "STRING", "enum": ["proposal"]},
            "story_adaptation_outline": {
                "type": "OBJECT",
                "required": ["premise", "beat_outline"],
                "properties": {
                    "premise": {"type": "STRING"},
                    "beat_outline": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
            "asset_library": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "required": [
                        "asset_tag",
                        "asset_category",
                        "visual_anchor",
                        "layout",
                        "full_prompt_string",
                    ],
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
                },
            },
            "asset_layout_rules": {"type": "ARRAY", "items": {"type": "STRING"}},
            "approval_checkpoint": {
                "type": "OBJECT",
                "required": ["status", "next_action"],
                "properties": {
                    "status": {"type": "STRING"},
                    "next_action": {"type": "STRING"},
                },
            },
        },
    }


def build_execution_schema() -> dict:
    return {
        "type": "OBJECT",
        "required": ["phase", "storyboard_script", "voiceover_script", "validation_report"],
        "properties": {
            "phase": {"type": "STRING", "enum": ["execution"]},
            "storyboard_script": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "required": [
                        "shot_id",
                        "theme",
                        "duration_seconds",
                        "scene_tag",
                        "scene_description",
                        "used_asset_tags",
                        "used_props",
                        "continuity_from_prev",
                        "aspect_ratio",
                        "narrative_mode",
                        "full_prompt_string",
                        "first_frame_prompt",
                        "scela_prompt",
                        "dialogue",
                        "audio",
                    ],
                    "properties": {
                        "shot_id": {"type": "STRING"},
                        "theme": {"type": "STRING"},
                        "duration_seconds": {"type": "NUMBER"},
                        "scene_tag": {"type": "STRING"},
                        "scene_description": {"type": "STRING"},
                        "used_asset_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "used_props": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "continuity_from_prev": {"type": "STRING"},
                        "aspect_ratio": {"type": "STRING"},
                        "narrative_mode": {"type": "STRING"},
                        "full_prompt_string": {"type": "STRING"},
                        "benchmark_inheritance": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "override_notes": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "first_frame_prompt": {"type": "STRING"},
                        "scela_prompt": {"type": "STRING"},
                        "dialogue": {"type": "STRING"},
                        "audio": {"type": "STRING"},
                        "referenced_assets": {"type": "ARRAY", "items": {"type": "STRING"}},
                    },
                },
            },
            "voiceover_script": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "required": ["shot_id", "line"],
                    "properties": {
                        "shot_id": {"type": "STRING"},
                        "line": {"type": "STRING"},
                    },
                },
            },
            "validation_report": {
                "type": "OBJECT",
                "required": [
                    "undefined_asset_tags",
                    "missing_scene_context_shots",
                    "missing_prop_context_shots",
                    "shot_count_check",
                    "rule_violations",
                ],
                "properties": {
                    "undefined_asset_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "missing_scene_context_shots": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "missing_prop_context_shots": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "shot_count_check": {"type": "STRING"},
                    "rule_violations": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "assumptions": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
        },
    }


def build_execution_schema_compact() -> dict:
    return {
        "type": "OBJECT",
        "required": ["phase", "storyboard_script", "voiceover_script", "validation_report"],
        "properties": {
            "phase": {"type": "STRING", "enum": ["execution"]},
            "storyboard_script": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "required": [
                        "shot_id",
                        "duration_seconds",
                        "scene_tag",
                        "scene_description",
                        "used_asset_tags",
                        "used_props",
                        "continuity_from_prev",
                        "full_prompt_string",
                        "first_frame_prompt",
                        "scela_prompt",
                        "dialogue",
                        "audio",
                    ],
                    "properties": {
                        "shot_id": {"type": "STRING"},
                        "duration_seconds": {"type": "NUMBER"},
                        "scene_tag": {"type": "STRING"},
                        "scene_description": {"type": "STRING"},
                        "used_asset_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "used_props": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "continuity_from_prev": {"type": "STRING"},
                        "full_prompt_string": {"type": "STRING"},
                        "first_frame_prompt": {"type": "STRING"},
                        "scela_prompt": {"type": "STRING"},
                        "dialogue": {"type": "STRING"},
                        "audio": {"type": "STRING"},
                        "referenced_assets": {"type": "ARRAY", "items": {"type": "STRING"}},
                    },
                },
            },
            "voiceover_script": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "required": ["shot_id", "line"],
                    "properties": {
                        "shot_id": {"type": "STRING"},
                        "line": {"type": "STRING"},
                    },
                },
            },
            "validation_report": {
                "type": "OBJECT",
                "required": [
                    "undefined_asset_tags",
                    "missing_scene_context_shots",
                    "missing_prop_context_shots",
                    "rule_violations",
                ],
                "properties": {
                    "undefined_asset_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "missing_scene_context_shots": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "missing_prop_context_shots": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "rule_violations": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
            },
        },
    }


def build_full_schema() -> dict:
    proposal_schema = build_proposal_schema()["properties"]
    execution_schema = build_execution_schema()["properties"]
    return {
        "type": "OBJECT",
        "required": [
            "mode",
            "global_visual_definition",
            "story_adaptation_outline",
            "asset_library",
            "asset_layout_rules",
            "storyboard_script",
            "voiceover_script",
            "validation_report",
        ],
        "properties": {
            "mode": {"type": "STRING", "enum": ["single"]},
            "global_visual_definition": proposal_schema["global_visual_definition"],
            "story_adaptation_outline": proposal_schema["story_adaptation_outline"],
            "asset_library": proposal_schema["asset_library"],
            "asset_layout_rules": proposal_schema["asset_layout_rules"],
            "storyboard_script": execution_schema["storyboard_script"],
            "voiceover_script": execution_schema["voiceover_script"],
            "validation_report": execution_schema["validation_report"],
        },
    }


def build_full_schema_compact() -> dict:
    proposal_schema = build_proposal_schema_compact()["properties"]
    execution_schema = build_execution_schema_compact()["properties"]
    return {
        "type": "OBJECT",
        "required": [
            "mode",
            "story_adaptation_outline",
            "asset_library",
            "asset_layout_rules",
            "storyboard_script",
            "voiceover_script",
            "validation_report",
        ],
        "properties": {
            "mode": {"type": "STRING", "enum": ["single"]},
            "story_adaptation_outline": proposal_schema["story_adaptation_outline"],
            "asset_library": proposal_schema["asset_library"],
            "asset_layout_rules": proposal_schema["asset_layout_rules"],
            "storyboard_script": execution_schema["storyboard_script"],
            "voiceover_script": execution_schema["voiceover_script"],
            "validation_report": execution_schema["validation_report"],
        },
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
    approved_proposal = load_proposal_context(args)
    if args.mode == "single":
        args.phase = "full"
    if args.mode == "two-phase" and args.phase == "full":
        raise ValueError("Mode two-phase requires --phase proposal or --phase execution.")
    if args.mode == "two-phase" and args.phase == "execution" and not approved_proposal:
        raise ValueError("Phase execution requires --proposal-file or --proposal-json.")

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
        # For URL sources (for example YouTube watch links), pass URI directly
        # instead of downloading bytes and forcing inline_data.
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
            "text": build_seedance_prompt(
                brief, args.reference, args.mode, args.phase, approved_proposal, args.output_profile
            )
        }
    )

    if args.output_profile == "compact":
        if args.mode == "single":
            response_schema = build_full_schema_compact()
        elif args.phase == "proposal":
            response_schema = build_proposal_schema_compact()
        else:
            response_schema = build_execution_schema_compact()
    else:
        if args.mode == "single":
            response_schema = build_full_schema()
        elif args.phase == "proposal":
            response_schema = build_proposal_schema()
        else:
            response_schema = build_execution_schema()

    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "temperature": 0.6,
        },
    }


def write_output(path: str | None, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def send_request(args: argparse.Namespace, payload: dict) -> dict:
    token = args.token or os.getenv("YUNWU_API_TOKEN") or os.getenv("GEMINI_API_TOKEN")
    if not token:
        raise ValueError("Missing token. Set --token or YUNWU_API_TOKEN.")

    url = f"{args.base_url.rstrip('/')}/v1beta/models/{args.model}:generateContent"
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


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


def has_english_dialogue_or_voice(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    for shot in result.get("storyboard_script", []):
        if not isinstance(shot, dict):
            continue
        dialogue = shot.get("dialogue", "")
        audio = shot.get("audio", "")
        if isinstance(dialogue, str) and ENGLISH_WORD_RE.search(dialogue):
            return True
        if isinstance(audio, str) and ENGLISH_WORD_RE.search(audio):
            return True
    for line in result.get("voiceover_script", []):
        if not isinstance(line, dict):
            continue
        value = line.get("line", "")
        if isinstance(value, str) and ENGLISH_WORD_RE.search(value):
            return True
    return False


def normalize_structured_output(result: dict) -> dict:
    if not isinstance(result, dict):
        return result

    assets = result.get("asset_library")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            tag = str(asset.get("asset_tag", ""))
            category = str(asset.get("asset_category", ""))
            layout = str(asset.get("layout", ""))
            full_prompt = str(asset.get("full_prompt_string", ""))

            is_role = tag.startswith("@角色_") or ("角色" in category)
            is_prop = tag.startswith("@道具_") or ("道具" in category)

            # Ensure role/prop assets keep clean white-background sheets for stable downstream generation.
            if is_role or is_prop:
                white_bg_phrase = "纯白背景（#FFFFFF），无环境元素"
                four_view_phrase = "四视图（正面全身、侧面全身、背面全身、局部主要特征特写），各视图绝对不能重叠"
                if white_bg_phrase not in layout:
                    asset["layout"] = f"{layout}。{white_bg_phrase}".strip("。")
                if four_view_phrase not in asset["layout"]:
                    asset["layout"] = f"{asset['layout']}。{four_view_phrase}".strip("。")
                if white_bg_phrase not in full_prompt:
                    asset["full_prompt_string"] = f"{full_prompt}，{white_bg_phrase}".strip("，")
                if four_view_phrase not in asset["full_prompt_string"]:
                    asset["full_prompt_string"] = f"{asset['full_prompt_string']}，{four_view_phrase}".strip("，")
                # Strip scene-rich wording from role/prop prompts to avoid conflicts with white-background assets.
                cleaned = str(asset.get("full_prompt_string", ""))
                for phrase in ["环境细节丰富", "丰富的环境细节", "场景内容丰富", "丰富场景细节", "场景细节丰富"]:
                    cleaned = cleaned.replace(f"，{phrase}", "").replace(f"。{phrase}", "")
                    cleaned = cleaned.replace(phrase, "")
                asset["full_prompt_string"] = cleaned.strip("，。 ")

            if tag.startswith("@角色_"):
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

    shots = result.get("storyboard_script")
    if isinstance(shots, list):
        # Build deterministic role alias mapping used in storyboard text.
        role_alias_map: dict[str, str] = {}
        role_index = 0
        for asset in assets if isinstance(assets, list) else []:
            if not isinstance(asset, dict):
                continue
            tag = str(asset.get("asset_tag", ""))
            if tag.startswith("@角色_") and tag not in role_alias_map:
                role_alias_map[tag] = f"角色{chr(ord('A') + role_index)}"
                role_index += 1

        known_names = [
            "Rumi",
            "Mira",
            "Zoey",
            "Jinu",
            "Abby",
            "Baby saja",
            "Mystery",
            "Romance",
            "Doctor",
            "C罗",
            "梅西",
            "内马尔",
        ]
        name_alias_map: dict[str, str] = {}
        for idx, n in enumerate(known_names):
            name_alias_map[n] = f"角色{chr(ord('A') + (idx % 26))}"

        def sanitize_story_text(value: str) -> str:
            text = str(value or "")
            if not text:
                return text
            # Remove shot numbering labels.
            text = SHOT_LABEL_RE.sub("", text)
            # Replace role tags first.
            for tag, alias in role_alias_map.items():
                text = text.replace(tag, alias)
            # Remove any residual @tags/--ref directives.
            text = ASSET_TAG_RE.sub("", text)
            # Replace known concrete names with anonymized role aliases.
            for name, alias in name_alias_map.items():
                text = text.replace(name, alias)
            # Clean duplicated punctuation and spaces.
            text = re.sub(r"\s{2,}", " ", text).strip(" ，。;；")
            return text

        for idx, shot in enumerate(shots):
            if not isinstance(shot, dict):
                continue
            if not str(shot.get("scene_tag", "")).strip():
                shot["scene_tag"] = "@场景_未标注"
            if not str(shot.get("scene_description", "")).strip():
                shot["scene_description"] = "场景描述缺失，需补充空间结构、光线状态与环境细节。"

            used_assets = shot.get("used_asset_tags")
            if not isinstance(used_assets, list):
                shot["used_asset_tags"] = []

            used_props = shot.get("used_props")
            if not isinstance(used_props, list) or len(used_props) == 0:
                shot["used_props"] = ["无道具"]

            if not str(shot.get("continuity_from_prev", "")).strip():
                if idx == 0:
                    shot["continuity_from_prev"] = "起始镜头，无上一镜头，建立角色与环境初始状态。"
                else:
                    shot["continuity_from_prev"] = "承接上一镜头的角色位置、动作趋势与道具状态。"

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
                "continuity_from_prev",
                "first_frame_prompt",
                "scela_prompt",
                "dialogue",
                "audio",
            ]:
                shot[key] = sanitize_story_text(str(shot.get(key, "")))

    report = result.get("validation_report")
    if not isinstance(report, dict):
        report = {}
        result["validation_report"] = report
    if "missing_scene_context_shots" not in report or not isinstance(
        report.get("missing_scene_context_shots"), list
    ):
        report["missing_scene_context_shots"] = []
    if "missing_prop_context_shots" not in report or not isinstance(
        report.get("missing_prop_context_shots"), list
    ):
        report["missing_prop_context_shots"] = []

    # Ensure voiceover lines also satisfy zero-name/zero-tag constraint.
    voice_lines = result.get("voiceover_script")
    if isinstance(voice_lines, list):
        for item in voice_lines:
            if not isinstance(item, dict):
                continue
            line = str(item.get("line", ""))
            line = SHOT_LABEL_RE.sub("", line)
            line = ASSET_TAG_RE.sub("", line)
            for n in ["Rumi", "Mira", "Zoey", "Jinu", "Abby", "Baby saja", "Mystery", "Romance", "Doctor", "C罗", "梅西", "内马尔"]:
                line = line.replace(n, "角色A")
            item["line"] = re.sub(r"\s{2,}", " ", line).strip(" ，。;；")

    return result


def build_chinese_repair_payload(args: argparse.Namespace, schema: dict, result: dict) -> dict:
    prompt = (
        "你是 JSON 语言修正器。将下面 JSON 中所有字符串值改写为简体中文，"
        "只做语言转换，不改动结构、不删字段、不新增字段。"
        "JSON key 必须保持英文原样。保留 @资产标签、ID、数字、比例、时长。"
        "输出必须是合法 JSON。\n\n"
        f"待修正 JSON：\n{json.dumps(result, ensure_ascii=False)}"
    )
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.2,
        },
    }


def main() -> int:
    try:
        args = parse_args()
        payload = build_request(args)
        if args.dry_run:
            write_output(args.output, payload)
            return 0
        response = send_request(args, payload)
        parsed = unwrap_response_json(response)
        schema = payload.get("generationConfig", {}).get("responseSchema", {})
        if has_english_dialogue_or_voice(parsed):
            repair_payload = build_chinese_repair_payload(args, schema, parsed)
            repair_response = send_request(args, repair_payload)
            parsed = unwrap_response_json(repair_response)
        parsed = normalize_structured_output(parsed)
        write_output(args.output, parsed)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
