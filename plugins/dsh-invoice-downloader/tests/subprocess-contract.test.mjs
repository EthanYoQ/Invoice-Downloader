import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { PassThrough, Readable } from 'node:stream'
import test from 'node:test'
import { ArtifactRegistry } from '../lib/file-security.js'
import { EngineIpcClient } from '../lib/ipc-client.js'
import { createInvoiceRpcHandler } from '../lib/rpc-handler.js'
import { resolveRuntimeLayout } from '../lib/runtime.js'

function handle(stdoutText, exitCode = 0) {
  return {
    pid: 1,
    stdin: new PassThrough(),
    stdout: Readable.from([stdoutText]),
    stderr: Readable.from([]),
    done: Promise.resolve({ exitCode, signal: null }),
    terminate() {},
  }
}

function temporaryRuntime() {
  const root = mkdtempSync(join(tmpdir(), 'invoice-dsh-test-'))
  const engineRoot = join(root, 'engine')
  const pythonPath = join(root, process.platform === 'win32' ? 'python.exe' : 'python')
  mkdirSync(join(engineRoot, 'src', 'invoice_engine'), { recursive: true })
  writeFileSync(join(engineRoot, 'src', 'invoice_engine', 'app_api.py'), '')
  writeFileSync(pythonPath, '')
  return {
    root,
    engineRoot,
    pythonPath,
    runtime: {
      runtimeRoot: root,
      engineRoot,
      pythonPath,
      bootstrapPython: undefined,
      venvRoot: join(root, '.venv'),
      adapterRoot: join(root, 'adapter'),
      installerPath: join(root, 'install.py'),
      healthCheckPath: join(root, 'health.py'),
      requirementsPath: join(root, 'requirements.txt'),
      bootstrapArgv: ['python'],
    },
  }
}

test('engine subprocess always includes the DSH grace period', async () => {
  const calls = []
  const client = new EngineIpcClient(
    {
      spawn(spec) {
        calls.push(spec)
        return handle('{"type":"job.result","result":{"status":"completed","invoicesProcessed":1}}\n')
      },
    },
    'C:/runtime/python.exe',
    'C:/runtime/engine',
    'C:/runtime/adapter',
    2_000,
  )

  const result = await client.startJob({
    jobId: 'fixture-job',
    email: 'fixture@example.test',
    authCode: 'fixture-only',
    dateFrom: '2026-01-01',
    dateTo: '2026-01-01',
    company: 'Fixture',
    savePath: 'C:/fixture-output',
    ocrProvider: 'local',
    imapFetchTimeoutSeconds: 15,
  })

  assert.deepEqual(result, { status: 'completed', invoicesProcessed: 1 })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].graceMs, 2_000)
  assert.deepEqual(calls[0].stdio, {
    stdin: 'pipe',
    stdout: 'pipe',
    stderr: { maxBytes: 1024 * 1024 },
  })
  assert.deepEqual(calls[0].argv.slice(0, 3), [
    'C:/runtime/python.exe',
    '-c',
    'import runpy, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]); sys.path.insert(0, sys.argv[3]); runpy.run_module(sys.argv[4], run_name="__main__")',
  ])
})

test('health checks use the same explicit subprocess contract', async () => {
  const { root, runtime } = temporaryRuntime()
  const calls = []
  const handler = createInvoiceRpcHandler({
    savePath: join(root, 'output'),
    profileDir: join(root, 'profile'),
    graceMs: 2_000,
    runtime,
    subprocess: {
      spawn(spec) {
        calls.push(spec)
        return handle('{"ok":true,"checks":[]}\n')
      },
    },
    scanner: { isRunning: false, stopScan() {} },
    sessions: {},
    credentials: {},
    artifacts: new ArtifactRegistry(),
  })

  const result = await handler('testConnection', {}, new AbortController().signal)
  assert.deepEqual(result, {
    ok: true,
    value: { engineVersion: '0.1.0', ocrAvailable: true },
  })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].graceMs, 2_000)
  assert.deepEqual(calls[0].stdio, {
    stdin: 'ignore',
    stdout: 'pipe',
    stderr: { maxBytes: 1024 * 1024 },
  })
})

test('runtime installation selects the newest supported Windows Python Launcher entry', async () => {
  const { root, runtime } = temporaryRuntime()
  const calls = []
  const replies = [
    ' -V:3.14 * C:\\Python314\\python.exe\n -V:3.12 C:\\Python312\\python.exe\n -V:3.13 C:\\Python313\\python.exe\n',
    'installed\n',
    '{"ok":true,"checks":[]}\n',
  ]
  const handler = createInvoiceRpcHandler({
    savePath: join(root, 'output'),
    profileDir: join(root, 'profile'),
    graceMs: 2_000,
    runtime,
    subprocess: {
      spawn(spec) {
        calls.push(spec)
        return handle(replies.shift() || '')
      },
    },
    scanner: { isRunning: false, stopScan() {} },
    sessions: {},
    credentials: {},
    artifacts: new ArtifactRegistry(),
  })

  const result = await handler('installRuntime', {}, new AbortController().signal)
  assert.equal(result.ok, true)
  if (process.platform === 'win32') {
    assert.deepEqual(calls[0].argv, ['py', '-0p'])
    assert.deepEqual(calls[1].argv.slice(0, 2), ['py', '-3.13'])
  }
})

