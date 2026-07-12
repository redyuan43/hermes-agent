import { Capacitor, registerPlugin } from '@capacitor/core'

interface MobileSecretsPlugin {
  get(options: { key: string }): Promise<{ value: null | string }>
  remove(options: { key: string }): Promise<void>
  set(options: { key: string; value: string }): Promise<void>
}

const MobileSecrets = registerPlugin<MobileSecretsPlugin>('MobileSecrets')

const browserSecretStore = sessionStorage

export async function getMobileSecret(key: string): Promise<null | string> {
  if (!Capacitor.isNativePlatform()) {
    return browserSecretStore.getItem(key)
  }

  return (await MobileSecrets.get({ key })).value
}

export async function removeMobileSecret(key: string): Promise<void> {
  if (!Capacitor.isNativePlatform()) {
    browserSecretStore.removeItem(key)

    return
  }

  await MobileSecrets.remove({ key })
}

export async function setMobileSecret(key: string, value: string): Promise<void> {
  if (!Capacitor.isNativePlatform()) {
    browserSecretStore.setItem(key, value)

    return
  }

  await MobileSecrets.set({ key, value })
}
