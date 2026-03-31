# InvoiceFlowAI

InvoiceFlowAI is a Windows desktop app for extracting invoice emails, classifying invoice documents, and archiving them into a local output folder.

## Download

- Portable package:
  [InvoiceFlowAI-portable-2026.03.31.0.zip](https://github.com/Ethan-YoungQ/Invoice-Downloader/releases/download/v2026.03.31.0/InvoiceFlowAI-portable-2026.03.31.0.zip)
- Release page:
  [InvoiceFlowAI v2026.03.31.0](https://github.com/Ethan-YoungQ/Invoice-Downloader/releases/tag/v2026.03.31.0)

The current public release is the portable package. The installer is not published as a recommended download in this release.

## What It Does

- Connects to IMAP mailboxes such as QQ Mail and 163 Mail
- Extracts invoice attachments and invoice links from email messages
- Identifies common invoice document types
- Saves output into organized local folders by category and date
- Separates non-target-company invoices and manual-review cases

## Basic Workflow

1. Download and extract the portable package.
2. Run `InvoiceFlowAI.exe`.
3. In `Start Setup`, configure mailbox access, GLM API key, output folder, target company, and date range.
4. Start the extraction run.
5. Review progress in `Processing Center`.
6. Review results in `Result Analysis` and open the output folder.

## Output Folders

Common output folders include:

- `Food`
- `Hotel`
- `Train`
- `Taxi`
- `Other`
- `NonTargetCompany`
- `ManualReview`
- `_audit_retention`

Meaning:

- `NonTargetCompany`: invoices whose purchaser does not match the configured target company
- `ManualReview`: documents that require manual confirmation
- `_audit_retention`: retained audit artifacts, not successful archive output

## System Requirements

- Windows 10 or Windows 11
- Python 3.12 for source execution
- Working IMAP mailbox access
- Valid GLM API key

## Run From Source

```powershell
python main.py
```

## Build Releases

Prepare runtime dependencies:

```powershell
build\windows\prepare_runtime.ps1
```

Build release artifacts:

```powershell
build\windows\build_release.ps1
```

## Privacy

- This repository does not publish personal mailbox credentials, API keys, truth datasets, invoice samples, or diagnostic output.
- Local runtime settings stay on the local machine and are not part of the public repository.
- Public releases exclude diagnostics, handoff notes, screenshots, and intermediate packaging artifacts.
