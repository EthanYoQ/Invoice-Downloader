# InvoiceFlowAI

InvoiceFlowAI 是一个 Windows 桌面端发票抓取与归档工具，用于从邮箱中提取发票附件和发票链接，识别票据类型，并按目录自动整理输出。

## 适用场景

- 从 QQ 邮箱、163 邮箱批量拉取发票邮件
- 自动识别常见票据类型
- 按日期和类型归档到本地目录
- 将非目标公司票据、待人工复核票据分流保存

## 当前发布

- 便携版下载：
  [InvoiceFlowAI-portable-2026.03.31.0.zip](https://github.com/Ethan-YoungQ/Invoice-Downloader/releases/download/v2026.03.31.0/InvoiceFlowAI-portable-2026.03.31.0.zip)
- Release 页面：
  [InvoiceFlowAI v2026.03.31.0](https://github.com/Ethan-YoungQ/Invoice-Downloader/releases/tag/v2026.03.31.0)

当前公开发布物为便携版压缩包。下载安装后解压，直接运行 `InvoiceFlowAI.exe` 即可。

## 功能概览

- 邮箱连接配置
  - 支持 IMAP 邮箱登录
  - 支持连接测试与授权码方式接入
- 发票提取
  - 支持附件 PDF、图片、发票链接
  - 保留 provider 恢复链，兼顾百望等常见短链场景
- 自动归档
  - 按票据类型输出到对应目录
  - 支持目标公司过滤
  - 支持待人工复核与暂存记录分流
- 桌面体验
  - Windows 桌面应用
  - 内置发行版图标
  - 支持便携版直接解压运行

## 使用方法

1. 下载并解压便携版压缩包。
2. 运行 `InvoiceFlowAI.exe`。
3. 在 `启动配置` 页填写邮箱地址、授权信息、GLM API Key、输出目录和目标公司。
4. 选择提取日期范围。
5. 点击 `开始提取`。
6. 在 `处理中心` 查看进度与实时日志。
7. 在 `结果分析` 查看归档结果，并打开输出目录。

## 输出目录说明

程序会在你指定的输出目录中生成归档结果，常见目录包括：

- `餐饮`
- `住宿发票`
- `火车票`
- `打车发票`
- `其他`
- `非目标公司发票`
- `待人工复核`
- `_audit_retention`

其中：

- `非目标公司发票`：购买方明确不匹配当前目标公司
- `待人工复核`：信息不足或置信度不足，需要人工确认
- `_audit_retention`：系统保全的运行审计材料，不属于成功归档目录

## 系统要求

- Windows 10 或 Windows 11
- Python 3.12
- 可访问的 IMAP 邮箱
- 可用的 GLM API Key

## 从源码运行

1. 准备 Python 3.12 环境。
2. 安装项目依赖。
3. 执行：

```powershell
python main.py
```

## 构建发行版

先准备运行时资源：

```powershell
build\windows\prepare_runtime.ps1
```

再执行构建：

```powershell
build\windows\build_release.ps1
```

构建完成后可在 `dist` 下获取便携版等发行产物。

## 隐私与安全

- 仓库不包含个人邮箱、授权码、API Key、真值集或历史测试票据
- 本地运行配置保存在系统目录，不跟随源码仓库发布
- GitHub Release 当前仅发布便携版压缩包，不包含诊断目录和中间构建产物

## 许可证

当前仓库未单独附带开源许可证文件。如需对外分发或商用，请先明确许可证策略。