test('runtime state stays outside the package while engine assets remain package-owned', () => {
  const layout = resolveRuntimeLayout({ runtimeRoot: 'C:/invoice-dsh-state' })
  assert.equal(layout.runtimeRoot, 'C:/invoice-dsh-state')
  assert.match(layout.venvRoot, /invoice-dsh-state[\\/]\.venv$/)
  assert.match(layout.engineRoot, /runtime[\\/]engine$/)
  assert.doesNotMatch(layout.engineRoot, /invoice-dsh-state/)
  assert.doesNotMatch(layout.installerPath, /invoice-dsh-state/)
})

test('the default runtime state resolves under the active DSH profile home', () => {
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = 'C:/dsh-profile-home'
  try {
    const layout = resolveRuntimeLayout({})
    assert.match(layout.runtimeRoot, /dsh-profile-home[\\/]invoice-downloader[\\/]runtime$/)
    assert.match(layout.venvRoot, /dsh-profile-home[\\/]invoice-downloader[\\/]runtime[\\/]\.venv$/)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
  }
})

test('runtime installation repairs a virtual environment that lacks pip', () => {
  const installer = readFileSync(join(import.meta.dirname, '..', 'runtime', 'install.py'), 'utf8')
  assert.match(installer, /if not pip\.is_file\(\):/)
  assert.match(installer, /ensurepip', '--upgrade'/)
})

test('settings persist a selected output directory without persisting the credential value', async () => {
  const { root, runtime } = temporaryRuntime()
  const output = join(root, 'output')
  const stored = []
  const handler = createInvoiceRpcHandler({
    savePath: output,
    profileDir: join(root, 'profile'),
    graceMs: 2_000,
    runtime,
    subprocess: { spawn() { throw new Error('not used') } },
    scanner: { isRunning: false, stopScan() {} },
    sessions: {},
    credentials: {
      async set(ref, value) { stored.push({ ref, value }) },
      async describe() { return { configured: true, writable: true } },
    },
    artifacts: new ArtifactRegistry(),
  })

  const saved = await handler('saveSettings', {
    email: 'fixture@example.test',
    authCode: 'fixture-only',
    dateFrom: '2026-01-01',
    dateTo: '2026-01-01',
    company: 'Fixture',
    savePath: output,
    ocrProvider: 'local',
  }, new AbortController().signal)

  assert.deepEqual(saved, {
    ok: true,
    value: { saved: true, hasAuthCode: true, hasGlmApiKey: true },
  })
  assert.equal(stored.length, 1)
  const settingsText = readFileSync(join(root, 'profile', 'settings.json'), 'utf8')
  assert.match(settingsText, /fixture@example\.test/)
  assert.doesNotMatch(settingsText, /fixture-only/)
  const reloaded = await handler('getSettings', {}, new AbortController().signal)
  assert.equal(reloaded.ok, true)
  assert.equal(reloaded.value.savePath, output)
  assert.equal(reloaded.value.hasAuthCode, true)
  assert.equal(reloaded.value.authCode, undefined)
})

test('settings reject a relative output directory before writing anything', async () => {
  const { root, runtime } = temporaryRuntime()
  const profileDir = join(root, 'profile')
  const handler = createInvoiceRpcHandler({
    savePath: '',
    profileDir,
    graceMs: 2_000,
    runtime,
    subprocess: { spawn() { throw new Error('not used') } },
    scanner: { isRunning: false, stopScan() {} },
    sessions: {},
    credentials: {},
    artifacts: new ArtifactRegistry(),
  })

  const saved = await handler('saveSettings', {
    email: 'fixture@example.test',
    savePath: 'relative-output',
  }, new AbortController().signal)

  assert.deepEqual(saved, {
    ok: false,
    error: {
      code: 'bad-request',
      message: '保存路径必须是绝对路径。',
      details: { issues: [] },
    },
  })
  assert.equal(existsSync(join(profileDir, 'settings.json')), false)
})

test('artifact access is bound to the selected output root', () => {
  const root = mkdtempSync(join(tmpdir(), 'invoice-dsh-artifact-'))
  const output = join(root, 'output')
  const outside = join(root, 'outside')
  mkdirSync(output)
  mkdirSync(outside)
  const exportPath = join(output, 'summary.xlsx')
  const outsidePath = join(outside, 'summary.xlsx')
  writeFileSync(exportPath, 'fixture')
  writeFileSync(outsidePath, 'fixture')
  const artifacts = new ArtifactRegistry()
  const artifact = artifacts.registerArtifact('fixture-job', exportPath, output)
  assert.equal(artifacts.readArtifact(artifact.artifactId, 'fixture-job').ok, true)
  assert.equal(artifacts.readArtifact(artifact.artifactId, 'another-job').ok, false)
  assert.throws(() => artifacts.registerArtifact('fixture-job', outsidePath, output), /authorized output directory/)
})
