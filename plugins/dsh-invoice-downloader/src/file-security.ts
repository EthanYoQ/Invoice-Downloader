import * as crypto from 'node:crypto'
import * as fs from 'node:fs'
import * as path from 'node:path'

export interface ArtifactRef {
  artifactId: string
  jobId: string
  fileName: string
  category: 'excel-export'
  sha256: string
  size: number
}

interface StoredArtifact extends ArtifactRef {
  absolutePath: string
  outputRoot: string
}

export class ArtifactRegistry {
  private readonly artifacts = new Map<string, StoredArtifact>()

  validateSavePath(inputPath: string): { ok: true; canonicalPath: string } | { ok: false; errorMessage: string } {
    const trimmed = inputPath.trim()
    if (!trimmed) return { ok: false, errorMessage: '请选择或输入绝对保存路径。' }
    if (!path.isAbsolute(trimmed)) return { ok: false, errorMessage: '保存路径必须是绝对路径。' }
    try {
      fs.mkdirSync(trimmed, { recursive: true })
      const stat = fs.statSync(trimmed)
      if (!stat.isDirectory()) return { ok: false, errorMessage: '保存路径不是目录。' }
      return { ok: true, canonicalPath: fs.realpathSync(trimmed) }
    } catch {
      return { ok: false, errorMessage: '无法创建或访问保存路径。' }
    }
  }

  registerArtifact(jobId: string, filePath: string, outputRoot: string): ArtifactRef {
    const canonicalRoot = fs.realpathSync(outputRoot)
    const absolutePath = fs.realpathSync(filePath)
    if (!isWithinRoot(canonicalRoot, absolutePath)) {
      throw new Error('engine reported an artifact outside the authorized output directory')
    }
    const content = fs.readFileSync(absolutePath)
    const ref: StoredArtifact = {
      artifactId: `artifact-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`,
      jobId,
      fileName: path.basename(absolutePath),
      category: 'excel-export',
      sha256: crypto.createHash('sha256').update(content).digest('hex'),
      size: content.length,
      absolutePath,
      outputRoot: canonicalRoot,
    }
    this.artifacts.set(ref.artifactId, ref)
    return withoutPath(ref)
  }

  listArtifacts(jobId: string): ArtifactRef[] {
    return [...this.artifacts.values()]
      .filter(artifact => artifact.jobId === jobId)
      .map(withoutPath)
  }

  readArtifact(artifactId: string, jobId: string): { ok: true; content: Buffer; fileName: string } | { ok: false } {
    const artifact = this.artifacts.get(artifactId)
    if (!artifact || artifact.jobId !== jobId) return { ok: false }
    try {
      const current = fs.realpathSync(artifact.absolutePath)
      if (!isWithinRoot(artifact.outputRoot, current)) return { ok: false }
      return { ok: true, content: fs.readFileSync(current), fileName: artifact.fileName }
    } catch {
      return { ok: false }
    }
  }
}

function withoutPath({ absolutePath: _absolutePath, outputRoot: _outputRoot, ...artifact }: StoredArtifact): ArtifactRef {
  return artifact
}

function isWithinRoot(root: string, target: string): boolean {
  const relativePath = path.relative(root, target)
  return relativePath === '' || (
    relativePath !== '..'
    && !relativePath.startsWith(`..${path.sep}`)
    && !path.isAbsolute(relativePath)
  )
}
