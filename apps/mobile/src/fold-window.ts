import { Capacitor, registerPlugin } from '@capacitor/core'

export type FoldPosture = 'flat' | 'half-opened' | 'unknown'
export type MobileDisplayRole = 'cover' | 'inner' | 'unknown'

export interface FoldWindowState {
  displayRole: MobileDisplayRole
  foldBounds: null | { bottom: number; left: number; right: number; top: number }
  heightDp: number
  isSeparating: boolean
  posture: FoldPosture
  widthDp: number
}

interface FoldWindowPlugin {
  getState(): Promise<FoldWindowState>
  addListener(
    eventName: 'changed',
    listenerFunc: (state: FoldWindowState) => void
  ): Promise<{ remove: () => Promise<void> }>
}

const FoldWindow = registerPlugin<FoldWindowPlugin>('FoldWindow')

function inferredState(): FoldWindowState {
  // Android WebView reports layout viewport dimensions in CSS px, which already
  // match Android dp for our responsive breakpoints. Dividing by devicePixelRatio
  // misclassified the Fold inner display (690dp) as the compact cover display.
  const widthDp = Math.round(window.innerWidth)
  const heightDp = Math.round(window.innerHeight)

  return {
    displayRole: widthDp < 480 ? 'cover' : widthDp >= 600 ? 'inner' : 'unknown',
    foldBounds: null,
    heightDp,
    isSeparating: false,
    posture: 'unknown',
    widthDp
  }
}

function applyState(state: FoldWindowState): void {
  const root = document.documentElement
  const isCompact = state.widthDp < 480 || state.heightDp < 480
  const isWide = state.widthDp >= 760 && state.heightDp >= 480

  root.dataset.foldPosture = state.posture
  root.dataset.displayRole = state.displayRole
  root.dataset.mobileLayout = isCompact ? 'compact' : isWide ? 'wide' : 'inner'
  root.style.setProperty('--mobile-window-width-dp', `${state.widthDp}`)
  root.style.setProperty('--mobile-window-height-dp', `${state.heightDp}`)
  root.style.setProperty('--mobile-fold-left-dp', `${state.foldBounds?.left ?? -1}`)
  root.style.setProperty('--mobile-fold-top-dp', `${state.foldBounds?.top ?? -1}`)
  root.style.setProperty('--mobile-fold-right-dp', `${state.foldBounds?.right ?? -1}`)
  root.style.setProperty('--mobile-fold-bottom-dp', `${state.foldBounds?.bottom ?? -1}`)
}

export async function startFoldWindowTracking(): Promise<() => void> {
  let removeNativeListener: (() => Promise<void>) | undefined
  const updateFallback = () => applyState(inferredState())

  updateFallback()
  window.addEventListener('resize', updateFallback)
  window.addEventListener('orientationchange', updateFallback)

  if (!Capacitor.isNativePlatform()) {
    return () => {
      window.removeEventListener('resize', updateFallback)
      window.removeEventListener('orientationchange', updateFallback)
    }
  }

  try {
    applyState(await FoldWindow.getState())
    const listener = await FoldWindow.addListener('changed', applyState)
    removeNativeListener = listener.remove
  } catch {
    // A missing native plugin still leaves development builds responsive.
  }

  return () => {
    window.removeEventListener('resize', updateFallback)
    window.removeEventListener('orientationchange', updateFallback)
    void removeNativeListener?.()
  }
}
