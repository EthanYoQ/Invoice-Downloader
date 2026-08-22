import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'

const source = readFileSync(join(import.meta.dirname, '..', 'src', 'client-sidebar.js'), 'utf8')

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
