import * as fs from 'node:fs'
import * as path from 'node:path'

export interface InvoiceSettings {
  email: string
  dateFrom: string
  dateTo: string
  company: string
  savePath: string
  ocrProvider: 'local' | 'glm'
}

const DEFAULT_SETTINGS: InvoiceSettings = {
  email: '',
  dateFrom: '',
  dateTo: '',
  company: '',
  savePath: '',
  ocrProvider: 'local',
}

function settingsPath(profileDir: string): string {
  return path.join(profileDir, 'settings.json')
}

export function readSettings(profileDir: string): InvoiceSettings {
  try {
    const value = JSON.parse(fs.readFileSync(settingsPath(profileDir), 'utf8')) as Partial<InvoiceSettings>
    return {
      email: typeof value.email === 'string' ? value.email : '',
      dateFrom: typeof value.dateFrom === 'string' ? value.dateFrom : '',
      dateTo: typeof value.dateTo === 'string' ? value.dateTo : '',
      company: typeof value.company === 'string' ? value.company : '',
      savePath: typeof value.savePath === 'string' ? value.savePath : '',
      ocrProvider: value.ocrProvider === 'glm' ? 'glm' : 'local',
    }
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

export function writeSettings(profileDir: string, settings: InvoiceSettings): void {
  fs.mkdirSync(profileDir, { recursive: true })
  fs.writeFileSync(settingsPath(profileDir), `${JSON.stringify(settings, null, 2)}\n`, 'utf8')
}
