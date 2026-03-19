---
name: gemini-video-story-adapter
description: Analyze a source video plus an adaptation brief, reference images, and creative constraints, then generate a structured remake package with story premise, characters, scenes, assets, and storyboard image prompts using Gemini native generateContent payloads. Use when the user provides a local video path, a video URL, existing Gemini file URI, remake direction, mood/style references, or wants a JSON-first preproduction package for video adaptation.
---

# Gemini Video Story Adapter

Use this skill to turn raw source material into a reusable adaptation package instead of a loose prose summary.

Prefer structured JSON output that downstream tools can validate. Keep the model focused on adaptation, not transcription.

## Workflow

1. Normalize the inputs.
2. Default to `single` mode unless the user explicitly wants staged review.
3. Build the analysis prompt around the user's remake goal and Seedance constraints.
4. Call Gemini using native `generateContent` format.
5. Validate the JSON shape before presenting results.
6. If the user wants, render the JSON into readable tables after validation.

## Input Normalization

Collect as many of these as the user can provide:

- Source video: local file path, direct video URL, or an existing Gemini `file_uri`.
- Adaptation brief: what should change, what must stay, target platform, duration, tone, genre, audience.
- Reference images: local paths or URLs for style, costume, environment, lighting, lensing, or character appearance.
- Hard constraints: budget, cast count, locations, era, aspect ratio, language, censorship/safety limits.

Ask for missing constraints only when they materially affect the output. If the brief is vague, infer a conservative adaptation thesis and label it as inferred.

## Payload Rules

Use Gemini native `contents` and `parts`. Keep media parts separate from text parts.

- For small local or downloaded files, use `inline_data` with base64 bytes.
- For existing uploaded Gemini files, use `file_data` with `file_uri`.
- Put the creative brief in a plain text part after the media parts.
- Set `generationConfig.responseMimeType` to `application/json`.
- Include a concrete `responseSchema` so the model returns machine-parseable output.

Use [api-summary.md](./references/api-summary.md) when you need the exact proxy endpoint or payload structure.

Use [run_analysis.py](./scripts/run_analysis.py) when you want a ready-made request builder and API caller.

Use [seedance-structured-prompt.md](./references/seedance-structured-prompt.md) when the user wants Seedance 2.0 prompt engineering, micro-innovation against a benchmark video, or explicit control over single-pass vs two-phase output.

## Required Config

Do not attempt a live API call without auth config.

Required for real requests:

- `YUNWU_API_TOKEN`: required unless `--token` is passed explicitly.

Optional overrides:

- `YUNWU_BASE_URL`: defaults to `https://yunwu.ai`.
- `GEMINI_BASE_URL`: fallback only if `YUNWU_BASE_URL` is absent.
- `--base-url`: overrides both environment variables for the current run.
- `--model`: overrides the default model for the current run.
- `--output-profile`: `compact` (default, lower token cost) or `full` (more detailed fields).

Use [assets/.env.example](./assets/.env.example) as the starting template.

Recommended shell setup:

```bash
export YUNWU_API_TOKEN="your-token"
export YUNWU_BASE_URL="https://yunwu.ai"
```

If the token is missing, the script should only be used with `--dry-run` to build the request body.

## Prompt Construction

Frame the task as Seedance production planning, not generic summarization. The prompt should force the model to:

- identify what the source video is doing now
- explain how the remake should differ
- preserve only the source details that still matter
- output production-ready assets, standalone first-frame prompts, SCELA prompts, and voice lines in Simplified Chinese
- expose uncertainty and assumptions explicitly
- respect your non-negotiable Seedance rules around assets, layout, lighting, continuity, and shot inheritance
- keep JSON keys in English and output values in Simplified Chinese
- enforce three-view asset rules explicitly in `layout` and `full_prompt_string`:
- character: front/side/back full-body three-view
- prop: front/side/back three-view
- scene: panorama/bird's-eye/close-up three-view

