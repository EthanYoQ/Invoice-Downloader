# Codex Long-Term Memory

## Frontend Copy Guardrails
- Never render design notes, implementation notes, rewrite rationale, TODOs, review comments, or scope explanations as user-visible UI text.
- Treat page headers, badges, status bars, helper copy, empty states, and dialog subtitles as the highest-risk leak zones for developer-only language.
- Before finishing any frontend redesign or UI polish task, run a leak audit over visible copy for terms like `保留原有`, `只重做`, `仅重构视觉`, `设计稿`, `实现`, `备注`, `TODO`, `mockup`, `phase`, `Apple`, and manually inspect every screen for accidental developer-facing text.
- Visible UI copy must come from product intent and user workflow needs, not from implementation commentary.

## README Style Guardrails
- Default README style must follow the Chinese product-facing structure and tone established by the `cbc51d7` version of `README.md`.
- README content should read like software usage documentation, not a handoff memo, baseline note, or internal engineering summary.
- Prefer the following structure unless the user explicitly asks otherwise: `适用场景`、`当前发布`、`功能概览`、`使用方法`、`输出目录说明`、`系统要求`、`从源码运行`、`构建发行版`、`隐私与安全`.
- README should prioritize user value, product explanation, and practical usage guidance over implementation detail.
- When a release asset changes, update README download links and release wording to match the actual public release state before publishing.
- Do not include personal data, internal test labels, developer commentary, or outdated release instructions in README content.
