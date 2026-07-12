import './mobile.css'

import { hasMobileConnection, installMobileBridge } from './mobile-bridge'
import { startFoldWindowTracking } from './fold-window'
import { installMobileKeyboardActivation } from './mobile-keyboard'

async function start(): Promise<void> {
  installMobileBridge()
  installMobileKeyboardActivation()
  void startFoldWindowTracking()

  if (!(await hasMobileConnection())) {
    const { renderMobileSetup } = await import('./mobile-setup')
    renderMobileSetup()
    return
  }

  await import('../../desktop/src/main')
}

void start()
