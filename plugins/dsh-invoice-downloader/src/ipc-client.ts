import type { SubprocessHandle, SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import { join } from 'node:path'

export interface JobConfig {
  jobId: string
  email: string
  authCode: string
  dateFrom: string
  dateTo: string
  company: string
  savePath: string
  ocrProvider: 'local' | 'glm'
  imapFetchTimeoutSeconds: number
  glmApiKey?: string
}

export interface JobEvent {
  event: string
  [key: string]: unknown
}

export interface JobResult {
  status: 'completed'
  invoicesProcessed: number
  successCount?: number
  retainedCount?: number
  manualReviewCount?: number
  exportPath?: string
}

export interface ExtractionRequest {
  requestId: string
  ocrText: string
  documentTypeHint?: string
}

export interface ExtractionResponse {
  requestId: string
  extractedFields: Record<string, unknown>
  rawModelOutput?: string
}

export interface ExtractionError {
  requestId: string
  errorCode: string
  errorMessage: string
  retryable: boolean
}

export type ExtractionHandler = (request: ExtractionRequest) => Promise<ExtractionResponse | ExtractionError>

interface Frame {
  type: string
  jobId?: string
  event?: string
  requestId?: string
  ocrText?: string
  documentTypeHint?: string
  result?: JobResult
}

export class EngineIpcClient {
  private handle: SubprocessHandle | null = null
  private buffer = ''
  private extractionHandler: ExtractionHandler | null = null
  private eventHandler: ((event: JobEvent) => void) | null = null
  private resultResolve: ((result: JobResult) => void) | null = null
  private resultReject: ((error: Error) => void) | null = null

  constructor(
    private readonly subprocess: SubprocessRuntime,
    private readonly pythonPath: string,
    private readonly engineRoot: string,
    private readonly adapterRoot: string,
    private readonly graceMs: number,
  ) {}

  onExtraction(handler: ExtractionHandler): void {
    this.extractionHandler = handler
  }

  onEvent(handler: (event: JobEvent) => void): void {
    this.eventHandler = handler
  }

  async startJob(config: JobConfig): Promise<JobResult> {
    const handle = this.subprocess.spawn({
      argv: [
        this.pythonPath,
        '-c',
        'import runpy, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]); sys.path.insert(0, sys.argv[3]); runpy.run_module(sys.argv[4], run_name="__main__")',
        join(this.engineRoot, 'src'),
        join(this.engineRoot, 'src', 'invoice_engine'),
        this.adapterRoot,
        'dsh_runner',
      ],
      cwd: this.engineRoot,
      stdio: { stdin: 'pipe', stdout: 'pipe', stderr: { maxBytes: 1024 * 1024 } },
      graceMs: this.graceMs,
    })
    if (!handle.stdin || !handle.stdout) {
      handle.terminate()
      throw new Error('invoice engine did not provide required standard I/O')
    }
    this.handle = handle
    handle.stdin.write(`${JSON.stringify({
      type: 'job.start',
      protocolVersion: 1,
      timestamp: Date.now(),
      ...config,
    })}\n`)

    void this.readLoop(handle, handle.stdout).then(
      async () => {
        const outcome = await handle.done
        this.rejectIfPending(new Error(`invoice engine exited before returning a result (${String(outcome.exitCode)})`))
      },
      () => this.rejectIfPending(new Error('invoice engine communication failed')),
    )

    return new Promise<JobResult>((resolve, reject) => {
      this.resultResolve = resolve
      this.resultReject = reject
    })
  }

  stopJob(): void {
    this.handle?.terminate()
  }

  private async readLoop(handle: SubprocessHandle, stdout: NonNullable<SubprocessHandle['stdout']>): Promise<void> {
    for await (const chunk of stdout) {
      this.buffer += chunk
      const lines = this.buffer.split('\n')
      this.buffer = lines.pop() ?? ''
      for (const line of lines) {
        if (!line.trim()) continue
        try {
          await this.handleFrame(JSON.parse(line) as Frame, handle)
        } catch {
          continue
        }
      }
    }
  }

  private async handleFrame(frame: Frame, handle: SubprocessHandle): Promise<void> {
    switch (frame.type) {
      case 'job.event':
        if (typeof frame.event === 'string') this.eventHandler?.(frame as unknown as JobEvent)
        return
      case 'job.result':
        if (!frame.result) return
        this.resolveResult(frame.result)
        return
      case 'job.error':
        this.rejectIfPending(new Error('invoice engine reported a failure'))
        return
      case 'extraction.request':
        await this.respondToExtraction(frame, handle)
        return
      default:
        return
    }
  }

  private async respondToExtraction(frame: Frame, handle: SubprocessHandle): Promise<void> {
    const requestId = frame.requestId
    if (!requestId || !this.extractionHandler) return
    try {
      const response = await this.extractionHandler({
        requestId,
        ocrText: frame.ocrText ?? '',
        documentTypeHint: frame.documentTypeHint,
      })
      this.sendFrame(handle, {
        type: 'extraction.response',
        protocolVersion: 1,
        jobId: frame.jobId,
        timestamp: Date.now(),
        ...response,
      })
    } catch {
      this.sendFrame(handle, {
        type: 'extraction.error',
        protocolVersion: 1,
        jobId: frame.jobId,
        timestamp: Date.now(),
        requestId,
        errorCode: 'HANDLER_ERROR',
        errorMessage: 'model extraction failed',
        retryable: false,
      })
    }
  }

  private sendFrame(handle: SubprocessHandle, frame: Record<string, unknown>): void {
    if (!handle.stdin) throw new Error('invoice engine input stream is unavailable')
    handle.stdin.write(`${JSON.stringify(frame)}\n`)
  }

  private resolveResult(result: JobResult): void {
    const resolve = this.resultResolve
    this.resultResolve = null
    this.resultReject = null
    this.handle = null
    resolve?.(result)
  }

  private rejectIfPending(error: Error): void {
    const reject = this.resultReject
    this.resultResolve = null
    this.resultReject = null
    this.handle = null
    reject?.(error)
  }
}
