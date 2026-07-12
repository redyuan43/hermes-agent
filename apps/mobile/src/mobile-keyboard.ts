import { Capacitor, registerPlugin } from '@capacitor/core'

interface MobileKeyboardPlugin {
  show(): Promise<void>
}

const MobileKeyboard = registerPlugin<MobileKeyboardPlugin>('MobileKeyboard')

const EDITABLE_TARGET = [
  'textarea',
  'input:not([type="button"]):not([type="checkbox"]):not([type="radio"]):not([type="range"])',
  '[contenteditable]:not([contenteditable="false"])'
].join(',')

function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof Element && Boolean(target.closest(EDITABLE_TARGET))
}

export function installMobileKeyboardActivation(): () => void {
  if (!Capacitor.isNativePlatform()) {
    return () => undefined
  }

  let timer: number | undefined
  const requestKeyboard = (event: Event) => {
    if (!isEditableTarget(event.target)) {
      return
    }

    window.clearTimeout(timer)
    timer = window.setTimeout(() => {
      void MobileKeyboard.show().catch(() => undefined)
    }, 80)
  }

  document.addEventListener('focusin', requestKeyboard, true)
  document.addEventListener('pointerup', requestKeyboard, true)

  return () => {
    window.clearTimeout(timer)
    document.removeEventListener('focusin', requestKeyboard, true)
    document.removeEventListener('pointerup', requestKeyboard, true)
  }
}
