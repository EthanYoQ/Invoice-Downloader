import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'

const scanner = readFileSync(join(import.meta.dirname, '..', 'src', 'invoice-scanner.ts'), 'utf8')
const storage = readFileSync(join(import.meta.dirname, '..', 'src', 'storage.ts'), 'utf8')

test('model extraction uses a prepared DSH stream and records its exact input', () => {
  assert.match(scanner, /const prepared = await this\.llmContext\.llm\.prepareCall\(/)
  assert.match(scanner, /prepared\.stream\(\{ \.\.\.prepared\.config, messages: \[message\] \}\)/)
  assert.match(scanner, /new BlockAssembler\(\)/)
  assert.doesNotMatch(scanner, /\.stream\(\{\}\)/)
  assert.match(storage, /createUserMessage\(/)
  assert.match(storage, /session\?\.append\('user\/message', message, \{ surfaceOp: 'append' \}\)/)
})
