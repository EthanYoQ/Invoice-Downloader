import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const required = [
  'lib/index.js',
  'lib/activate.js',
  'src/client-sidebar.js',
  'cordis.patch.yml',
  'runtime/install.py',
  'runtime/health_check.py',
  'runtime/requirements-headless.txt',
  'runtime/engine/MANIFEST.json',
  'runtime/engine/src/invoice_engine/app_api.py',
  'engine-adapter/dsh_runner.py',
  'engine-adapter/dsh_scan.py',
  'runtime/engine/src/invoice_engine/ipc/protocol.py',
  'LICENSE',
  'NOTICE',
]

for (const file of required) {
  assert.ok(existsSync(join(packageRoot, file)), `missing package file: ${file}`)
}

const manifest = JSON.parse(readFileSync(join(packageRoot, 'runtime/engine/MANIFEST.json'), 'utf8'))
assert.equal(typeof manifest.sourceSha, 'string')
assert.match(manifest.sourceSha, /^[0-9a-f]{40}$/)
assert.equal(manifest.sourceRepository, 'https://github.com/EthanYoQ/Invoice-Downloader')
assert.ok(Array.isArray(manifest.sourceFiles))

function collectPythonFiles(root, prefix = '') {
  return readdirSync(root, { withFileTypes: true }).flatMap(entry => {
    const path = join(root, entry.name)
    const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name
    if (entry.isDirectory()) return collectPythonFiles(path, relativePath)
    return entry.isFile() && entry.name.endsWith('.py') ? [relativePath] : []
  })
}

function collectJavaScriptFiles(root, prefix = '') {
  return readdirSync(root, { withFileTypes: true }).flatMap(entry => {
    const path = join(root, entry.name)
    const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name
    if (entry.isDirectory()) return collectJavaScriptFiles(path, relativePath)
    return entry.isFile() && entry.name.endsWith('.js') ? [relativePath] : []
  })
}

const bundledPythonFiles = collectPythonFiles(join(packageRoot, 'runtime', 'engine', 'src', 'invoice_engine'))
  .filter(file => file !== '__init__.py')
  .sort()
assert.deepEqual(bundledPythonFiles, manifest.sourceFiles)

const packageManifest = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8'))
assert.ok(packageManifest.files.includes('lib/**/*.js'))
assert.ok(packageManifest.files.includes('lib/**/*.d.ts'))
assert.ok(!packageManifest.files.includes('lib/**'), 'publish only runtime JavaScript and declarations')
assert.equal(packageManifest.os, undefined)
assert.equal(packageManifest.cpu, undefined)

const directDshImports = collectJavaScriptFiles(join(packageRoot, 'lib'))
  .filter(file => /(?:from|import\()\s*['"]@deepseek-ai\//.test(readFileSync(join(packageRoot, 'lib', file), 'utf8')))
assert.deepEqual(directDshImports, [])

const stagedPaths = [
  'runtime/.venv',
  'runtime/engine/.venv',
  '.runtime',
]
for (const path of stagedPaths) {
  assert.ok(!existsSync(join(packageRoot, path)), `runtime residue must not be packaged: ${path}`)
}

console.log('package layout and privacy exclusions: ok')
