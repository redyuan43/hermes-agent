import type { CapacitorConfig } from '@capacitor/cli'

const config: CapacitorConfig = {
  appId: 'com.nousresearch.hermes.mobile',
  appName: 'Hermes',
  webDir: 'dist',
  android: {
    allowMixedContent: false,
    backgroundColor: '#f8faff'
  },
  server: {
    androidScheme: 'https'
  }
}

export default config
