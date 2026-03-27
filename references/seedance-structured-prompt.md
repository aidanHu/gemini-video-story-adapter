# Seedance Structured Prompt

## Goal

Use this reference when you want a Seedance-style realistic remake package generated in one pass with a stable three-block JSON structure.

## Current Working Shape

The current contract is:

- `asset_json`
- `storyboard_json`
- `video_prompts_json`

These blocks divide responsibilities clearly:

### `asset_json`
Use for:
- story adaptation outline
- asset library
- asset layout rules
- optional richer global visual definition in `full` mode

### `storyboard_json`
Use for:
- storyboard shots
- first-frame prompts
- SCELA objects
- dialogue and audio fields
- voiceover lines

### `video_prompts_json`
Use for:
- shot-level video prompts only
- one item per shot
- fields aligned with `shot_id`, `scela_prompt`, `dialogue`, and `audio`

## Core Rules to Preserve

### Canonical asset rules
- Use compact internal tags like `@角色A`, `@道具A`, `@场景A`.
- One real entity maps to one canonical asset tag.
- Temporary state changes stay in shot-level prompts instead of creating extra asset entries.

### Storyboard rules
- Every shot must include scene context and used props/assets.
- `full_prompt_string` is standalone and starts with `(单张全屏，严禁拼图，无边框，电影定格单帧)`.
- `first_frame_prompt` describes the t=0 visible state.
- `scela_prompt` stays structured as `subject`, `camera`, `effect`, `audio`.
- Final storyboard text must not leak internal `@...` tags.

### Video prompt rules
- Output by shot, not by scene.
- Keep `scela_prompt` as the motion paragraph for the shot.
- Keep `dialogue` and `audio` as separate fields.
- Emphasize visible acting details and camera priority.
- Avoid extra sections like titles, wardrobe notes, remarks, or analysis labels.

## Why this structure works

- single-pass request path
- fewer API calls
- lower coordination overhead
- easier downstream consumption
- clearer separation between assets, storyboard shots, and shot-level video prompts

## Usage pattern

1. Run one analysis call with source video, adaptation brief, and references.
2. Return the three JSON blocks in one response.
3. If needed, render human-readable summaries from those blocks after generation.

## Operational note

The implementation is intentionally single-pass. Do not rely on the model to manage multi-stage pauses inside one response.