Character mapping table (must be enforced in analysis):

- `Rumi->紫发女人`
- `Mira->红发女人`
- `Zoey->黑发女人`
- `Jinu->黑发男人`
- `Abby->红发男人`
- `Baby saja->蓝发男人`
- `Mystery->银发男人`
- `Romance->粉发男人`

Use these labels as stable identifiers for asset anchoring. Keep script/dialogue names as original character names.
Do not output any hair-related descriptors in角色提示词或分镜提示词（发型、发色、长短等均禁止）。

Mode-specific output contract:

- `single` (default): `global_visual_definition`, `story_adaptation_outline`, `asset_library`, `asset_layout_rules`, `storyboard_script`, `voiceover_script`, `validation_report`
- `two-phase proposal`: `global_visual_definition`, `story_adaptation_outline`, `asset_library`, `asset_layout_rules`, `approval_checkpoint`
- `two-phase execution`: `storyboard_script`, `voiceover_script`, `validation_report`

If the user asks for a rendered human-readable version, still generate the full JSON first, then derive the readable version from it.

## Output Modes

Default mode is `single`. Use it when the user wants the full result in one pass to save tokens and avoid an extra round trip.

Use `two-phase` only when the user wants to inspect and approve the asset library before generating shots.

Two-phase workflow:

1. `--mode two-phase --phase proposal`
2. Wait for explicit confirmation.
3. `--mode two-phase --phase execution --proposal-file ...`

Do not rely on the model to "stop itself" mid-response in a single call.

## Media Strategy

Prefer these input modes in order:

1. Existing Gemini `file_uri` for large reusable videos.
2. Local video file encoded as `inline_data` when comfortably under the request limit.
3. Remote video URL downloaded locally, then encoded as `inline_data` if size allows.

For reference images, prefer `inline_data` unless the user already has a reusable uploaded URI.

If the source video is too large for inline upload and no reusable file URI is available, stop and explain that the request should switch to an uploaded Gemini file workflow before analysis.

## Validation

Before presenting results:

- Confirm the API returned valid JSON.
- Confirm every storyboard shot references an existing scene.
- Confirm every required asset is connected to at least one scene or shot.
- Confirm characters have distinct goals or functional roles.
- Flag guessed details under `production_notes.assumptions`.

If validation fails, repair the JSON through a follow-up prompt instead of manually inventing missing sections.

## Execution

Use this script for most runs:

```bash
python3 ./scripts/run_analysis.py \
  --video ./input/source.mp4 \
  --brief "改编成90秒悬疑短片，保留母女关系，把结局改成开放式。" \
  --mode single \
  --output-profile compact \
  --reference ./refs/look-1.jpg \
  --reference https://example.com/look-2.png \
  --model gemini-3.1-pro-preview \
  --base-url https://yunwu.ai \
  --dry-run \
  --output ./tmp/request.json
```

To send the request directly:

```bash
export YUNWU_API_TOKEN="..."
export YUNWU_BASE_URL="https://yunwu.ai"
python3 ./scripts/run_analysis.py \
  --video ./input/source.mp4 \
  --brief-file ./brief.txt \
  --mode single \
  --reference ./refs/look-1.jpg \
  --output ./tmp/result.json
```

Strict two-phase mode:

```bash
python3 ./scripts/run_analysis.py \
  --video ./input/source.mp4 \
  --brief-file ./brief.txt \
  --mode two-phase \
  --phase proposal \
  --output ./tmp/proposal.json

python3 ./scripts/run_analysis.py \
  --video ./input/source.mp4 \
  --brief-file ./brief.txt \
  --mode two-phase \
  --phase execution \
  --proposal-file ./tmp/proposal.json \
  --reference ./refs/look-1.jpg \
  --output ./tmp/result.json
```

The script defaults to the Yunwu proxy and Gemini native payload shape. Review the generated JSON before changing the schema.

Use `--output-profile full` when you need richer descriptive fields and can accept higher token usage.
