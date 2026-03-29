InvoiceFlowAI external test package

What this package is
- A portable Windows build for external batch-testing.
- No separate Python installation is required on the target machine.
- This package includes its own WebView2 runtime and should not require a separate browser runtime installation.
- This package does not include real mailbox credentials, auth codes, API keys, or prior test data.

How to run
1. Extract the full folder to a normal writable local directory.
2. Keep `InvoiceFlowAI.exe` and `_internal` in the same folder.
3. Start `InvoiceFlowAI.exe`.
4. Enter your own mailbox, mailbox auth code, company name, and GLM API key.
5. The app creates its default output folder on the Desktop.

Important notes
- This build is intended for external testing only.
- Do not add `.env` files, prior run outputs, or diagnostic folders into the program directory.
- If startup fails, provide the exact error message or a screenshot of the popup.
