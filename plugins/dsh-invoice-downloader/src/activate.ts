import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { clientRequestSchema, type HostConnectionHandle } from '@deepseek-ai/dsh-client-connection'
import type { CredentialProvider } from '@deepseek-ai/dsh-credentials'
import { isLoopbackHost, type WebServer } from '@deepseek-ai/dsh-host-webserver'
import type { LlmRuntime } from '@deepseek-ai/dsh-llm'
import type { SessionStore } from '@deepseek-ai/dsh-session'
import type { SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import { ArtifactRegistry } from './file-security.js'
import { InvoiceScanner, type LlmContext } from './invoice-scanner.js'
import { createInvoiceRpcHandler } from './rpc-handler.js'
import { resolveRuntimeLayout, type RuntimeConfig } from './runtime.js'
import { ScanSessionManager } from './storage.js'

export interface PluginContext {
  subprocess: SubprocessRuntime
  connection: HostConnectionHandle
  webServer: Pick<WebServer, 'host'>
  sessions: SessionStore
  llm: Pick<LlmRuntime, 'prepareCall'>
  agentDefaultModel: LlmContext['agentDefaultModel']
  credentials: CredentialProvider
}

export interface PluginConfig extends RuntimeConfig {
  savePath?: string
  deadlineMs?: number
  graceMs?: number
  imapFetchTimeoutSeconds?: number
  profileDir?: string
}

export const name = 'invoice-downloader'
export const inject = ['subprocess', 'connection', 'webServer', 'sessions', 'llm', 'agentDefaultModel', 'credentials']

export function apply(context: PluginContext, config: PluginConfig): void {
  const connection = context.connection
  if (!isLoopbackHost(context.webServer.host) || connection.allowsRemoteAuthorities !== false) {
    throw new Error('Invoice Downloader requires a loopback-only DSH listener with no remote trusted authorities.')
  }

  const runtime = resolveRuntimeLayout(config)
  const profileDir = config.profileDir?.trim()
    || path.join(process.env.DSH_HOME || path.join(os.homedir(), '.dsh'), 'invoice-downloader')
  const savePath = config.savePath?.trim() || ''
  fs.mkdirSync(profileDir, { recursive: true })
  fs.mkdirSync(runtime.runtimeRoot, { recursive: true })

  const scanner = new InvoiceScanner(context.subprocess, {
    pythonPath: runtime.pythonPath,
    engineRoot: runtime.engineRoot,
    adapterRoot: runtime.adapterRoot,
    deadlineMs: config.deadlineMs ?? 600_000,
    graceMs: config.graceMs ?? 2_000,
    imapFetchTimeoutSeconds: config.imapFetchTimeoutSeconds ?? 15,
  })
  scanner.setLlmContext({
    llm: context.llm,
    agentDefaultModel: context.agentDefaultModel,
  })

  const sessions = new ScanSessionManager()
  sessions.setContext({ sessions: context.sessions })
  const handler = createInvoiceRpcHandler({
    savePath,
    profileDir,
    graceMs: config.graceMs ?? 2_000,
    subprocess: context.subprocess,
    runtime,
    scanner,
    sessions,
    credentials: context.credentials,
    artifacts: new ArtifactRegistry(),
  })
  const method = 'invoice/rpc'
  connection.fetch.register({
    path: `/api/${method}`,
    methods: ['POST'],
    requestBody: 'buffered',
    async fetch(request) {
      const contentType = request.headers.get('content-type')?.split(';', 1)[0].trim().toLowerCase()
      if (contentType !== 'application/json') return new Response(null, { status: 415 })

      const envelope = clientRequestSchema.safeParse(await request.json().catch(() => undefined))
      if (!envelope.success || envelope.data.method !== method) return new Response(null, { status: 400 })

      const { rpcId, payload } = envelope.data
      if (!payload || typeof payload !== 'object' || !('endpoint' in payload)
          || typeof payload.endpoint !== 'string' || !('payload' in payload)) {
        return new Response(null, { status: 400 })
      }
      const result = await handler(payload.endpoint, payload.payload, request.signal)
      return Response.json({ type: 'server-response', rpcId, result })
    },
  })
}
