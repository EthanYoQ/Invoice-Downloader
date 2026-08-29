# InvoiceFlowAI — E-Invoice Organization and Reimbursement Preparation

<div align="center">

**English** | [中文](README.md)

</div>




<div align="center">

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-Apache--2.0-blue)
![AI](https://img.shields.io/badge/AI-GLM--4.5V%20%7C%20GLM--OCR-purple)
![Platform](https://img.shields.io/badge/Platform-Windows%2011%20%7C%20macOS-lightblue)
![Status](https://img.shields.io/badge/Status-Stable-brightgreen)

**Turn PDF, OFD, and XML e-invoices from your mailbox, plus supported invoice download links, into organized files and an Excel reimbursement summary.**

Designed for individuals, freelancers, and small teams that manually collect multiple e-invoices each month. Connect your own QQ Mail or 163 Mail account to collect invoices in batches, run OCR, archive them by category, and create a summary while retaining low-confidence results for human review.

Download: [latest Windows installer / portable package and macOS (Apple Silicon) installer](https://github.com/EthanYoQ/Invoice-Downloader/releases/latest)

*Email and invoice files are processed locally. If you enable GLM OCR / vision recognition, invoice images are sent to your configured model provider for extraction.*

</div>

<p align="center">
  <img src="./docs/images/invoiceflowai-hero-en.png" alt="InvoiceFlowAI desktop invoice assistant with setup, processing, analysis, and safety screens" />
</p>

## DeepSeek Harness Plugin

`@ethanyoq/dsh-invoice-downloader` brings local IMAP invoice download, OCR, archiving, and Excel summaries to a right-side DeepSeek Harness panel. It is intended for reimbursement workflows that automate repetitive organizing while keeping a human review step.

```sh
dsh plugin --profile web add @ethanyoq/dsh-invoice-downloader
dsh web --profile web
```

Open **Invoice Downloader** from the right-side entry, choose an output directory, install the local engine once, and enter a QQ Mail or 163 Mail IMAP authorization code. First-time setup needs network access to download Python dependencies and Chromium; the `rapidocr-onnxruntime` pip wheel includes the default PP-OCRv3 models, so no separate manual model download is required. Invoice files are OCRed locally; OCR text is sent to the currently selected DeepSeek model for field extraction. The plugin supports Windows x64 and macOS Apple Silicon. The DSH credential service stores authorization codes, never plugin settings or scan logs.

---

## ✨ Key Highlights

| &nbsp; | Feature | Description |
|--------|---------|-------------|
| 🔒 | **One-click launch, ready to use** | Extract and run — no need to install Python or any dependencies |
| 🤖 | **Dual-engine AI recognition** | Track A (OCR precision flow) + Track B (vision fallback flow), automatic switching with no manual operation needed |
| 🔍 | **Four-layer smart funnel** | Whitelist domains → Subject keywords → Body detection → QR code scanning, precisely filtering non-invoice emails |
| 📄 | **Link-based invoice auto-recovery** | Playwright automatically opens Baiwang Cloud and tax platform links, downloading official PDF archives |
| 🧾 | **Automatic invoice/document pairing** | Hotel invoice/folio and ride invoice/itinerary pairs are archived with adjacent names |
| 🪟 | **Quiet background processing** | URL workers, Node, and Chromium run without repeatedly opening console windows |
| 🗂️ | **Natural language classification rules** | Supports custom rules like "Didi rides over 100 yuan go into the high-amount category" |
| 📊 | **One-click Excel report** | Automatically generates `summary_report.xlsx`, covering invoice lists and amount summaries |

---

## 🏗️ Overall Workflow

```mermaid
flowchart LR
    A["📧 QQ / 163\nMailbox IMAP"] --> B["📥 Email Fetching\nEmailFetcher"]
    B --> C{"🔍 Four-layer\nSmart Funnel"}
    C -->|"Pass"| D["📎 Attachment Extraction\nZIP Recursive Unpack"]
    C -->|"Discard"| X["🗑️ Non-invoice Emails"]
    D --> E{"Attachment Type?"}
    E -->|"PDF/OFD/XML"| F["🤖 AI Extraction Engine"]
    E -->|"URL Link"| G["🌐 PDF Recovery\nPlaywright"]
    G --> F
    F --> H{"Extraction\nSuccessful?"}
    H -->|"✅"| I["📂 Smart Classification\nRule-based Renaming"]
    H -->|"❌"| J["📁 Manual_Check"]
    I --> K["🗂️ Local Archiving"]
    K --> L["📊 Excel Report"]
```

---

## 🤖 Dual-Engine AI Extraction Architecture

The system employs **Track A + Track B + Local Fallback** as a three-layer defense. If any layer succeeds, its result is adopted, ensuring an extremely high recognition success rate.

![Dual-Engine AI Recognition Architecture](docs/track-ab.svg)

> **Why this design?**
> - **Track A** (OCR + LLM): Highest precision — extracts text structure first, then understands it
> - **Track B** (glm-4.5V Vision): Directly "looks at the image" — ideal for complex layouts or image-based invoices
> - **Local Fallback**: Local regex rules — works offline with zero API consumption

---

## 🔍 Four-Layer Smart Filtering Funnel

The system does not call AI for every email. Instead, it first passes through a four-layer funnel for precise filtering, significantly reducing false recognition rates and API costs.

![Four-Layer Smart Filtering Funnel](docs/funnel.svg)

After passing the filter, attachments also go through a **three-tier decision process**:

| Tier | Trigger Condition | Handling |
|------|-------------------|----------|
| 🗑️ **Tier A** (Discard) | Tracking pixels, logos, decorative images (≤32px) | Skipped directly |
| 📦 **Tier B** (Hold) | Attachments >5MB, ZIP unpack failure | Retained but not processed |
| ✅ **Tier C** (Archive) | Normal PDF/OFD/XML | Enters AI extraction pipeline |

---

## 🌐 Three-Tier PDF Recovery Strategy

Many invoice emails only contain "click to download" links. The system automatically identifies the platform and selects the optimal approach:

```mermaid
flowchart TD
    Link(["🔗 Invoice Links in Email"]) --> Detect{"Identify Link Platform"}
    Detect -->|"Baiwang Cloud"| BW["🏢 Playwright Automation\nLogin → Click Download → Capture File\nField Matching Verification"]
    Detect -->|"Tax Bureau · Nuonuo · Aisino"| DI["📥 Direct HTTP Download\nIdentify Invoice Family\nLocal Field Validation"]
    Detect -->|"Unknown Webpage"| Generic["🌐 Web to PDF\nDetect Login/Captcha\nA4 Format Rendering"]
    BW & DI & Generic --> Final(["📄 Local PDF"])
    Final --> AI(["🤖 AI Extraction Pipeline"])
```

---

## ⚙️ Configuration Guide

> Only needs to be configured once on first use; after that, just click run for each scan.

### Step 1 · Enable 163 Mailbox IMAP

<details>
<summary>📖 Click to expand detailed steps for 163 mailbox</summary>

**Server Parameters**

| Parameter | Value |
|-----------|-------|
| IMAP Server | `imap.163.com` |
| Port | `993` (SSL/TLS) |

**Setup Steps**

1. Log in to [mail.163.com](https://mail.163.com), click **Settings** in the upper right corner
2. Select **POP3/SMTP/IMAP** from the dropdown menu
3. Find **IMAP/SMTP Service**, click the **Enable** button on the right
4. An "Account Security Verification" window will appear:
   - **QR Code Method** (Recommended): Scan the QR code with your phone to automatically send a verification SMS
   - **Manual Method**: Manually send an SMS to the designated number as prompted
5. After sending the SMS, click **I Have Sent It**
6. The system generates a **16-character authorization code** (letter combination, **only displayed once — copy and save it immediately**)

> ⚠️ The authorization code is not your mailbox login password. It is a separate password specifically for third-party clients, and it is case-sensitive.

📚 [163 Mailbox Official Help](https://help.mail.163.com/)

</details>

---

### Step 2 · Enable QQ Mailbox IMAP

<details>
<summary>📖 Click to expand detailed steps for QQ mailbox</summary>

**Server Parameters**

| Parameter | Value |
|-----------|-------|
| IMAP Server | `imap.qq.com` |
| Port | `993` (SSL/TLS) |

**Setup Steps**

1. Log in to [mail.qq.com](https://mail.qq.com), click the **Settings** icon in the upper right corner
2. Select the **Account** tab
3. Find **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV Service**
4. Click **Manage Service** → **Enable Service**
5. Click **Generate Authorization Code** and complete identity verification:
   - **QR Code Method** (Recommended): Scan the QR code with your phone to automatically send a verification SMS
   - **Manual Method**: Send "Configure Email Client" to **1069070069** using your QQ-linked phone
6. Click **I Have Sent It** — the authorization code is generated instantly after verification (**save it immediately**)

> ⚠️ The authorization code automatically becomes invalid after changing your QQ password and must be regenerated.

📚 [QQ Mailbox Official Help](https://service.mail.qq.com/detail/0/339)

</details>

---

### Step 3 · Obtain Zhipu GLM API Key

<details>
<summary>📖 Click to expand GLM API configuration steps</summary>

The system uses **GLM-4.5V** (multimodal vision) and **GLM-OCR** to recognize invoice content.

**Steps**

1. Visit [open.bigmodel.cn](https://open.bigmodel.cn/) and register an account
2. Go to Console → **API Keys** → **Create API Key**
3. Copy and save the Key (format: `xxxxxxxx.xxxxxxxxxxxxxxxx`)

**Cost Reference**

| Scenario | Description |
|----------|-------------|
| 🎁 New User Bonus | 5 million GLM-4 tokens gifted (valid for 30 days) |
| 💰 Recommended Top-up | **Under 5 yuan**, pay-as-you-go |
| 📊 Usage Estimate | Each invoice consumes approximately 1,000–3,000 tokens; for 200 invoices/month, 5 yuan lasts about 12 months |

📚 [Zhipu AI Open Platform](https://open.bigmodel.cn/)

</details>

---

## 🚀 Quick Start

### Windows (installer / portable)

1. Installer: run `InvoiceFlowAI-*-windows-x64-setup.exe`. Portable: extract `InvoiceFlowAI-*-windows-x64-portable.zip` to a regular folder (avoid cloud-sync directories) and keep `_internal` beside `InvoiceFlowAI.exe`.
2. Double-click `InvoiceFlowAI.exe`; the settings interface appears on first launch.
3. Enter your email address and authorization code (QQ or 163) and GLM API Key, then click "Start Scanning". Invoices are archived to the "Invoice Organizer" folder on your desktop.

### macOS (Apple Silicon)

1. Open `InvoiceFlowAI-*-macos-arm64.dmg` and drag `InvoiceFlowAI` to Applications.
2. The settings interface appears on first launch. This build is unsigned and not notarized; if macOS blocks it, confirm opening it in **System Settings → Privacy & Security**.
3. Enter your email address and authorization code (QQ or 163) and GLM API Key, then click "Start Scanning".

---

## 📁 Output Directory Structure

```
Invoice Organizer/
├── Train Tickets/
│   └── 20260315-Beijing-Shanghai-Train-Ticket.pdf
├── Flight Tickets/
│   └── 20260301_Flight_1280.00_Air-China.pdf
├── Hotel Invoices/
│   ├── 20260310-Hotel-01-Invoice_888.00.pdf
│   └── 20260310-Hotel-01-Folio_888.00.pdf
├── Taxi/
│   ├── 0312-DiDi-01-Invoice_45.50.pdf
│   └── 0312-DiDi-01-Itinerary_45.50.pdf
├── Dining/
├── Manual Review/     ← Materials that cannot be confirmed reliably
├── Non-target Company Invoices/
└── summary_report.xlsx
```

---

## ❓ FAQ

<details>
<summary>Q: White screen or no response after launching the software?</summary>

- Make sure the **entire archive is extracted**; the `_internal` folder must be in the same directory as `InvoiceFlowAI.exe`
- Avoid placing the software in a directory containing **Chinese characters or spaces**

</details>

<details>
<summary>Q: Very few invoices found after scanning?</summary>

- In the software's time range settings, **adjust the start date to more than 180 days ago**
- Some mailboxes default to fetching only the last 30 days of emails — select "Fetch all emails" in the mailbox IMAP settings

</details>

<details>
<summary>Q: Authentication failure after entering the authorization code?</summary>

- **QQ Mailbox**: Must be obtained through "Manage Service → Generate Authorization Code" — it is **not your QQ password**
- **163 Mailbox**: Must be generated from the "Enable IMAP Service" popup — it is **not your mailbox login password**, and it is case-sensitive
- The QQ authorization code must be regenerated after changing your QQ password

</details>

<details>
<summary>Q: GLM API reports insufficient balance?</summary>

Log in to [open.bigmodel.cn](https://open.bigmodel.cn/) → Billing Center → Top Up. Recommended top-up of **5 yuan**, pay-as-you-go.

</details>

<details>
<summary>Q: Some invoices end up in the Manual_Check folder?</summary>

This is normal. When AI recognition confidence is insufficient, the system automatically places invoices in the `Manual_Check` queue for manual confirmation. This is usually caused by blurry images, non-standard documents, or encrypted PDFs.

</details>

---

## 🛡️ Privacy & Security

- All emails and invoice files are processed **locally** and are never uploaded to any server
- Mailbox credentials are encrypted with **DPAPI** on Windows and stored in **Keychain** on macOS
- The GLM API only receives **invoice images** (Base64) for text recognition and does not send the original email content

---

## ⚠️ Disclaimer

By using this software, you acknowledge and accept the following terms.

**Compliant Use** · This software accesses mailboxes through IMAP in **read-only** mode. It does not send, delete, or modify any emails. Users must ensure they have legitimate authorization for the mailboxes being processed.

**Purpose** · This software is intended only as an automation assistant for downloading, recognizing, classifying, and archiving invoice-related emails and files.

**Accuracy & Compliance** · The author does not warrant the accuracy, completeness, legality, tax compliance, financial compliance, or accounting compliance of any invoice data or generated results. Users must independently verify all invoices, reimbursements, tax filings, accounting records, and compliance outcomes before relying on them.

**Data** · When calling the GLM API, invoice images are sent to Zhipu AI servers for recognition, subject to the [Zhipu AI Privacy Policy](https://www.zhipuai.cn/zh/privacy). Original email content is never sent.

**Liability** · The author is not liable for any losses, omissions, errors, failed reimbursements, tax risks, compliance issues, or data loss arising from use of this software.

**Third-Party Services**

| Service | Purpose | Provider |
|---------|---------|----------|
| Zhipu GLM API | Invoice OCR and visual recognition | Beijing Zhipu Huazhang Technology Co., Ltd. |
| QQ Mailbox IMAP | Email reading | Tencent Technology (Shenzhen) Co., Ltd. |
| 163 Mailbox IMAP | Email reading | NetEase (Hangzhou) Network Co., Ltd. |

---

## 📜 License

Licensed under the [Apache License 2.0](LICENSE). Commercial use, modification, distribution, and closed-source integration are permitted, provided that redistributions retain the copyright notice, license notice, and author attribution in [NOTICE](NOTICE).

---

<div align="center">

Made with ❤️ by **EthanYoQ / Yong Qi**

[Report Issues](https://github.com/EthanYoQ/Invoice-Downloader/issues) · [Zhipu AI Open Platform](https://open.bigmodel.cn/) · [163 Mailbox Help](https://help.mail.163.com/) · [QQ Mailbox Help](https://service.mail.qq.com/detail/0/339)

</div>
