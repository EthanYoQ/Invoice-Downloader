import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import type { CredentialProvider } from '@deepseek-ai/dsh-credentials'
import type { LlmRuntime } from '@deepseek-ai/dsh-llm'
import type { SessionStore } from '@deepseek-ai/dsh-session'
import type { SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import { ArtifactRegistry } from './file-security.js'
import { InvoiceScanner, type LlmContext } from './invoice-scanner.js'
import { createInvoiceRpcHandler } from './rpc-handler.js'
import { resolveRuntimeLayout, type RuntimeConfig } from './runtime.js'
import { ScanSessionManager } from './storage.js'

export interface PluginContext {
  effect(callback: () => void | (() => void), description?: string): void
  subprocess: SubprocessRuntime
  connection: {
    rpc: {
      handle(
        channel: string,
        handler: (endpoint: string, payload: unknown, signal: AbortSignal) => Promise<unknown>,
        options: { authority: 'loopback' },
      ): void
    }
  }
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
export const inject = ['subprocess', 'connection', 'sessions', 'llm', 'agentDefaultModel', 'credentials']

export function apply(context: PluginContext, config: PluginConfig): void {
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
  context.effect(
    () => context.connection.rpc.handle('/invoice', handler, { authority: 'loopback' }),
    'invoice-downloader: loopback RPC channel',
  )
}
