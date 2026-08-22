import { credentialRef, type CredentialProvider, type CredentialRef } from '@deepseek-ai/dsh-credentials'

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
