---
name: gemini-video-story-adapter
description: Analyze a source video plus an adaptation brief, reference images, and creative constraints, then generate a structured remake package with story premise, characters, scenes, assets, and storyboard image prompts using Gemini native generateContent payloads. Use when the user provides a local video path, a video URL, existing Gemini file URI, remake direction, mood/style references, or wants a JSON-first preproduction package for video adaptation.
---

# Gemini Video Story Adapter

Use this skill to turn a source video, an adaptation brief, and optional reference images into a three-block preproduction package:

- `asset_json`
- `storyboard_json`
- `video_prompts_json`

It is a single-pass Gemini analysis workflow for remake planning, not a generic prose summarizer.

## Current Workflow

1. Normalize the source video, adaptation brief, and reference images.
2. Build one Gemini native `generateContent` request with a strict JSON schema.
3. Ask for three structured blocks: assets, storyboard, and shot-level video prompts.
4. Apply local post-processing to normalize tags, clean prompt text, and fill safe defaults where needed.
5. If `--output` is provided, write the full result JSON and also emit renderer-ready `assets.json` and `storyboard.json` in the same directory.
6. Optionally render the JSON into a human-readable summary after generation.

## Inputs

Provide as many of these as useful:

- Source video: local path, direct video URL, or Gemini `file_uri`
- Adaptation brief: what changes, what stays, target platform, duration, tone, genre, audience
- Reference images: local paths or URLs for style, costume, environment, lighting, lensing, or appearance anchoring
- Hard constraints: budget, cast count, locations, era, aspect ratio, language, censorship/safety limits

If the brief is vague, the skill should infer a conservative adaptation direction and treat it as inferred rather than as confirmed fact.

## Output Contract

The skill asks Gemini for exactly three top-level JSON blocks:

- `asset_json`
- `storyboard_json`
- `video_prompts_json`

Use English keys and Simplified Chinese values.

### `asset_json`
Use this block for:
- story adaptation outline
- asset library
- asset layout rules
- optional richer global visual definition in `full` mode

### `storyboard_json`
Use this block for:
- shot-level storyboard prompts
- first-frame prompts
- SCELA objects
- dialogue and audio fields
- voiceover lines

### `video_prompts_json`
Use this block for:
- shot-level video prompts only
- one item per shot
- fields aligned with `shot_id`, `scela_prompt`, `dialogue`, and `audio`

## Core Asset Rules

- Internal canonical asset tags must use compact forms like `@角色A`, `@道具A`, `@场景A`.
- One real entity maps to one canonical asset tag.
- Do not split the same person, prop, or scene into multiple assets because of temporary state changes.
- Temporary state changes such as expression, pose, held/not-held, open/closed, damage, lighting shift, framing shift, or local room-angle change belong in shot-level prompts, not separate asset entries.
- Character wording in storyboard output must stay aligned with the corresponding asset definition.
- Use explicit source names when the source clearly provides them; otherwise use stable generic labels such as `角色A`, `角色B`, `道具A`, `场景A` consistently.
- Never expose internal `@...` tags in final storyboard text fields.

## Asset Layout Rules

- All asset images must use `16:9` landscape layouts.
- Character assets: front / side / back full-body three-view.
- Prop assets: front / side / back three-view.
- Scene assets: panorama / bird's-eye / close-up detail three-view.
- Character and prop assets must use pure white background with no environment elements.
- Scene assets must not contain people, body parts, silhouettes, reflections of people, or character-presence cues.
- Character assets must include concrete `wardrobe_design`, `makeup_design`, and `accessory_design` fields.
- Do not describe hair traits in asset prompts or storyboard prompts.
- Do not add gender, age, or body-shape labels.

## Storyboard Rules

- Every storyboard shot must explicitly include scene context and used props/assets.
- `used_props` must never be empty; use `['无道具']` when needed.
- `full_prompt_string` is required per shot and must start with `(单张全屏，严禁拼图，无边框，电影定格单帧)`.
- `first_frame_prompt` must describe the t=0 visible state, not a mid-action extreme.
- `scela_prompt` must stay structured as an object with `subject`, `camera`, `effect`, and `audio`.
- Do not include shot-number labels like `Shot 1` / `镜头1` / `s1` in prompt text fields.
- Every shot prompt must be standalone and reusable outside the conversation.

## Video Prompt Rules

