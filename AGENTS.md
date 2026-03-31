# Codex Long-Term Memory

## Frontend Copy Guardrails
- Never render design notes, implementation notes, rewrite rationale, TODOs, review comments, or scope explanations as user-visible UI text.
- Treat page headers, badges, status bars, helper copy, empty states, and dialog subtitles as the highest-risk leak zones for developer-only language.
- Before finishing any frontend redesign or UI polish task, run a leak audit over visible copy for terms like `保留原有`, `只重做`, `仅重构视觉`, `设计稿`, `实现`, `备注`, `TODO`, `mockup`, `phase`, `Apple`, and manually inspect every screen for accidental developer-facing text.
- Visible UI copy must come from product intent and user workflow needs, not from implementation commentary.

## README Style Guardrails
- The current `README.md` on `main` is the canonical baseline and replaces any earlier README preference memory, including the prior `cbc51d7`-based baseline.
- Future README work must preserve the current structure, tone, and user-facing writing style unless the user explicitly requests a broader rewrite.
- README changes must be limited to local corrections of inaccurate facts, broken links, outdated release references, or similarly narrow issues; do not proactively restructure or rewrite the document.
- README content should read like software usage documentation, not a handoff memo, baseline note, or internal engineering summary.
- Keep the current Chinese product-facing structure unless the user explicitly asks otherwise.
- When a release asset changes, update README download links and release wording to match the actual public release state before publishing.
- Do not include personal data, internal test labels, developer commentary, or outdated release instructions in README content.
- For learning/reference purposes, also study the `8efc8a6` README version as the preferred example of stronger README storytelling and presentation style.
- The `8efc8a6` README is the reference for tone and format patterns such as product-first headline writing, badge-based summary, visual architecture explanation, SVG/diagram support, step-by-step setup guidance, FAQ structure, and fuller project explanation.
- If there is any conflict between the current `main` README and the `8efc8a6` README, use the current `main` README as the exact content baseline and use `8efc8a6` only as a stylistic and explanatory reference.
