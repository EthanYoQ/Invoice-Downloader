import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

export interface RuntimeConfig {
  runtimeRoot?: string
  engineRoot?: string
  pythonPath?: string
  bootstrapPython?: string
}

export interface RuntimeLayout {
  runtimeRoot: string
  engineRoot: string
  pythonPath: string
  bootstrapPython?: string
  venvRoot: string
  adapterRoot: string
  installerPath: string
  healthCheckPath: string
  requirementsPath: string
  bootstrapArgv: readonly string[]
}

export interface RuntimeStatus {
  installed: boolean
  enginePresent: boolean
  platform: 'windows-x64' | 'macos-arm64' | 'unsupported'
}

function packageRoot(): string {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
}

function defaultBootstrapArgv(): readonly string[] {
  if (process.platform === 'win32') return ['py', '-3']
  return ['python3']
}

export function resolveRuntimeLayout(config: RuntimeConfig): RuntimeLayout {
  const root = packageRoot()
  const packageRuntimeRoot = path.join(root, 'runtime')
  const runtimeRoot = config.runtimeRoot?.trim()
    || path.join(process.env.DSH_HOME || path.join(process.env.USERPROFILE || process.env.HOME || '.', '.dsh'), 'invoice-downloader', 'runtime')
  const venvRoot = path.join(runtimeRoot, '.venv')
  const venvPython = process.platform === 'win32'
    ? path.join(venvRoot, 'Scripts', 'python.exe')
    : path.join(venvRoot, 'bin', 'python')
  const bootstrapPython = config.bootstrapPython?.trim()

  return {
    runtimeRoot,
    engineRoot: config.engineRoot?.trim() || path.join(packageRuntimeRoot, 'engine'),
    pythonPath: config.pythonPath?.trim() || venvPython,
    ...(bootstrapPython ? { bootstrapPython } : {}),
    venvRoot,
    adapterRoot: path.join(root, 'engine-adapter'),
    installerPath: path.join(packageRuntimeRoot, 'install.py'),
    healthCheckPath: path.join(packageRuntimeRoot, 'health_check.py'),
    requirementsPath: path.join(packageRuntimeRoot, 'requirements-headless.txt'),
    bootstrapArgv: bootstrapPython ? [bootstrapPython] : defaultBootstrapArgv(),
  }
}

export function getRuntimeStatus(layout: RuntimeLayout): RuntimeStatus {
  const enginePresent = fs.existsSync(path.join(layout.engineRoot, 'src', 'invoice_engine', 'app_api.py'))
  const installed = enginePresent && fs.existsSync(layout.pythonPath)
  const platform = process.platform === 'win32' && process.arch === 'x64'
    ? 'windows-x64'
    : process.platform === 'darwin' && process.arch === 'arm64'
      ? 'macos-arm64'
      : 'unsupported'
  return { installed, enginePresent, platform }
}
