import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'

const scanner = readFileSync(join(import.meta.dirname, '..', 'src', 'invoice-scanner.ts'), 'utf8')
const storage = readFileSync(join(import.meta.dirname, '..', 'src', 'storage.ts'), 'utf8')

test('model extraction uses a prepared DSH stream and records its exact input', () => {
  assert.match(scanner, /const prepared = await this\.llmContext\.llm\.prepareCall\(/)
  assert.match(scanner, /prepared\.stream\(\{ \.\.\.prepared\.config, messages: \[message\] \}\)/)
  assert.match(scanner, /new TextResponseAssembler\(\)/)
  assert.doesNotMatch(scanner, /\.stream\(\{\}\)/)
  assert.match(storage, /createInvoiceUserMessage\(/)
  assert.match(storage, /session\?\.append\('user\/message', message, \{ surfaceOp: 'append' \}\)/)
})

test('model-visible scan text uses the DSH user-message wire fields without a private runtime import', async () => {
  const { ScanSessionManager } = await import('../lib/storage.js')
  const appended = []
  const manager = new ScanSessionManager()
  manager.setContext({
    sessions: {
      create(id) {
        return {
          id,
          append(type, message, options) { appended.push({ type, message, options }) },
        }
      },
    },
  })
  const created = manager.createSession('fixture-job')
  const message = manager.logModelVisible(created.sessionId, { inputText: 'fixture OCR text' })
  assert.equal(message.role, 'user')
  assert.equal(message.source.kind, 'plugin')
  assert.equal(message.source.plugin, 'invoice-downloader')
  assert.deepEqual(message.content, [{ type: 'text', text: 'fixture OCR text' }])
  assert.equal(appended.length, 1)
  assert.equal(appended[0].type, 'user/message')
  assert.deepEqual(appended[0].options, { surfaceOp: 'append' })
})

test('model extraction collects streamed text without importing a private DSH runtime package', async () => {
  const { InvoiceScanner } = await import('../lib/invoice-scanner.js')
  const scanner = new InvoiceScanner({ spawn() { throw new Error('not used') } }, {
    pythonPath: 'fixture-python',
    engineRoot: 'fixture-engine',
    adapterRoot: 'fixture-adapter',
    deadlineMs: 1_000,
    graceMs: 1,
    imapFetchTimeoutSeconds: 1,
  })
  scanner.setLlmContext({
    agentDefaultModel: { currentSelection() { return { provider: 'fixture', model: 'fixture-model' } } },
    llm: {
      async prepareCall() {
        return {
          config: {},
          async *stream() {
            yield { type: 'text-delta', index: 0, text: '{"seller":"Fixture"}' }
            yield { type: 'finish', reason: { kind: 'stop' } }
          },
        }
      },
    },
  })
  const response = await scanner.extractWithModel(
    { requestId: 'fixture-request', ocrText: 'fixture OCR' },
    () => ({ id: 'fixture-message', role: 'user', content: [], source: { kind: 'plugin', plugin: 'invoice-downloader' } }),
  )
  assert.deepEqual(response, {
    requestId: 'fixture-request',
    extractedFields: { seller: 'Fixture' },
    rawModelOutput: '{"seller":"Fixture"}',
  })
})
