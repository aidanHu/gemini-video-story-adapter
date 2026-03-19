# gemini-video-story-adapter

用于“视频拆解 + 改编策划”的结构化分析 skill。  
输入参考视频与改编需求，输出可直接给下游生图/分镜流程使用的 JSON（角色、道具、场景、分镜、台词、校验报告）。

## 当前默认

- 默认模型：`gemini-3.1-pro-preview`
- 默认模式：`single`
- 默认输出：`compact`
- 接口：`POST {YUNWU_BASE_URL}/v1beta/models/{model}:generateContent`

## 目录结构

- `scripts/run_analysis.py`：主执行脚本
- `references/api-summary.md`：API 结构摘要
- `references/seedance-structured-prompt.md`：提示词规范参考
- `assets/.env.example`：环境变量模板（如存在）

## 环境变量

- 必填：`YUNWU_API_TOKEN`
- 可选：`YUNWU_BASE_URL`（默认 `https://yunwu.ai`）

## 常用命令

```bash
python3 ./scripts/run_analysis.py \
  --video "https://www.youtube.com/watch?v=xxxx" \
  --brief-file ./brief.txt \
  --mode single \
  --output-profile compact \
  --output ./result.json
```

仅构造请求不发送：

```bash
python3 ./scripts/run_analysis.py \
  --video-file-uri "https://example.com/video.mp4" \
  --brief "test" \
  --dry-run \
  --output ./request.json
```

## 输出核心字段（single/compact）

- `story_adaptation_outline`
- `asset_library`
- `asset_layout_rules`
- `storyboard_script`
- `voiceover_script`
- `validation_report`

## 规则要点（已内置）

- 分镜文本：`Zero Tags / Zero Names / Zero Shot Labels`
- 每条分镜包含：场景、道具、连续性
- 分镜 `full_prompt_string` 强制前缀：`(单张全屏，严禁拼图，无边框，电影定格单帧)`
- 角色/道具：绝对白底 + 四视图 + 视图不重叠
- 场景：三视图 + 环境细节丰富
- 输出值默认中文（JSON key 保持英文）

## 备注

更完整的指令流程见同目录 `SKILL.md`。
