# InvoiceFlowAI Source Baseline

This repository is the clean Git baseline for the current externally shared InvoiceFlowAI portable build.

What it is
- A source snapshot derived from the latest external-test packaging source.
- The only repo that should be used for new branches and `git worktree`.
- A clean baseline that preserves the current dark UI and the packaged WebView2 startup fix.

What it is not
- It is not the portable package directory.
- It does not track local credentials, diagnostics, truth datasets, build outputs, or packaged browser runtimes.
- It does not replace the old `Codex Invoice` repository for historical forensics.

Before building
1. Create or activate a Python 3.12 environment with the build dependencies.
2. Run `build\\windows\\prepare_runtime.ps1` to hydrate Playwright Chromium and the fixed-version WebView2 runtime.
3. Run `build\\windows\\build_release.ps1`.

Historical reference
- Old source/history repo: `D:\Vibe Coding Project\Coading Backup\Codex Invoice`
- Shared portable folder: `D:\Vibe Coding Project\InvoiceFlowAI-portable-external-test`
- Shared portable zip: `D:\Vibe Coding Project\InvoiceFlowAI-portable-external-test-webview2-fixed.zip`

