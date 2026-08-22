import type { Message, UserMessage } from '@deepseek-ai/dsh-llm'
import type { Session, SessionStore } from '@deepseek-ai/dsh-session'

export interface ModelVisibleEvent {
  inputText: string
}

export interface RunProjection {
  jobId: string
  sessionId: string
  status: 'active' | 'completed' | 'failed' | 'cancelled'
  startedAt: string
  completedAt?: string
  invoicesProcessed: number
  successCount: number
  retainedCount: number
  manualReviewCount: number
  exportArtifactId?: string
  lastProgress?: string
}

export interface ScanSessionManagerContext {
  sessions: SessionStore
}

function createInvoiceUserMessage(inputText: string): UserMessage {
  const content = Object.freeze([Object.freeze({ type: 'text', text: inputText })])
  return Object.freeze({
    id: crypto.randomUUID(),
    role: 'user',
    content,
    source: { kind: 'plugin', plugin: 'invoice-downloader' },
  }) as unknown as UserMessage
}

export const PRIVACY_DISCLOSURE = {
  defaultChain: '发票文件在本地 OCR；OCR 文本会发送到当前选择的模型以提取字段，并记录在 DSH 会话中。',
  glmChain: '启用 GLM 时，发票图像会发送到 GLM 服务。',
  credentials: '邮箱授权码和 GLM API Key 由 DSH 凭据服务管理，不会写入插件设置。',
  localOnly: '此插件只支持运行在本机的 DSH。',
} as const

export class ScanSessionManager {
  private context: ScanSessionManagerContext | null = null
  private readonly sessions = new Map<string, Session>()
  private readonly projections = new Map<string, RunProjection>()

  setContext(context: ScanSessionManagerContext): void {
    this.context = context
  }

  createSession(jobId: string): { sessionId: string; jobId: string } {
    if (!this.context) throw new Error('DSH session service is unavailable')
    const sessionId = `invoice-scan-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    const session = this.context.sessions.create(sessionId as Parameters<SessionStore['create']>[0])
    if (session.id !== sessionId) throw new Error('DSH session service returned a different session id')
    this.sessions.set(sessionId, session)
    this.projections.set(jobId, {
      jobId,
      sessionId,
      status: 'active',
      startedAt: new Date().toISOString(),
      invoicesProcessed: 0,
      successCount: 0,
      retainedCount: 0,
      manualReviewCount: 0,
    })
    return { sessionId, jobId }
  }

  logModelVisible(sessionId: string, event: ModelVisibleEvent): Message {
    const message = createInvoiceUserMessage(event.inputText)
    const session = this.sessions.get(sessionId)
    session?.append('user/message', message, { surfaceOp: 'append' })
    return message
  }

  recordProgress(jobId: string, event: string): void {
    const projection = this.projections.get(jobId)
    if (projection) projection.lastProgress = event
  }

  complete(jobId: string, result: {
    invoicesProcessed: number
    successCount: number
    retainedCount: number
    manualReviewCount: number
    exportArtifactId?: string
  }): void {
    const projection = this.projections.get(jobId)
    if (!projection) return
    projection.status = 'completed'
    projection.completedAt = new Date().toISOString()
    projection.invoicesProcessed = result.invoicesProcessed
    projection.successCount = result.successCount
    projection.retainedCount = result.retainedCount
    projection.manualReviewCount = result.manualReviewCount
    projection.exportArtifactId = result.exportArtifactId
  }

  fail(jobId: string): void {
    const projection = this.projections.get(jobId)
    if (!projection) return
    projection.status = 'failed'
    projection.completedAt = new Date().toISOString()
  }

  cancel(jobId: string): void {
    const projection = this.projections.get(jobId)
    if (!projection) return
    projection.status = 'cancelled'
    projection.completedAt = new Date().toISOString()
  }

  getProjection(jobId: string): RunProjection | undefined {
    return this.projections.get(jobId)
  }
}
