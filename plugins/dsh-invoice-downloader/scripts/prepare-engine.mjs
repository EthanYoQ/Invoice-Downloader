import { cpSync, existsSync, lstatSync, mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const projectRoot = resolve(packageRoot, '..', '..')
const runtimeRoot = resolve(packageRoot, 'runtime')
const engineRoot = resolve(runtimeRoot, 'engine')
const sourceRoot = projectRoot
const extensionRoot = resolve(packageRoot, 'engine-adapter', 'engine-src')

function requireInside(parent, child) {
  const rel = relative(parent, child)
  if (rel === '' || rel === '..' || rel.startsWith(`..${sep}`) || rel.includes(`${sep}..${sep}`)) {
    throw new Error(`path must be inside ${parent}: ${child}`)
  }
}

function copyFile(source, destination) {
  const stat = lstatSync(source)
  if (!stat.isFile()) throw new Error(`expected file: ${source}`)
  mkdirSync(dirname(destination), { recursive: true })
  cpSync(source, destination, { force: true })
}

requireInside(packageRoot, runtimeRoot)
requireInside(runtimeRoot, engineRoot)
if (!existsSync(join(sourceRoot, 'app_api.py'))) {
  throw new Error(`Invoice Downloader source is unavailable at ${sourceRoot}`)
}
if (!existsSync(extensionRoot)) {
  throw new Error(`DSH adapter sources are unavailable at ${extensionRoot}`)
}

rmSync(engineRoot, { recursive: true, force: true })
mkdirSync(join(engineRoot, 'src', 'invoice_engine'), { recursive: true })

for (const entry of readdirSync(sourceRoot, { withFileTypes: true })) {
  if (entry.isFile() && entry.name.endsWith('.py')) {
    copyFile(join(sourceRoot, entry.name), join(engineRoot, 'src', 'invoice_engine', entry.name))
  }
}

for (const entry of readdirSync(extensionRoot, { withFileTypes: true })) {
  if (entry.isFile() && entry.name.endsWith('.py')) {
    copyFile(join(extensionRoot, entry.name), join(engineRoot, 'src', 'invoice_engine', entry.name))
  }
}

copyFile(
  join(packageRoot, 'engine-adapter', 'ipc', 'protocol.py'),
  join(engineRoot, 'src', 'invoice_engine', 'ipc', 'protocol.py'),
)
writeFileSync(join(engineRoot, 'src', 'invoice_engine', '__init__.py'), '"""Bundled Invoice Downloader engine source."""\n')

const sourceSha = execFileSync('git', ['-C', sourceRoot, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim()
writeFileSync(
  join(engineRoot, 'MANIFEST.json'),
  `${JSON.stringify({
    sourceRepository: 'https://github.com/EthanYoQ/Invoice-Downloader',
    sourceSha,
    generatedBy: '@ethanyoq/dsh-invoice-downloader',
    generatedAt: new Date().toISOString(),
  }, null, 2)}\n`,
)
