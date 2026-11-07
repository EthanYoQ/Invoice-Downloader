import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const log = await readFile(process.argv[2], 'utf8')
const launchUrl = /^dsh web: (http:\/\/127\.0\.0\.1:\d+\/\?token=[A-Za-z0-9_-]+)\r?$/m.exec(log)?.[1]
assert.ok(launchUrl, 'DSH did not announce its loopback Web URL')

const origin = new URL('/', launchUrl)
const exchange = await fetch(launchUrl, { redirect: 'manual', signal: AbortSignal.timeout(10_000) })
assert.equal(exchange.status, 303, 'DSH did not accept the launch token')
const cookie = exchange.headers.getSetCookie()[0]?.split(';', 1)[0]
assert.ok(cookie, 'DSH did not issue a browser cookie')

const page = await fetch(origin, { headers: { cookie }, signal: AbortSignal.timeout(10_000) })
assert.equal(page.status, 200)
const html = await page.text()
const pluginClient = '@ethanyoq/dsh-invoice-downloader/client.js'
const combo = Array.from(html.matchAll(/\bhref="(plugins\/\?\?[^"]+)"/g), match => match[1])
  .find(href => href.includes(pluginClient))
assert.ok(combo, 'The DSH page did not include the invoice client')
const resource = await fetch(new URL(combo.replaceAll('&amp;', '&'), origin), {
  headers: { cookie },
  signal: AbortSignal.timeout(10_000),
})
assert.equal(resource.status, 200)
assert.ok((await resource.text()).includes('invoice-downloader-client'), 'DSH did not serve the invoice client factory')

async function call(method, payload, authCookie = cookie) {
  return fetch(new URL(`api/${method}`, origin), {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...(authCookie ? { cookie: authCookie } : {}) },
    body: JSON.stringify({ type: 'client-request', rpcId: 'invoice-smoke', method, payload }),
    signal: AbortSignal.timeout(20_000),
  })
}

async function result(response) {
  assert.equal(response.status, 200)
  const message = await response.json()
  assert.equal(message.type, 'server-response')
  assert.equal(message.rpcId, 'invoice-smoke')
  assert.equal(message.result.ok, true, JSON.stringify(message.result.error))
  return message.result.value
}

const runtimeStatus = { endpoint: 'getRuntimeStatus', payload: {} }
const anonymous = await call('invoice/rpc', runtimeStatus, '')
assert.equal(anonymous.status, 401, 'Invoice RPC must require DSH authentication')
await result(await call('settings/describe', { args: {} }))
const status = await result(await call('invoice/rpc', runtimeStatus))
assert.equal(status.enginePresent, true)
if (process.argv.includes('--with-runtime')) {
  assert.equal(status.installed, true)
  const health = await result(await call('invoice/rpc', { endpoint: 'testConnection', payload: {} }))
  assert.equal(health.ocrAvailable, true)
}

console.log('DSH served the invoice client, authenticated invoice RPC, and kept the built-in settings route active.')
