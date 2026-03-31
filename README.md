# InvoiceFlowAI Source Baseline

This repository is the public source baseline for the current InvoiceFlowAI desktop application.

What it is
- The source of truth for ongoing desktop development, packaging, and release preparation.
- The repository that should be used for new branches and `git worktree`.
- A baseline that preserves the current dark UI and the packaged WebView2 startup fix.

What it is not
- It is not the portable package directory.
- It does not track local credentials, diagnostics, truth datasets, build outputs, or packaged browser runtimes.
- It is not intended to carry private handoff notes, local screenshots, or machine-specific paths.

Before building
1. Create or activate a Python 3.12 environment with the build dependencies.
2. Run `build\\windows\\prepare_runtime.ps1` to hydrate Playwright Chromium and the fixed-version WebView2 runtime.
3. Run `build\\windows\\build_release.ps1`.

