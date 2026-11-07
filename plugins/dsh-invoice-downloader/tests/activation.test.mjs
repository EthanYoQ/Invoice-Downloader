import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { apply } from '../lib/index.js'

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'invoice-activation-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const config = { profileDir: join(root, 'profile'), runtimeRoot: join(root, 'runtime') }
  const routes = new Map()
  const context = {
    webServer: { host: '127.0.0.1' },
    connection: {
      allowsRemoteAuthorities: false,
      fetch: { register: route => routes.set(route.path, route) },
    },
    subprocess: {},
    sessions: {},
    llm: {},
    agentDefaultModel: {},
    credentials: {},
  }
  return { config, routes, context }
}

test('the invoice route handles its own RPC envelope and rejects malformed calls', async t => {
  const { config, routes, context } = fixture(t)
  apply(context, config)

  const route = routes.get('/api/invoice/rpc')
  assert.equal(routes.size, 1)
  assert.deepEqual(route.methods, ['POST'])
  const envelope = {
    type: 'client-request',
    rpcId: 'test-invoice',
    method: 'invoice/rpc',
    payload: { endpoint: 'getRuntimeStatus', payload: {} },
  }
  const request = (body, contentType = 'application/json') => new Request(`http://127.0.0.1${route.path}`, {
    method: 'POST',
    headers: { 'content-type': contentType },
    body,
  })

  const response = await route.fetch(request(JSON.stringify(envelope)))
  assert.equal(response.status, 200)
  const message = await response.json()
  assert.equal(message.type, 'server-response')
  assert.equal(message.rpcId, envelope.rpcId)
  assert.equal(message.result.ok, true)
  assert.equal(message.result.value.installed, false)

  assert.equal((await route.fetch(request('{}', 'text/plain'))).status, 415)
  assert.equal((await route.fetch(request('{'))).status, 400)
  assert.equal((await route.fetch(request(JSON.stringify({ ...envelope, method: 'settings/describe' })))).status, 400)
  assert.equal((await route.fetch(request(JSON.stringify({ ...envelope, payload: {} })))).status, 400)
})

test('a remote listener or trusted authority blocks activation before any invoice state is created', t => {
  const { config, routes, context } = fixture(t)
  assert.throws(() => apply({ ...context, webServer: { host: '192.0.2.1' } }, config), /loopback-only/)
  assert.throws(
    () => apply({ ...context, connection: { ...context.connection, allowsRemoteAuthorities: true } }, config),
    /loopback-only/,
  )
  assert.equal(routes.size, 0)
  assert.equal(existsSync(config.profileDir), false)
  assert.equal(existsSync(config.runtimeRoot), false)
})
