# InvoiceFlowAI / 发票助手 / Invoice Downloader — 邮箱电子发票自动下载、OCR 识别与 Excel 报销汇总

<div align="center">

[English](README.en.md) | **中文**

</div>




<div align="center">

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-Apache--2.0-blue)
![AI](https://img.shields.io/badge/AI-GLM--4.5V%20%7C%20GLM--OCR-purple)
![Platform](https://img.shields.io/badge/Platform-Windows%2011-lightblue?logo=windows)
![Status](https://img.shields.io/badge/Status-Stable-brightgreen)

**个人职场报销用的 Windows 发票助手 / 发票管家：连接 QQ 邮箱 / 163 邮箱 → 自动下载 PDF/OFD/XML 电子发票和百望云发票链接 → AI OCR 识别 → 本地分类归档 → 生成 Excel 报销汇总**

适合这些搜索和需求：**发票助手**、**发票管家**、**报销助手**、**电子发票归档**、**发票报销**、**QQ邮箱发票自动下载**、**163邮箱电子发票归档**、**OFD发票下载**、**百望云发票下载**、**Windows发票管理**。

下载：[最新版 Windows 免安装包](https://github.com/EthanYoQ/Invoice-Downloader/releases/latest)

*邮件和发票文件在本地处理。启用 GLM OCR / 视觉识别时，发票图片会发送到你配置的模型服务商用于提取。*

</div>

<p align="center">
  <img src="./docs/images/invoiceflowai-hero-zh.png" alt="InvoiceFlowAI Windows 发票助手真实启动配置、处理中心与安全提示界面" />
</p>

---

## v2026.07.13.1 · 可靠性与后台体验更新

本版本围绕“发票必须完整抓回、正确归档、正确撮合”重构了关键链路，并完成一次基于最终真值集的 Windows 前端全量跑批验收。

### 结果改进

| 验收项 | 本版结果 |
|--------|----------|
| 真值集覆盖 | QQ `INBOX`，邮件日期 `2025-11-25` 至 `2026-06-14`，共 `215` 份应归档凭证 |
| P0 · 漏票 | `0`，`215 / 215` 唯一匹配 |
| P1 · 分类、字段或归档错误 | `0` |
| P2 · 发票与配套凭证未撮合 | `0` |
| 撮合验证 | `10 / 10` 组住宿发票与水单、`16 / 16` 组打车发票与行程单 |
| Windows 后台窗口 | Playwright Node 与 Chromium 均完成真实启动验证，可见后台控制台窗口 `0` |
| 自动化回归 | `818` 项 pytest 通过、`109` 项子测试通过 |

> 上述数字是指定邮箱、指定日期窗口与对应最终真值集的验收结果，用于说明本版回归基线，不代表对任意邮箱内容作无条件准确率承诺。

### 实现方法

- **可终止的 URL 恢复工作池**：把 Playwright 链接恢复隔离到有并发上限、超时边界和进程树回收能力的 worker，单个网页卡死不会阻塞整批任务。
- **先直连、后浏览器的恢复策略**：优先使用供应商直链和本地字段验证，仅在必要时进入浏览器恢复；所有恢复路径都写入明确终态，失败不再被静默吞掉。
- **证据绑定与失败关闭**：归档结果绑定来源邮件、供应商身份和最终文件证据；部分写入、碰撞、异常退出和不完整结果不会被当作成功。
- **确定性撮合与相邻命名**：住宿发票/水单、打车发票/行程单按业务键撮合，并使用相同日期、序号和金额生成相邻文件名。
- **三层 Windows 无窗口启动**：主 worker、Playwright Node 驱动和 Chromium 子进程分别采用 Windows 隐藏启动策略，避免批量恢复时反复弹出黑色控制台窗口。

完整版本与免安装包：[v2026.07.13.1 Release](https://github.com/EthanYoQ/Invoice-Downloader/releases/tag/v2026.07.13.1)

---

## 🎬 视频介绍

https://github.com/user-attachments/assets/ae945367-35d3-4412-9fa0-c3bde80e2de5

## 🖥️ 软件界面预览

### 启动配置：邮箱、识别引擎、日期范围与本地输出

![InvoiceFlowAI 启动配置界面，使用示例邮箱、遮罩凭据、日期范围和本地归档配置](docs/images/invoiceflowai-setup-zh.png)

### 处理中心：进度、计数与实时日志

![InvoiceFlowAI 处理中心，展示任务进度、已扫描邮件、已识别发票、异常处理和实时日志](docs/images/invoiceflowai-processing-zh.png)

### 安全提示：使用结果前进行人工复核

![InvoiceFlowAI 免责声明，说明邮箱授权、API 使用、数据安全和人工复核要求](docs/images/invoiceflowai-disclaimer-zh.png)

---

## ✨ 核心亮点

| &nbsp; | 特性 | 说明 |
|--------|------|------|
| 🔒 | **一键运行，开箱即用** | 解压即可运行，无需安装 Python 或任何依赖 |
| 🤖 | **双引擎 AI 识别** | Track A（OCR精确流）+ Track B（视觉降级流），自动切换，无需手动操作 |
| 🔍 | **四层智能漏斗** | 白名单域名 → 主题关键字 → 正文检测 → 二维码扫描，精准过滤非发票邮件 |
| 📄 | **链接发票自动恢复** | Playwright 自动打开百望云、税务平台链接，下载正式 PDF 存档 |
| 🧾 | **发票与凭证自动撮合** | 住宿发票/水单、打车发票/行程单自动配对并相邻命名归档 |
| 🪟 | **安静的后台处理** | URL 恢复 worker、Node 与 Chromium 全链路隐藏运行，不再批量弹出控制台窗口 |
| 🗂️ | **自然语言分类规则** | 支持"滴滴大于100元放进大额"这样的自定义规则 |
| 📊 | **一键 Excel 报表** | 自动生成 `summary_report.xlsx`，发票清单、金额汇总全覆盖 |

---

## 🏗️ 整体工作流程

```mermaid
flowchart LR
    A["📧 QQ / 163\n邮箱 IMAP"] --> B["📥 邮件抓取\nEmailFetcher"]
    B --> C{"🔍 四层\n智能漏斗"}
    C -->|"通过"| D["📎 附件提取\nZIP 递归解包"]
    C -->|"丢弃"| X["🗑️ 非发票邮件"]
    D --> E{"附件类型?"}
    E -->|"PDF/OFD/XML"| F["🤖 AI 提取引擎"]
    E -->|"URL 链接"| G["🌐 PDF 恢复\nPlaywright"]
    G --> F
    F --> H{"提取\n成功?"}
    H -->|"✅"| I["📂 智能分类\n规则重命名"]
    H -->|"❌"| J["📁 Manual_Check"]
    I --> K["🗂️ 本地归档"]
    K --> L["📊 Excel 报表"]
```

---

## 🤖 双引擎 AI 提取架构

系统采用 **Track A + Track B + Local Fallback** 三层防线，任何一层成功即采用结果，确保极高的识别成功率。

![双引擎AI识别架构](docs/track-ab.svg)

> **为什么这样设计？**
> - **Track A**（OCR + LLM）：精度最高，先提取文字结构再理解
> - **Track B**（glm-4.5V 视觉）：直接"看图"，适合复杂排版或图片类发票
> - **Local Fallback**：本地正则规则，断网可用，零 API 消耗

---

## 🔍 四层智能筛选漏斗

系统不会对每封邮件都调用 AI，而是先经过四层漏斗精准判断，大幅降低误识别率和 API 费用。

![四层智能筛选漏斗](docs/funnel.svg)

筛选通过后，附件还会经过 **三级决策**：

| 层级 | 触发条件 | 处理方式 |
|------|----------|----------|
| 🗑️ **A 层**（丢弃） | Tracking pixel、Logo、装饰图（≤32px） | 直接跳过 |
| 📦 **B 层**（暂存） | 附件 >5MB、ZIP 解包失败 | 保留但不处理 |
| ✅ **C 层**（归档） | 正常 PDF/OFD/XML | 进入 AI 提取流程 |

---

## 🌐 三级 PDF 恢复方案

许多发票邮件只有"点击下载"的链接，系统自动识别平台并选择最优方案：

```mermaid
flowchart TD
    Link(["🔗 邮件中的发票链接"]) --> Detect{"识别链接平台"}
    Detect -->|"百望云"| BW["🏢 Playwright 自动化\n登录 → 点击下载 → 捕获文件\n字段匹配验证"]
    Detect -->|"国税·诺诺·航信"| DI["📥 HTTP 直接下载\n识别发票族群\n本地字段校验"]
    Detect -->|"未知网页"| Generic["🌐 网页转 PDF\n检测登录/验证码\nA4 格式渲染"]
    BW & DI & Generic --> Final(["📄 本地 PDF"])
    Final --> AI(["🤖 AI 提取流程"])
```

---

## ⚙️ 配置指南

> 首次使用只需配置一次，之后每次扫描直接点击运行。

### 第一步 · 开启 163 邮箱 IMAP

<details>
<summary>📖 点击展开 163 邮箱详细步骤</summary>

**服务器参数**

| 参数 | 值 |
|------|----|
| IMAP 服务器 | `imap.163.com` |
| 端口 | `993`（SSL/TLS） |

**开启步骤**

1. 登录 [mail.163.com](https://mail.163.com)，点击右上角「**设置**」
2. 在下拉菜单中选择「**POP3/SMTP/IMAP**」
3. 找到「**IMAP/SMTP 服务**」，点击右侧「**开启**」按钮
4. 弹出「账号安全验证」窗口：
   - **扫码方式**（推荐）：手机扫描二维码，自动发送验证短信
   - **手动方式**：按提示手动发送短信到指定号码
5. 短信发送后点击「**我已发送**」
6. 系统生成 **16 位授权码**（字母组合，**仅显示一次，务必立即复制保存**）

> ⚠️ 授权码不是邮箱登录密码，是专用于第三方客户端的独立密码，大小写敏感。

📚 [163 邮箱官方帮助](https://help.mail.163.com/)

</details>

---

### 第二步 · 开启 QQ 邮箱 IMAP

<details>
<summary>📖 点击展开 QQ 邮箱详细步骤</summary>

**服务器参数**

| 参数 | 值 |
|------|----|
| IMAP 服务器 | `imap.qq.com` |
| 端口 | `993`（SSL/TLS） |

**开启步骤**

1. 登录 [mail.qq.com](https://mail.qq.com)，点击右上角「**设置**」图标
2. 选择「**账户**」选项卡
3. 找到「**POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务**」
4. 点击「**管理服务**」→「**开启服务**」
5. 点击「**生成授权码**」，进行身份验证：
   - **扫码方式**（推荐）：手机扫码后自动发送验证短信
   - **手动方式**：用 QQ 绑定手机发送「**配置邮件客户端**」到 **1069070069**
6. 点击「**我已发送**」，验证后授权码即时生成（**请立即保存**）

> ⚠️ 修改 QQ 密码后授权码自动失效，需重新生成。

📚 [QQ 邮箱官方帮助](https://service.mail.qq.com/detail/0/339)

</details>

---

### 第三步 · 获取智谱 GLM API Key

<details>
<summary>📖 点击展开 GLM API 配置步骤</summary>

系统使用 **GLM-4.5V**（多模态视觉）和 **GLM-OCR** 识别发票内容。

**步骤**

1. 访问 [open.bigmodel.cn](https://open.bigmodel.cn/)，注册账号
2. 进入控制台 → **API Keys** → **创建 API Key**
3. 复制并保存 Key（格式：`xxxxxxxx.xxxxxxxxxxxxxxxx`）

**费用参考**

| 情况 | 说明 |
|------|------|
| 🎁 新用户福利 | 赠送 500 万 GLM-4 tokens（30 天有效） |
| 💰 推荐充值 | **5 元以内**，按量计费 |
| 📊 使用估算 | 每张发票约消耗 1,000–3,000 tokens；每月 200 张，5 元可用约 12 个月 |

📚 [智谱 AI 开放平台](https://open.bigmodel.cn/)

</details>

---

## 🚀 快速开始

```
Step 1  解压软件包到普通文件夹（避免云盘同步目录）
        保持 _internal 文件夹与 InvoiceFlowAI.exe 同级
        ↓
Step 2  双击运行 InvoiceFlowAI.exe
        首次启动自动弹出设置界面
        ↓
Step 3  填入配置并保存：
        · 邮箱地址 + 授权码（QQ 或 163）
        · GLM API Key
        ↓
        点击「开始扫描」→ 等待完成
        发票自动归档到桌面「发票整理」文件夹 ✅
```

---

## 📁 输出目录结构

```
发票整理/
├── 火车票/
│   └── 20260315-北京-上海-火车票.pdf
├── 机票/
│   └── 20260301_机票_1280.00_中国国际航空.pdf
├── 住宿发票/
│   ├── 20260310-住宿-01-发票_888.00元.pdf
│   └── 20260310-住宿-01-水单_888.00元.pdf
├── 打车/
│   ├── 0312-滴滴-01-发票_45.50元.pdf
│   └── 0312-滴滴-01-行程单_45.50元.pdf
├── 餐饮/
├── 待人工复核/        ← 无法可靠确认的材料，需人工处理
├── 非目标公司发票/
└── summary_report.xlsx
```

---

## ❓ 常见问题

<details>
<summary>Q：软件启动后白屏或无响应？</summary>

- 确认已将**整个压缩包解压**，`_internal` 文件夹须与 `InvoiceFlowAI.exe` 在同一目录
- 避免将软件放在含有**中文路径或空格**的目录下

</details>

<details>
<summary>Q：扫描完发票数量很少？</summary>

- 在软件的时间范围设置中，将起始日期**往前调整至 180 天以上**
- 部分邮箱默认只拉取近30天邮件，需要在邮箱 IMAP 设置里选择「收取全部邮件」

</details>

<details>
<summary>Q：授权码填写后提示认证失败？</summary>

- **QQ 邮箱**：须从「管理服务 → 生成授权码」流程中获取，**不是 QQ 密码**
- **163 邮箱**：须从「开启 IMAP 服务」弹窗中生成，**不是邮箱登录密码**，注意大小写
- QQ 修改密码后需重新生成授权码

</details>

<details>
<summary>Q：GLM API 报错余额不足？</summary>

登录 [open.bigmodel.cn](https://open.bigmodel.cn/) → 费用中心 → 充值。推荐充值 **5 元**，按量计费。

</details>

<details>
<summary>Q：部分发票进入 Manual_Check 文件夹？</summary>

正常现象。当 AI 识别置信度不足时，系统自动放入 `Manual_Check` 队列，需人工确认。通常由图片模糊、非标准票据或加密 PDF 导致。

</details>

---

## 🛡️ 隐私与安全

- 所有邮件、发票文件均在**本地处理**，不上传任何服务器
- 邮箱凭据通过 **Windows DPAPI** 加密存储，只有当前 Windows 账户可解密
- GLM API 仅接收**发票图片**（Base64）用于文字识别，不发送邮件原文内容

---

## ⚠️ 免责声明

使用本软件即表示您已理解并接受以下内容。

**合规使用** · 本软件通过 IMAP **只读**访问邮箱，不发送、删除或修改任何邮件。用户须确保对所处理邮箱拥有合法授权。

**用途** · 本软件仅用于发票邮件下载、识别、分类、归档等自动化辅助。

**准确性与合规性** · 作者不保证发票数据或生成结果的准确性、完整性、合法性、税务合规性、财务合规性或会计合规性。用户必须自行核验所有发票、报销、税务、会计和合规结果后再使用。

**数据** · 调用 GLM API 时，发票图片会发送至智谱 AI 服务器进行识别，受 [智谱 AI 隐私政策](https://www.zhipuai.cn/zh/privacy) 约束；邮件原文不会发送。

**责任限制** · 作者不对使用本软件造成的损失、遗漏、错误、报销失败、税务风险、合规问题或数据丢失承担责任。

**第三方服务**

| 服务 | 用途 | 服务方 |
|------|------|--------|
| 智谱 GLM API | 发票 OCR 与视觉识别 | 北京智谱华章科技有限公司 |
| QQ 邮箱 IMAP | 邮件读取 | 腾讯科技（深圳）有限公司 |
| 163 邮箱 IMAP | 邮件读取 | 网易（杭州）网络有限公司 |

---

## 📜 许可证

本项目基于 [Apache License 2.0](LICENSE) 授权。允许商业使用、修改、分发和闭源集成，但再分发时必须保留 copyright notice、license notice，以及 [NOTICE](NOTICE) 中的作者署名。

---

<div align="center">

Made with ❤️ by **EthanYoQ / Yong Qi**

[报告问题](https://github.com/EthanYoQ/Invoice-Downloader/issues) · [智谱AI开放平台](https://open.bigmodel.cn/) · [163邮箱帮助](https://help.mail.163.com/) · [QQ邮箱帮助](https://service.mail.qq.com/detail/0/339)

</div>
