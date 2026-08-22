import * as path from 'node:path'
import type { CredentialProvider } from '@deepseek-ai/dsh-credentials'
import type { SubprocessHandle, SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import { GLM_API_KEY_REF, IMAP_AUTH_CODE_REF, isCredentialConfigured, resolveCredential } from './credentials.js'
import { ArtifactRegistry } from './file-security.js'
import { InvoiceScanner, type ScanRequest } from './invoice-scanner.js'
import { getRuntimeStatus, type RuntimeLayout } from './runtime.js'
import { readSettings, type InvoiceSettings, writeSettings } from './settings-store.js'
import { PRIVACY_DISCLOSURE, ScanSessionManager } from './storage.js'

export interface InvoiceRpcContext {
  savePath: string
  profileDir: string
  graceMs: number
  subprocess: SubprocessRuntime
  runtime: RuntimeLayout
  scanner: InvoiceScanner
  sessions: ScanSessionManager
  credentials: CredentialProvider
  artifacts: ArtifactRegistry
}

export interface RpcResult {
  ok: boolean
  value?: unknown
  error?: {
    code: 'bad-request' | 'internal'
    message: string
    details: { issues: [] } | Record<string, never>
  }
}

interface RuntimeHealth {
  ok: boolean
}

const ok = (value: unknown): RpcResult => ({ ok: true, value })
const badRequest = (message: string): RpcResult => ({
  ok: false,
  error: { code: 'bad-request', message, details: { issues: [] } },
})
const internal = (message: string): RpcResult => ({
  ok: false,
  error: { code: 'internal', message, details: {} },
})

export function createInvoiceRpcHandler(
  context: InvoiceRpcContext,
): (endpoint: string, payload: unknown, signal: AbortSignal) => Promise<RpcResult> {
  let installation: Promise<RpcResult> | null = null

  return async (endpoint, payload, signal) => {
    try {
      switch (endpoint) {
        case 'getRuntimeStatus':
          return ok(getRuntimeStatus(context.runtime))
        case 'installRuntime':
          if (!installation) {
            installation = installRuntime(context, signal).finally(() => {
              installation = null
            })
          }
          return await installation
        case 'testConnection':
          return await testConnection(context, signal)
        case 'testEmailAuth':
          return await testEmailAuth(context, payload, signal)
        case 'getSettings':
          return await getSettings(context)
        case 'saveSettings':
          return await saveSettings(context, payload)
        case 'startScan':
          return await startScan(context, payload)
        case 'stopScan':
          context.scanner.stopScan()
          return ok({ stopped: true })
        case 'getScanStatus':
          return ok(getScanStatus(context, payload))
        case 'getPrivacyDisclosure':
          return ok(PRIVACY_DISCLOSURE)
        case 'listArtifacts':
          return listArtifacts(context, payload)
        case 'downloadArtifact':
          return downloadArtifact(context, payload)
        default:
          return badRequest('未知的发票插件请求。')
      }
    } catch {
      return internal('发票插件请求未完成。')
    }
  }
}

async function installRuntime(context: InvoiceRpcContext, signal: AbortSignal): Promise<RpcResult> {
  const status = getRuntimeStatus(context.runtime)
  if (status.platform === 'unsupported') {
    return badRequest('此版本仅支持 Windows x64 和 macOS Apple Silicon。')
  }
  if (!status.enginePresent) {
    return internal('插件运行时不完整，请重新安装 npm 包。')
  }
  const bootstrapArgv = await resolveBootstrapArgv(context, signal)
  const result = await runCommand(
    context,
    [
      ...bootstrapArgv,
      context.runtime.installerPath,
      '--venv',
      context.runtime.venvRoot,
      '--requirements',
      context.runtime.requirementsPath,
      '--engine-root',
      context.runtime.engineRoot,
      '--health-check',
      context.runtime.healthCheckPath,
    ],
    signal,
  )
  if (result.exitCode !== 0) {
    return internal('本地引擎安装失败。请确认 Python 3.10+ 可用后重试。')
  }
  return testConnection(context, signal)
}

/** Chooses an installed, supported CPython version when Windows exposes the Python Launcher. */
async function resolveBootstrapArgv(context: InvoiceRpcContext, signal: AbortSignal): Promise<readonly string[]> {
  if (process.platform !== 'win32' || context.runtime.bootstrapPython) return context.runtime.bootstrapArgv
  const launcher = await runCommand(context, ['py', '-0p'], signal)
  if (launcher.exitCode !== 0) return context.runtime.bootstrapArgv
  const versions = [...launcher.stdout.matchAll(/-V:3\.(10|11|12|13)\b/g)]
    .map(match => Number(match[1]))
  const minor = versions.length > 0 ? Math.max(...versions) : undefined
  return minor ? ['py', `-3.${minor}`] : context.runtime.bootstrapArgv
}

async function testConnection(context: InvoiceRpcContext, signal: AbortSignal): Promise<RpcResult> {
  const status = getRuntimeStatus(context.runtime)
  if (status.platform === 'unsupported') {
    return badRequest('此版本仅支持 Windows x64 和 macOS Apple Silicon。')
  }
  if (!status.installed) {
    return badRequest('本地引擎尚未安装。')
  }
  const result = await runCommand(
    context,
    [
      context.runtime.pythonPath,
      context.runtime.healthCheckPath,
      '--engine-root',
      context.runtime.engineRoot,
    ],
    signal,
  )
  if (result.exitCode !== 0) return internal('本地引擎健康检查失败。')
  const health = parseHealth(result.stdout)
  return health.ok
    ? ok({ engineVersion: '0.1.0', ocrAvailable: true })
    : internal('本地引擎健康检查失败。')
}

async function testEmailAuth(
  context: InvoiceRpcContext,
  payload: unknown,
  signal: AbortSignal,
): Promise<RpcResult> {
  const request = asObject(payload)
  const email = stringValue(request.email)
  const authCode = stringValue(request.authCode) || await resolveCredential(context.credentials, IMAP_AUTH_CODE_REF)
  const server = imapServer(email)
  if (!server || !authCode) return badRequest('请填写 QQ 或 163 邮箱与 IMAP 授权码。')
  const status = getRuntimeStatus(context.runtime)
  if (!status.installed) return badRequest('请先安装本地引擎。')

  const script = [
    'import json, sys',
    'sys.path.insert(0, sys.argv[1])',
    'sys.path.insert(0, sys.argv[2])',
    'from invoice_engine.email_fetcher import EmailFetcher',
    'cfg = json.loads(sys.stdin.readline())',
    'try:',
    '  fetcher = EmailFetcher(cfg["email"], cfg["authCode"], imap_server=cfg["server"])',
    '  fetcher.connect()',
    '  fetcher.disconnect()',
    '  print(json.dumps({"ok": True}))',
    'except Exception:',
    '  print(json.dumps({"ok": False}))',
  ].join('\n')
  const result = await runPython(
    context,
    script,
    JSON.stringify({ email, authCode, server }),
    signal,
  )
  const success = result.exitCode === 0 && parseHealth(result.stdout).ok
  return ok({ ok: success, message: success ? '邮箱连接成功。' : '邮箱连接失败，请检查授权码和 IMAP 设置。' })
}

async function getSettings(context: InvoiceRpcContext): Promise<RpcResult> {
  const settings = readSettings(context.profileDir)
  return ok({
    ...settings,
    savePath: settings.savePath || context.savePath,
    hasAuthCode: await isCredentialConfigured(context.credentials, IMAP_AUTH_CODE_REF),
    hasGlmApiKey: await isCredentialConfigured(context.credentials, GLM_API_KEY_REF),
  })
}

async function saveSettings(context: InvoiceRpcContext, payload: unknown): Promise<RpcResult> {
  const request = asObject(payload)
  const current = readSettings(context.profileDir)
  const settings = parseSettings(request, current)
  if (!settings) return badRequest('设置字段格式不正确。')
  if (settings.savePath) {
    const validated = context.artifacts.validateSavePath(settings.savePath)
    if (!validated.ok) return badRequest(validated.errorMessage)
    settings.savePath = validated.canonicalPath
  }
  const authCode = stringValue(request.authCode)
  const glmApiKey = stringValue(request.glmApiKey)
  if (authCode) await context.credentials.set(IMAP_AUTH_CODE_REF, authCode)
  if (glmApiKey) await context.credentials.set(GLM_API_KEY_REF, glmApiKey)
  writeSettings(context.profileDir, settings)
  return ok({
    saved: true,
    hasAuthCode: await isCredentialConfigured(context.credentials, IMAP_AUTH_CODE_REF),
    hasGlmApiKey: await isCredentialConfigured(context.credentials, GLM_API_KEY_REF),
  })
}

async function startScan(context: InvoiceRpcContext, payload: unknown): Promise<RpcResult> {
  if (context.scanner.isRunning) return badRequest('已有扫描正在进行。')
  if (!getRuntimeStatus(context.runtime).installed) return badRequest('请先安装本地引擎。')
  const request = asObject(payload)
  const settings = parseSettings(request, readSettings(context.profileDir))
  if (!settings || !settings.email || !settings.company || !settings.savePath) {
    return badRequest('请填写邮箱、公司名称和保存位置。')
  }
  if (!isValidDateRange(settings.dateFrom, settings.dateTo)) {
    return badRequest('请选择有效且按时间顺序排列的日期范围。')
  }
  const server = imapServer(settings.email)
  if (!server) return badRequest('目前仅支持 QQ 邮箱和 163 邮箱。')
  void server
  const output = context.artifacts.validateSavePath(settings.savePath)
  if (!output.ok) return badRequest(output.errorMessage)
  const authCode = stringValue(request.authCode) || await resolveCredential(context.credentials, IMAP_AUTH_CODE_REF)
  const glmApiKey = stringValue(request.glmApiKey) || await resolveCredential(context.credentials, GLM_API_KEY_REF)
  if (!authCode) return badRequest('请填写并保存 IMAP 授权码。')

  const jobId = `scan-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const session = context.sessions.createSession(jobId)
  context.scanner.setProgressHandler(progress => context.sessions.recordProgress(jobId, progress.event))
  const scanRequest: ScanRequest = {
    jobId,
    email: settings.email,
    authCode,
    dateFrom: settings.dateFrom,
    dateTo: settings.dateTo,
    company: settings.company,
    savePath: output.canonicalPath,
    ocrProvider: settings.ocrProvider,
    ...(settings.ocrProvider === 'glm' && glmApiKey ? { glmApiKey } : {}),
  }

  void context.scanner.startScan(scanRequest, event => context.sessions.logModelVisible(session.sessionId, event))
    .then(result => {
      if (result.status === 'cancelled') {
        context.sessions.cancel(jobId)
        return
      }
      if (result.status !== 'completed') {
        context.sessions.fail(jobId)
        return
      }
      let exportArtifactId: string | undefined
      if (result.exportPath) {
        try {
          exportArtifactId = context.artifacts.registerArtifact(jobId, result.exportPath, output.canonicalPath).artifactId
        } catch {
          exportArtifactId = undefined
        }
      }
      context.sessions.complete(jobId, {
        invoicesProcessed: result.invoicesProcessed,
        successCount: result.successCount,
        retainedCount: result.retainedCount,
        manualReviewCount: result.manualReviewCount,
        ...(exportArtifactId ? { exportArtifactId } : {}),
      })
    })
    .catch(() => context.sessions.fail(jobId))

  return ok({ jobId })
}

function getScanStatus(context: InvoiceRpcContext, payload: unknown): Record<string, unknown> {
  const jobId = stringValue(asObject(payload).jobId)
  const projection = jobId ? context.sessions.getProjection(jobId) : undefined
  return {
    running: context.scanner.isRunning,
    ...(jobId ? { jobId } : {}),
    ...(projection ? { status: projection.status, progress: projection.lastProgress ?? projection.status } : {}),
  }
}

function listArtifacts(context: InvoiceRpcContext, payload: unknown): RpcResult {
  const jobId = stringValue(asObject(payload).jobId)
  if (!jobId) return badRequest('缺少扫描任务标识。')
  return ok({ artifacts: context.artifacts.listArtifacts(jobId) })
}

function downloadArtifact(context: InvoiceRpcContext, payload: unknown): RpcResult {
  const request = asObject(payload)
  const artifactId = stringValue(request.artifactId)
  const jobId = stringValue(request.jobId)
  if (!artifactId || !jobId) return badRequest('缺少文件标识或扫描任务标识。')
  const artifact = context.artifacts.readArtifact(artifactId, jobId)
  if (!artifact.ok) return badRequest('文件不可用。')
  return ok({
    fileName: artifact.fileName,
    content: artifact.content.toString('base64'),
  })
}

async function runPython(
  context: InvoiceRpcContext,
  script: string,
  input: string,
  signal: AbortSignal,
): Promise<{ exitCode: number | null; stdout: string }> {
  const handle = context.subprocess.spawn({
    argv: [
      context.runtime.pythonPath,
      '-c',
      script,
      path.join(context.runtime.engineRoot, 'src'),
      path.join(context.runtime.engineRoot, 'src', 'invoice_engine'),
    ],
    cwd: context.runtime.engineRoot,
    stdio: { stdin: 'pipe', stdout: 'pipe', stderr: { maxBytes: 1024 * 1024 } },
    graceMs: context.graceMs,
    signal,
  })
  if (!handle.stdin || !handle.stdout) {
    handle.terminate()
    throw new Error('invoice helper did not provide required standard I/O')
  }
  handle.stdin.write(`${input}\n`)
  let stdout = ''
  for await (const chunk of handle.stdout) stdout += chunk
  return { exitCode: (await handle.done).exitCode, stdout }
}

async function runCommand(
  context: InvoiceRpcContext,
  argv: readonly string[],
  signal: AbortSignal,
): Promise<{ exitCode: number | null; stdout: string }> {
  const handle = context.subprocess.spawn({
    argv,
    cwd: context.runtime.runtimeRoot,
    stdio: { stdin: 'ignore', stdout: 'pipe', stderr: { maxBytes: 1024 * 1024 } },
    graceMs: context.graceMs,
    signal,
  })
  return collectOutput(handle)
}

async function collectOutput(handle: SubprocessHandle): Promise<{ exitCode: number | null; stdout: string }> {
  if (!handle.stdout) {
    handle.terminate()
    throw new Error('runtime command did not provide output')
  }
  let stdout = ''
  for await (const chunk of handle.stdout) stdout += chunk
  return { exitCode: (await handle.done).exitCode, stdout }
}

function parseHealth(stdout: string): RuntimeHealth {
  const last = stdout.trim().split('\n').at(-1)
  if (!last) return { ok: false }
  try {
    const result = JSON.parse(last) as RuntimeHealth
    return { ok: result.ok === true }
  } catch {
    return { ok: false }
  }
}

function parseSettings(value: Record<string, unknown>, fallback: InvoiceSettings): InvoiceSettings | null {
  const next = (key: keyof InvoiceSettings): string => Object.hasOwn(value, key)
    ? stringValue(value[key])
    : fallback[key]
  const ocrProvider = next('ocrProvider') || 'local'
  if (ocrProvider !== 'local' && ocrProvider !== 'glm') return null
  return {
    email: next('email'),
    dateFrom: next('dateFrom'),
    dateTo: next('dateTo'),
    company: next('company'),
    savePath: next('savePath'),
    ocrProvider,
  }
}

function asObject(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function imapServer(email: string): string | null {
  const domain = email.toLowerCase().split('@')[1]
  if (domain === 'qq.com') return 'imap.qq.com'
  if (domain === '163.com') return 'imap.163.com'
  return null
}

function isValidDateRange(first: string, last: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(first) || !/^\d{4}-\d{2}-\d{2}$/.test(last)) return false
  return first <= last
}
