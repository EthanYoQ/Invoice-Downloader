import { existsSync, rmSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const targets = [
  resolve(packageRoot, 'lib'),
  resolve(packageRoot, 'runtime', 'engine'),
]

for (const target of targets) {
  if (!target.startsWith(`${packageRoot}\\`) && !target.startsWith(`${packageRoot}/`)) {
    throw new Error(`refusing to clean outside package: ${target}`)
  }
  if (existsSync(target)) rmSync(target, { recursive: true, force: true })
}
