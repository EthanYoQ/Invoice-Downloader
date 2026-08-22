import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'
import { runInNewContext } from 'node:vm'

const source = readFileSync(join(import.meta.dirname, '..', 'src', 'client-sidebar.js'), 'utf8')

function loadPluginExports() {
  let definition
  runInNewContext(source, {
    window: {
      __ModuleLoader__: {
        load(value) { definition = value },
      },
    },
  })
  assert.ok(definition)
  return definition.factory(name => {
    if (name === 'react') return { createElement() {} }
    throw new Error(`unexpected client dependency: ${name}`)
  })
}

test('sidebar is explicitly anchored on the right and uses the DSH directory picker', () => {
  assert.match(source, /right: '16px'/)
  assert.match(source, /left: 'auto'/)
  assert.match(source, /width: 'min\(440px, calc\(100vw - 32px\)\)'/)
  assert.match(source, /workspaces\.pickDirectory\(\)/)
  assert.match(source, /const workspaces = ctx\.workspaces/)
  assert.match(source, /'选择保存位置'/)
})

test('sidebar preserves every pending field update during one render batch', () => {
  assert.match(source, /setSettings\(previous => \(\{ \.\.\.previous, \[key\]: value \}\)\)/)
})

test('sidebar handles text and date input events from the DSH web host', () => {
  assert.match(source, /type: 'date', disabled, value: settings\.dateFrom, onInput: event => update\('dateFrom', event\.target\.value\)/)
  assert.match(source, /type: 'date', disabled, value: settings\.dateTo, onInput: event => update\('dateTo', event\.target\.value\)/)
})

test('sidebar replaces the last progress event with the terminal scan status', () => {
  assert.match(source, /progress: next\.running \? \(next\.progress \|\| previous\.progress\) : \(next\.status \|\| next\.progress \|\| previous\.progress\)/)
})

test('directory picker applies a selected DSH path and reports success', async () => {
  const { chooseOutputDirectory } = loadPluginExports()
  const updates = []
  const statuses = []
  await chooseOutputDirectory(
    { async pickDirectory() { return 'C:\\invoice-output' } },
    (...args) => updates.push(args),
    value => statuses.push([value.kind, value.message]),
  )
  assert.deepEqual(updates, [['savePath', 'C:\\invoice-output']])
  assert.deepEqual(statuses, [
    ['idle', ''],
    ['success', '已选择保存位置；保存设置后会重新校验。'],
  ])
})

test('directory picker leaves settings unchanged when the native chooser is cancelled', async () => {
  const { chooseOutputDirectory } = loadPluginExports()
  const updates = []
  const statuses = []
  await chooseOutputDirectory(
    { async pickDirectory() { return undefined } },
    (...args) => updates.push(args),
    value => statuses.push([value.kind, value.message]),
  )
  assert.deepEqual(updates, [])
  assert.deepEqual(statuses, [['idle', '']])
})

test('directory picker reports a Host error without changing settings', async () => {
  const { chooseOutputDirectory } = loadPluginExports()
  const updates = []
  const statuses = []
  await chooseOutputDirectory(
    { async pickDirectory() { throw new Error('native dialog unavailable') } },
    (...args) => updates.push(args),
    value => statuses.push([value.kind, value.message]),
  )
  assert.deepEqual(updates, [])
  assert.deepEqual(statuses, [
    ['idle', ''],
    ['error', '无法打开目录选择器。'],
  ])
})
