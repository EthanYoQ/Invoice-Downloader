# Invoice Downloader for DeepSeek Harness

中文 | [English](#english)

在 DeepSeek Harness 的右侧边栏中运行本地 IMAP 发票下载：读取 QQ 邮箱或 163 邮箱、下载发票附件、在本地 OCR、按规则归档并生成 Excel 汇总。

## 安装

```powershell
dsh plugin --profile web add @ethanyoq/dsh-invoice-downloader
```

启动 DSH Web，打开右侧的“发票下载”入口，先点击“安装本地引擎”，再选择保存位置并完成 IMAP 配置。运行时安装仅在本机创建 Python 虚拟环境；不会在安装包或插件设置中保存邮箱授权码或 API 密钥。

## 支持的平台

- Windows x64
- macOS Apple Silicon

首次运行需要可用的 Python 3.10+。插件将可变的本地运行时安装在自己的 DSH profile 状态目录中；安装包内的引擎资源保持只读，扫描结果只写入你选择的输出目录。

## 数据处理

默认链路在本地对发票文件进行 OCR，并将 OCR 文本提交给当前已选择的 DeepSeek 模型以提取字段。启用可选 GLM 路径时，图像会发送给 GLM 服务。邮箱授权码和 GLM 密钥由 DSH 凭据服务管理，不写入插件设置或扫描日志。

扫描只读访问 IMAP 邮箱。请在报销、入账、税务或合规使用前人工核验所有结果。

## 开发与打包

```powershell
npm install
npm test
npm pack
```

`npm pack` 会从仓库根目录的 Python 源码生成包内的只读引擎副本。构建产物不含发票、邮箱地址、凭据、输出目录、DSH profile 或缓存。

## English

Run local IMAP invoice download, OCR, archive, and Excel summary workflows from a right-side DeepSeek Harness panel. Install the bundle with:

```sh
dsh plugin --profile web add @ethanyoq/dsh-invoice-downloader
```

The package supports Windows x64 and macOS Apple Silicon. Use the panel to install its local Python runtime, choose an output directory, and configure IMAP. Credentials are stored through the DSH credential service rather than plugin settings.

The default flow OCRs invoice files locally and sends OCR text to the selected DeepSeek model for field extraction. Optional GLM mode can send images to GLM. Review every invoice result before using it for reimbursement, accounting, tax, or compliance work.
