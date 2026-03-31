InvoiceFlowAI desktop package

What this package is
- A Windows desktop build of InvoiceFlowAI.
- No separate Python installation is required on the target machine.
- This package includes its own WebView2 runtime and should not require a separate browser runtime installation.
- This package does not include mailbox credentials, auth codes, API keys, or prior run data.

How to run
1. Extract the full folder to a normal writable local directory.
2. Keep `InvoiceFlowAI.exe` and `_internal` in the same folder.
3. Start `InvoiceFlowAI.exe`.
4. Enter your own mailbox, mailbox auth code, company name, and GLM API key.
5. The app creates its default output folder on the Desktop.

Important notes
- Do not add `.env` files, prior run outputs, or diagnostic folders into the program directory.
- If startup fails, provide the exact error message or a screenshot of the popup.
