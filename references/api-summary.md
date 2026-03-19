# API Summary

## Purpose

Use this reference when building Gemini-native requests for video adaptation analysis through the Yunwu proxy documented at:

- `https://yunwu.apifox.cn/api-309482709`
- Official Gemini video docs: `https://ai.google.dev/gemini-api/docs/video-understanding`

## Request Shape

Proxy endpoint used by the Apifox page:

```text
POST https://yunwu.ai/v1beta/models/{model}:generateContent
Authorization: Bearer <token>
Content-Type: application/json
```

Observed proxy example for video understanding:

- Route: `/v1beta/models/gemini-3.1-pro-preview:generateContent`
- Header auth: `Authorization: Bearer <token>`
- Video part: `contents[].parts[].inline_data`
- MIME example: `video/mp4`

Gemini-native body pattern:

```json
{
  "contents": [
    {
      "role": "user",
      "parts": [
        {
          "inline_data": {
            "mime_type": "video/mp4",
            "data": "<base64>"
          }
        },
        {
          "text": "analysis brief here"
        }
      ]
    }
  ],
  "generationConfig": {
    "responseMimeType": "application/json",
    "responseSchema": {}
  }
}
```

## Input Decisions

Use these media part types:

- `inline_data`: for local files or downloaded URLs when the request stays within practical size limits.
- `file_data`: for existing Gemini file URIs such as `https://...` or `gs://...` references already returned by a file upload flow.

Recommended practical rule from Gemini docs:

- Prefer uploaded files for large videos or reusable assets.
- Prefer inline video only for short, small one-off inputs.

## Prompting Rules

Keep the text part explicit about:

- source-video understanding
- adaptation goal
- elements to preserve
- elements to change
- required output schema
- how to use reference images

Do not ask the model to "be creative" without constraints. Always bind creativity to tone, audience, length, and production constraints.

## Output Contract

For this skill, require JSON with these top-level keys:

- `source_summary`
- `adaptation_strategy`
- `characters`
- `scenes`
- `assets`
- `storyboard`
- `production_notes`

Each storyboard shot should carry an image-generation-ready prompt that mentions:

- subject
- environment
- wardrobe or prop anchors
- framing
- lens or camera language
- lighting
- mood
- continuity constraints

## Failure Modes

- If the proxy returns non-JSON text, retry with a stricter schema reminder.
- If the video is too large for inline upload, switch to file upload and `file_data`.
- If remote URLs are unavailable or blocked, ask the user for local files.
- If the model hallucinates missing visual details, keep them under assumptions instead of presenting them as facts.
