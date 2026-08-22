import type { CredentialProvider, CredentialRef } from '@deepseek-ai/dsh-credentials'

function credentialRef(value: string): CredentialRef {
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(value)) {
    throw new TypeError(`credential ref "${value}" must use a shell identifier`)
  }
  return value as CredentialRef
}

export const IMAP_AUTH_CODE_REF = credentialRef('DSH_INVOICE_IMAP_AUTH_CODE')
export const GLM_API_KEY_REF = credentialRef('DSH_INVOICE_GLM_API_KEY')

export async function resolveCredential(
  service: CredentialProvider,
  ref: CredentialRef,
): Promise<string> {
  return (await service.resolve(ref))?.value ?? ''
}

export async function isCredentialConfigured(
  service: CredentialProvider,
  ref: CredentialRef,
): Promise<boolean> {
  return (await service.describe(ref)).configured
}
