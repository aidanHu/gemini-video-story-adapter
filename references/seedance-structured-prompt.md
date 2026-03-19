# Seedance Structured Prompt

## Goal

Use this reference when the user wants Seedance-style realistic image-to-video planning with strict rule enforcement and structured output.

## Why this version is stronger

The original prompt is powerful but too monolithic. It relies on the model remembering a long instruction stack and stopping between phases on its own. This structured version improves reliability by:

- supporting a default single-pass full-output mode for token efficiency
- separating `proposal` and `execution` into two explicit API phases
- mapping rules to concrete schema fields
- forcing validation output instead of trusting the model silently
- preserving your Seedance/SCELA constraints without asking the model to improvise the output shape

## Recommended Default

Use `single` mode by default.

Reason:

- fewer API calls
- lower coordination overhead
- lower total token cost in common cases
- faster iteration when the user already knows they want the full package

Switch to `two-phase` only when the user wants an approval gate on assets and wording before storyboard generation.

## Phase Design

### Phase 1

Return only:

- `phase`
- `global_visual_definition`
- `story_adaptation_outline`
- `asset_library`
- `asset_layout_rules`
- `approval_checkpoint`

Use this phase to lock story direction, asset tags, and asset wording before generating shots.

### Phase 2

Return only:

- `phase`
- `storyboard_script`
- `voiceover_script`
- `validation_report`

Use this phase only after the user approves the proposal.

## Recommended Interaction Pattern

1. Run `proposal` phase with the source video and the user's micro-innovation brief.
2. Show the proposal JSON or a rendered summary derived from it.
3. Wait for explicit user confirmation.
4. Run `execution` phase with the approved direction and inject the approved proposal JSON.

## Rule Mapping

Map the original prompt's rules like this:

- two-phase protocol -> `phase` plus phase-specific schema
- benchmark shot inheritance -> `benchmark_inheritance`
- asset independence -> one object per `asset_library` item
- layout rules -> `asset_layout_rules`
- first-frame independence -> `first_frame_prompt`
- SCELA expansion -> `scela_prompt`
- Simplified Chinese voice rules -> `voiceover_script[].line`
- post-check -> `validation_report`

## Operational Note

Yes, this can implement true two-stage output in practice, but not as one single model response that magically pauses itself. The reliable implementation is:

- first API call for proposal
- persist the approved proposal JSON locally
- explicit user confirmation
- second API call for execution with the approved proposal JSON included in the prompt

Anything else is imitation rather than control.
