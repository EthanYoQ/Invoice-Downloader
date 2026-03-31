# Codex Long-Term Memory

## Frontend Copy Guardrails
- Never render design notes, implementation notes, rewrite rationale, TODOs, review comments, or scope explanations as user-visible UI text.
- Treat page headers, badges, status bars, helper copy, empty states, and dialog subtitles as the highest-risk leak zones for developer-only language.
- Before finishing any frontend redesign or UI polish task, run a leak audit over visible copy for terms like `保留原有`, `只重做`, `仅重构视觉`, `设计稿`, `实现`, `备注`, `TODO`, `mockup`, `phase`, `Apple`, and manually inspect every screen for accidental developer-facing text.
- Visible UI copy must come from product intent and user workflow needs, not from implementation commentary.

## README Style Guardrails
- The `93e45a2` README is now the canonical baseline and replaces every earlier README preference memory.
- Future README work must preserve the `93e45a2` structure, tone, presentation style, diagrams, FAQ layout, and explanatory depth unless the user explicitly requests a broader rewrite.
- README changes must be limited to local corrections of inaccurate facts, broken links, outdated release references, or similarly narrow issues; do not proactively restructure, reframe, or redesign the document.
- README content should continue to read like a product-facing software manual, not a handoff memo, baseline note, or internal engineering summary.
- Keep the `93e45a2` Chinese product-first form, including badges, visual architecture sections, SVG/diagram usage, setup guides, FAQ blocks, disclaimer sections, and footer style.
- When a release asset changes, update README download links and release wording to match the actual public release state before publishing.
- Do not include personal data, internal test labels, developer commentary, or outdated release instructions in README content.
