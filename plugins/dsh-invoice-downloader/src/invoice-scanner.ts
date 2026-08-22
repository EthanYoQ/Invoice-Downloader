import {
  BlockAssembler,
  type LlmCallConfig,
  type LlmRuntime,
  type Message,
} from '@deepseek-ai/dsh-llm'
import type { SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import {
  EngineIpcClient,
  type ExtractionError,
  type ExtractionRequest,
  type ExtractionResponse,
  type JobConfig,
  type JobResult,
} from './ipc-client.js'
import type { ModelVisibleEvent } from './storage.js'

export interface InvoiceScannerConfig {
  pythonPath: string
  engineRoot: string
  adapterRoot: string
  deadlineMs: number
  graceMs: number
  imapFetchTimeoutSeconds: number
}

export interface ScanRequest {
  jobId: string
  email: string
  authCode: string
  dateFrom: string
  dateTo: string
  company: string
  savePath: string
  ocrProvider: 'local' | 'glm'
  glmApiKey?: string
}

export interface ScanProgress {
  event: string
}

export interface ScanResult {
  status: 'completed' | 'failed' | 'cancelled'
  invoicesProcessed: number
  successCount: number
  retainedCount: number
  manualReviewCount: number
  exportPath?: string
}

export interface LlmContext {
  llm: Pick<LlmRuntime, 'prepareCall'>
  agentDefaultModel: {
    currentSelection(): { provider: string; model: string; reasoningEffort?: LlmCallConfig['reasoningEffort'] } | null
  }
}

export class InvoiceScanner {
  private llmContext: LlmContext | null = null
  private client: EngineIpcClient | null = null
  private running = false
  private cancelled = false
  private progressHandler: ((progress: ScanProgress) => void) | null = null

  constructor(
    private readonly subprocess: SubprocessRuntime,
    private readonly config: InvoiceScannerConfig,
  ) {}

  setLlmContext(context: LlmContext): void {
    this.llmContext = context
  }

  setProgressHandler(handler: (progress: ScanProgress) => void): void {
    this.progressHandler = handler
  }

  get isRunning(): boolean {
    return this.running
  }

  async startScan(request: ScanRequest, logModelVisible: (event: ModelVisibleEvent) => Message): Promise<ScanResult> {
    if (this.running) throw new Error('an invoice scan is already running')
    this.running = true
    this.cancelled = false
    this.client = new EngineIpcClient(
      this.subprocess,
      this.config.pythonPath,
      this.config.engineRoot,
      this.config.adapterRoot,
      this.config.graceMs,
    )
    this.client.onEvent(event => this.progressHandler?.({ event: event.event }))
    this.client.onExtraction(request => this.extractWithModel(request, logModelVisible))

    try {
      const result = await this.withDeadline(this.client.startJob({
        jobId: request.jobId,
        email: request.email,
        authCode: request.authCode,
        dateFrom: request.dateFrom,
        dateTo: request.dateTo,
        company: request.company,
        savePath: request.savePath,
        ocrProvider: request.ocrProvider,
        imapFetchTimeoutSeconds: this.config.imapFetchTimeoutSeconds,
        ...(request.glmApiKey ? { glmApiKey: request.glmApiKey } : {}),
      }))
      return toScanResult(result)
    } catch {
      if (this.cancelled) return emptyResult('cancelled')
      return emptyResult('failed')
    } finally {
      this.client = null
      this.running = false
    }
  }

  stopScan(): void {
    this.cancelled = true
    this.client?.stopJob()
  }

  private async extractWithModel(
    request: ExtractionRequest,
    logModelVisible: (event: ModelVisibleEvent) => Message,
  ): Promise<ExtractionResponse | ExtractionError> {
    const selection = this.llmContext?.agentDefaultModel.currentSelection()
    if (!selection || !this.llmContext) {
      return {
        requestId: request.requestId,
        errorCode: 'MODEL_NOT_CONFIGURED',
        errorMessage: '请选择可用模型后再开始扫描。',
        retryable: false,
      }
    }

    try {
      const prepared = await this.llmContext.llm.prepareCall({
        provider: selection.provider,
        model: selection.model,
        temperature: 0,
        maxTokens: 500,
        ...(selection.reasoningEffort === undefined ? {} : { reasoningEffort: selection.reasoningEffort }),
      })
      const message = logModelVisible({
        inputText: JSON.stringify({
          type: 'invoice.field-extraction',
          requestId: request.requestId,
          instruction: '从 OCR 文本提取发票字段，并仅返回合法 JSON。',
          modelConfig: prepared.config,
          ocrText: request.ocrText,
        }),
      })
      const assembler = new BlockAssembler()
      for await (const chunk of prepared.stream({ ...prepared.config, messages: [message] })) assembler.push(chunk)
      const finish = assembler.finish
      if (finish.kind === 'error' || finish.kind === 'aborted') {
        return {
          requestId: request.requestId,
          errorCode: 'LLM_ERROR',
          errorMessage: '模型提取失败。',
          retryable: false,
        }
      }
      const responseText = assembler.blocks()
        .filter((block): block is { type: 'text'; text: string } => block.type === 'text')
        .map(block => block.text)
        .join('\n')
      if (!responseText) {
        return {
          requestId: request.requestId,
          errorCode: 'LLM_EMPTY_RESPONSE',
          errorMessage: '模型未返回可用文本。',
          retryable: false,
        }
      }
      return {
        requestId: request.requestId,
        extractedFields: extractJson(responseText),
        rawModelOutput: responseText,
      }
    } catch {
      return {
        requestId: request.requestId,
        errorCode: 'LLM_ERROR',
        errorMessage: '模型提取失败。',
        retryable: false,
      }
    }
  }

  private async withDeadline(result: Promise<JobResult>): Promise<JobResult> {
    let timer: NodeJS.Timeout | undefined
    try {
      return await new Promise<JobResult>((resolve, reject) => {
        timer = setTimeout(() => {
          this.client?.stopJob()
          reject(new Error('invoice scan deadline exceeded'))
        }, this.config.deadlineMs)
        void result.then(resolve, reject)
      })
    } finally {
      if (timer) clearTimeout(timer)
    }
  }
}

function extractJson(responseText: string): Record<string, unknown> {
  try {
    const match = responseText.match(/\{[\s\S]*\}/)
    return match ? JSON.parse(match[0]) as Record<string, unknown> : { raw: responseText }
  } catch {
    return { raw: responseText }
  }
}

function toScanResult(result: JobResult): ScanResult {
  return {
    status: 'completed',
    invoicesProcessed: result.invoicesProcessed || 0,
    successCount: result.successCount || 0,
    retainedCount: result.retainedCount || 0,
    manualReviewCount: result.manualReviewCount || 0,
    ...(typeof result.exportPath === 'string' ? { exportPath: result.exportPath } : {}),
  }
}

function emptyResult(status: 'failed' | 'cancelled'): ScanResult {
  return {
    status,
    invoicesProcessed: 0,
    successCount: 0,
    retainedCount: 0,
    manualReviewCount: 0,
  }
}