- Output `video_prompts_json` by shot, not by scene.
- Each item must contain `shot_id`, `scela_prompt`, `dialogue`, and `audio`.
- `scela_prompt` should be a concise standalone natural-language paragraph describing motion from the first frame.
- Dialogue and audio stay as separate fields rather than being merged into one paragraph.
- Emphasize visible acting detail: facial changes, eye focus shifts, eyebrow movement, mouth changes, jaw tension, posture changes, hesitation, recoil, leaning, freezing, and emotional transition.
- Avoid vague emotion labels unless they are unfolded into visible performance details.

## Request Construction

Use Gemini native `contents` and `parts`.

- Use `inline_data` for small local or downloaded media.
- Use `file_data` with `file_uri` when a reusable Gemini file already exists.
- Put the adaptation brief in a text part after media parts.
- Set `generationConfig.responseMimeType` to `application/json`.
- Include a strict `responseSchema` so the model returns the required three-block structure.

Useful files:
- [api-summary.md](./references/api-summary.md)
- [run_analysis.py](./scripts/run_analysis.py)
- [seedance-structured-prompt.md](./references/seedance-structured-prompt.md)

## Required Config

Required for live requests:
- `YUNWU_API_TOKEN` unless `--token` is passed explicitly

Optional overrides:
- `YUNWU_BASE_URL` or `GEMINI_BASE_URL`
- `GEMINI_MODEL`
- `--base-url`
- `--model`
- `--output-profile` (`compact` or `full`)

Use [assets/.env.example](./assets/.env.example) as the starting template.

Recommended shell setup:

```bash
export YUNWU_API_TOKEN="your-token"
export YUNWU_BASE_URL="https://yunwu.ai"
```

If the token is missing, the script cannot run a live request.

## Execution Discipline

This is a heavyweight multimodal request path.

- Typical latency: 60–180 seconds
- Default timeout: `600` seconds
- Lock file: `.run_analysis.lock`
- Heartbeat: stderr every 10 seconds during API wait
- Default retry count: `0`
- 429/5xx retries are opt-in; 4xx errors are not retried
- Timeout writes `analysis_status=timeout` and retry guidance, but does not auto-retry
- Do not start a second live instance while one is already running

## Returned Envelope

On success, the final JSON also includes:
- `analysis_status: ok`
- `success: true`

On failure or timeout, it still includes:
- `analysis_status`
- `success`
- `failure_reason`
- `should_retry`
- `retry_guidance`
- `server_request_ids`

If `--output` points to a file such as `./analysis/result.json`, the script also writes:
- `./analysis/assets.json`
- `./analysis/storyboard.json`
- `./analysis/video_prompts.json`

These side files make the output directory easier to use downstream:
- `assets.json` and `storyboard.json` are shaped for direct consumption by `banana-previz-renderer`
- `video_prompts.json` exposes the shot 级图生视频 prompts without forcing you to open `result.json`

Recommended output layout:
- `./analysis/result.json`
- `./analysis/assets.json`
- `./analysis/storyboard.json`
- `./analysis/video_prompts.json`

If you only need the shot 级图生视频 prompts, read `video_prompts.json` directly.

## Media Strategy

Prefer input modes in this order:

1. Existing Gemini `file_uri` for large reusable videos
2. Local video as `inline_data` when comfortably under the request limit
3. Remote video URL only when it can be treated as reusable remote media or safely downloaded inline

For reference images, prefer `inline_data` unless the user already has a reusable uploaded URI.

If the source video is too large for inline upload and no reusable `file_uri` is available, switch to an uploaded Gemini file workflow first.

## Example Usage

Live request:

```bash
export YUNWU_API_TOKEN="..."
export YUNWU_BASE_URL="https://yunwu.ai"
python3 ./scripts/run_analysis.py \
  --video ./input/source.mp4 \
  --brief-file ./brief.txt \
  --reference ./refs/look-1.jpg \
  --output ./analysis/result.json
```

After the run, shot 级图生视频内容就在 `./analysis/video_prompts.json`。

Then hand off directly to renderer:

```bash
python3 ../banana-previz-renderer/scripts/run_banana_pipeline.py \
  --analysis-json ./analysis \
  --phase assets \
  --identity-map-json ./role-refs.json \
  --output-dir ./outputs
```

Use `--output-profile full` when you want richer descriptive fields and can accept higher token usage.
