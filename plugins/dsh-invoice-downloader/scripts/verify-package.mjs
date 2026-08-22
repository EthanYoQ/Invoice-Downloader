import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
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

const packageManifest = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8'))
assert.ok(packageManifest.files.includes('lib/**/*.js'))
assert.ok(packageManifest.files.includes('lib/**/*.d.ts'))
assert.ok(!packageManifest.files.includes('lib/**'), 'publish only runtime JavaScript and declarations')

const stagedPaths = [
  'runtime/.venv',
  'runtime/engine/.venv',
  '.runtime',
]
for (const path of stagedPaths) {
  assert.ok(!existsSync(join(packageRoot, path)), `runtime residue must not be packaged: ${path}`)
}

console.log('package layout and privacy exclusions: ok')
