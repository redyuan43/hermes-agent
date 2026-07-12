import { Capacitor } from '@capacitor/core'

import { getMobileSecret, removeMobileSecret, setMobileSecret } from './mobile-secrets'

export interface MobileConnectionConfig {
  accessToken: string
  baseUrl: string
  refreshToken: string
}

export interface MobileLoginResponse {
  access_token: string
  expires_at: number
  provider: string
  refresh_token: string
  user: {
    display_name: string
    email: string
    id: string
  }
}

const CONNECTION_STORAGE_KEY = 'hermes.mobile.connection'
let cachedConnection: MobileConnectionConfig | null | undefined
let refreshingConnection: null | Promise<MobileConnectionConfig> = null
const mobileTerminalSessions = new Map<string, MobileTerminalSession>()

interface MobileTerminalSession {
  dataListeners: Set<(payload: string) => void>
  decoder: TextDecoder
  exitPayload: null | { code: null | number; signal: null | number }
  exitListeners: Set<(payload: { code: null | number; signal: null | number }) => void>
  pendingData: string[]
  socket: WebSocket
}

function normalizeBaseUrl(value: string): string {
  return value.trim().replace(/\/+$/, '')
}

function parseConnection(raw: null | string): MobileConnectionConfig | null {
  try {
    const parsed = JSON.parse(raw || 'null') as Partial<MobileConnectionConfig> | null
    const baseUrl = normalizeBaseUrl(String(parsed?.baseUrl || ''))
    const accessToken = String(parsed?.accessToken || '')
    const refreshToken = String(parsed?.refreshToken || '')

    return baseUrl && accessToken && refreshToken ? { accessToken, baseUrl, refreshToken } : null
  } catch {
    return null
  }
}

async function readConnection(): Promise<MobileConnectionConfig | null> {
  if (cachedConnection !== undefined) {
    return cachedConnection
  }

  cachedConnection = parseConnection(await getMobileSecret(CONNECTION_STORAGE_KEY))
  return cachedConnection
}

async function writeConnection(next: MobileConnectionConfig | null): Promise<void> {
  cachedConnection = next
  if (!next) {
    await removeMobileSecret(CONNECTION_STORAGE_KEY)

    return
  }

  await setMobileSecret(CONNECTION_STORAGE_KEY, JSON.stringify(next))
}

async function connectionOrThrow(): Promise<MobileConnectionConfig> {
  const connection = await readConnection()
  if (!connection) {
    throw new Error('Connect Hermes from the mobile setup screen before starting a session.')
  }

  return connection
}

async function refreshMobileConnection(): Promise<MobileConnectionConfig> {
  if (refreshingConnection) {
    return refreshingConnection
  }

  refreshingConnection = (async () => {
    const connection = await connectionOrThrow()
    const response = await fetch(`${connection.baseUrl}/api/auth/mobile/refresh`, {
      body: JSON.stringify({ refresh_token: connection.refreshToken }),
      headers: { 'Content-Type': 'application/json' },
      method: 'POST'
    })
    if (!response.ok) {
      await writeConnection(null)
      throw new Error('Your Hermes mobile session expired. Sign in again.')
    }

    const session = (await response.json()) as MobileLoginResponse
    const next = {
      accessToken: session.access_token,
      baseUrl: connection.baseUrl,
      refreshToken: session.refresh_token
    }
    await writeConnection(next)
    return next
  })()

  try {
    return await refreshingConnection
  } finally {
    refreshingConnection = null
  }
}

async function requestHeaders(extra: HeadersInit = {}): Promise<Headers> {
  const headers = new Headers(extra)
  const accessToken = (await connectionOrThrow()).accessToken
  headers.set('X-Hermes-Mobile-Access', accessToken)
  return headers
}

async function mobileApi<T>(request: {
  body?: unknown
  method?: string
  path: string
  timeoutMs?: number
}): Promise<T> {
  const connection = await connectionOrThrow()
  const abort = new AbortController()
  const timeout = window.setTimeout(() => abort.abort(), request.timeoutMs ?? 30_000)
  const init = {
    body: request.body === undefined ? undefined : JSON.stringify(request.body),
    headers: await requestHeaders(request.body === undefined ? {} : { 'Content-Type': 'application/json' }),
    method: request.method || (request.body === undefined ? 'GET' : 'POST'),
    signal: abort.signal
  }

  try {
    let response = await fetch(`${connection.baseUrl}${request.path}`, init)
    if (response.status === 401) {
      const refreshed = await refreshMobileConnection()
      init.headers.set('X-Hermes-Mobile-Access', refreshed.accessToken)
      response = await fetch(`${refreshed.baseUrl}${request.path}`, init)
    }
    if (!response.ok) {
      throw new Error(`Hermes request failed (${response.status})`)
    }

    return (await response.json()) as T
  } finally {
    window.clearTimeout(timeout)
  }
}

async function gatewayWsUrl(): Promise<string> {
  return nativeWsUrl('/api/ws')
}

async function nativeWsUrl(path: string, params: Record<string, string> = {}): Promise<string> {
  const connection = await connectionOrThrow()
  const ticket = await mobileApi<{ ticket: string }>({
    method: 'POST',
    path: '/api/auth/ws-ticket'
  })
  const url = new URL(connection.baseUrl)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.pathname = `${url.pathname.replace(/\/+$/, '')}${path}`
  url.search = new URLSearchParams({ ...params, ticket: ticket.ticket }).toString()

  return url.toString()
}

async function startMobileTerminal(options: { cols?: number; cwd?: string; rows?: number } = {}) {
  const id = globalThis.crypto?.randomUUID?.() ?? `mobile-terminal-${Date.now()}`
  const url = await nativeWsUrl('/api/terminal', {
    cols: String(options.cols || 80),
    cwd: options.cwd || '',
    rows: String(options.rows || 24)
  })

  return new Promise<{ cwd: string; id: string; shell: string }>((resolve, reject) => {
    const socket = new WebSocket(url)
    const dataListeners = new Set<(payload: string) => void>()
    const exitListeners = new Set<(payload: { code: null | number; signal: null | number }) => void>()
    const decoder = new TextDecoder()
    let ready = false
    let settled = false
    const timeout = window.setTimeout(() => {
      if (!settled) {
        settled = true
        socket.close()
        reject(new Error('Timed out starting the remote terminal.'))
      }
    }, 15_000)

    const settle = (callback: () => void) => {
      if (settled) {
        return
      }
      settled = true
      window.clearTimeout(timeout)
      callback()
    }

    socket.binaryType = 'arraybuffer'
    socket.onmessage = event => {
      if (typeof event.data === 'string') {
        try {
          const message = JSON.parse(event.data) as { cwd?: string; shell?: string; type?: string }
          if (message.type === 'ready') {
            ready = true
            mobileTerminalSessions.set(id, {
              dataListeners,
              decoder,
              exitListeners,
              exitPayload: null,
              pendingData: [],
              socket
            })
            settle(() => resolve({ cwd: message.cwd || '', id, shell: message.shell || 'shell' }))
            return
          }
        } catch {
          // Shell data is normally binary; retain textual fallback for proxies.
        }
        const session = mobileTerminalSessions.get(id)
        if (!session?.dataListeners.size) {
          session?.pendingData.push(event.data)
          return
        }
        for (const listener of session.dataListeners) {
          listener(event.data)
        }
        return
      }

      const text = decoder.decode(event.data as ArrayBuffer, { stream: true })
      if (text) {
        const session = mobileTerminalSessions.get(id)
        if (!session?.dataListeners.size) {
          session?.pendingData.push(text)
          return
        }
        for (const listener of session.dataListeners) {
          listener(text)
        }
      }
    }
    socket.onerror = () => {
      settle(() => reject(new Error('Could not connect to the remote terminal.')))
    }
    socket.onclose = event => {
      const trailing = decoder.decode()
      if (trailing) {
        for (const listener of dataListeners) {
          listener(trailing)
        }
      }
      const exitPayload = { code: event.code || null, signal: null }
      const session = mobileTerminalSessions.get(id)
      if (session) {
        session.exitPayload = exitPayload
        if (session.exitListeners.size) {
          for (const listener of session.exitListeners) {
            listener(exitPayload)
          }
        }
      }
      if (!ready) {
        settle(() => reject(new Error('The remote terminal closed before it started.')))
      }
    }
  })
}

function unsupported(name: string): never {
  throw new Error(`${name} is not available in Hermes for Android yet.`)
}

function mobileConnectionConfig(connection: MobileConnectionConfig | null) {
  return {
    envOverride: false,
    mode: 'remote',
    profile: null,
    remoteAuthMode: 'token',
    remoteOauthConnected: Boolean(connection),
    remoteTokenPreview: null,
    remoteTokenSet: Boolean(connection),
    remoteUrl: connection?.baseUrl || ''
  }
}

export async function hasMobileConnection(): Promise<boolean> {
  return Boolean(await readConnection())
}

export async function connectMobile(payload: {
  baseUrl: string
  password: string
  provider: string
  username: string
}): Promise<MobileLoginResponse> {
  const baseUrl = normalizeBaseUrl(payload.baseUrl)
  const response = await fetch(`${baseUrl}/api/auth/mobile/login`, {
    body: JSON.stringify({
      password: payload.password,
      provider: payload.provider,
      username: payload.username
    }),
    headers: { 'Content-Type': 'application/json' },
    method: 'POST'
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => null)) as { detail?: string } | null
    throw new Error(detail?.detail || `Could not connect to Hermes (${response.status}).`)
  }

  const session = (await response.json()) as MobileLoginResponse
  await writeConnection({
    accessToken: session.access_token,
    baseUrl,
    refreshToken: session.refresh_token
  })
  return session
}

export async function disconnectMobile(): Promise<void> {
  const connection = await readConnection()
  if (connection) {
    await fetch(`${connection.baseUrl}/api/auth/mobile/logout`, {
      body: JSON.stringify({ refresh_token: connection.refreshToken }),
      headers: {
        'Content-Type': 'application/json',
        'X-Hermes-Mobile-Access': connection.accessToken
      },
      method: 'POST'
    }).catch(() => undefined)
  }
  await writeConnection(null)
}

export function installMobileBridge(): void {
  const bridge = {
    api: mobileApi,
    getConnection: async (profile?: null | string) => {
      const connection = await connectionOrThrow()

      return {
        authMode: 'token',
        baseUrl: connection.baseUrl,
        isFullscreen: false,
        logs: [],
        mode: 'remote',
        nativeOverlayWidth: 0,
        profile: profile || undefined,
        source: 'settings',
        token: '',
        windowButtonPosition: null,
        wsUrl: await gatewayWsUrl()
      }
    },
    getGatewayWsUrl: async () => gatewayWsUrl(),
    getBootProgress: async () => ({
      error: null,
      fakeMode: false,
      message: 'Connected to Hermes',
      phase: 'mobile.ready',
      progress: 100,
      running: true,
      timestamp: Date.now()
    }),
    getConnectionConfig: async () => mobileConnectionConfig(await readConnection()),
    saveConnectionConfig: async (payload: { remoteToken?: string; remoteUrl?: string }) => {
      const current = await readConnection()
      const baseUrl = normalizeBaseUrl(payload.remoteUrl || current?.baseUrl || '')
      if (!current || !baseUrl) {
        return mobileConnectionConfig(null)
      }
      await writeConnection({ ...current, baseUrl })
      return mobileConnectionConfig(await readConnection())
    },
    applyConnectionConfig: async (payload: { remoteToken?: string; remoteUrl?: string }) => bridge.saveConnectionConfig(payload),
    testConnectionConfig: async (payload: { remoteUrl?: string }) => {
      const baseUrl = normalizeBaseUrl(payload.remoteUrl || (await readConnection())?.baseUrl || '')
      const response = await fetch(`${baseUrl}/api/status`)
      return { baseUrl, ok: response.ok, version: null }
    },
    probeConnectionConfig: async (remoteUrl: string) => {
      const baseUrl = normalizeBaseUrl(remoteUrl)
      const response = await fetch(`${baseUrl}/api/auth/providers`)
      const body = response.ok ? await response.json() as { providers?: Array<{ display_name: string; name: string; supports_password?: boolean }> } : null
      return {
        authMode: 'token',
        baseUrl,
        error: response.ok ? null : `Hermes gateway did not respond (${response.status})`,
        providers: (body?.providers || []).map(provider => ({
          displayName: provider.display_name,
          name: provider.name,
          supportsPassword: provider.supports_password
        })),
        reachable: response.ok,
        version: null
      }
    },
    oauthLoginConnectionConfig: async () => unsupported('Browser sign-in'),
    oauthLogoutConnectionConfig: async () => {
      await disconnectMobile()
      return { connected: false, ok: true }
    },
    revalidateConnection: async () => ({ ok: Boolean(await readConnection()), rebuilt: false }),
    touchBackend: async () => ({ ok: true }),
    profile: {
      get: async () => ({ profile: null }),
      set: async (profile: null | string) => ({ profile })
    },
    notify: async () => false,
    requestMicrophoneAccess: async () => false,
    writeClipboard: async (text: string) => navigator.clipboard.writeText(text).then(() => true, () => false),
    openExternal: async (url: string) => {
      window.open(url, '_blank', 'noopener,noreferrer')?.focus()
    },
    getPathForFile: () => '',
    getRecentLogs: async () => ({ lines: [], path: '' }),
    revealLogs: async () => ({ ok: false, path: '' }),
    readFileDataUrl: async () => unsupported('Local file access'),
    readFileText: async () => unsupported('Local file access'),
    readDir: async () => unsupported('Local file access'),
    selectPaths: async () => unsupported('Native file picker'),
    saveImageFromUrl: async () => unsupported('Image download'),
    saveImageBuffer: async () => unsupported('Image download'),
    saveClipboardImage: async () => unsupported('Clipboard image'),
    normalizePreviewTarget: async () => null,
    watchPreviewFile: async () => unsupported('Preview watching'),
    stopPreviewFileWatch: async () => false,
    sanitizeWorkspaceCwd: async (cwd?: null | string) => ({ cwd: cwd || '', sanitized: false }),
    settings: {
      getDefaultProjectDir: async () => ({ defaultLabel: '', dir: null, resolvedCwd: '' }),
      pickDefaultProjectDir: async () => ({ canceled: true, dir: null }),
      setDefaultProjectDir: async (dir: null | string) => ({ dir })
    },
    terminal: {
      dispose: async (id: string) => {
        const session = mobileTerminalSessions.get(id)
        if (!session) {
          return false
        }
        session.socket.close()
        mobileTerminalSessions.delete(id)
        return true
      },
      onData: (id: string, callback: (payload: string) => void) => {
        const session = mobileTerminalSessions.get(id)
        if (!session) {
          return () => undefined
        }
        session.dataListeners.add(callback)
        for (const payload of session.pendingData.splice(0)) {
          callback(payload)
        }
        return () => session.dataListeners.delete(callback)
      },
      onExit: (id: string, callback: (payload: { code: null | number; signal: null | number }) => void) => {
        const session = mobileTerminalSessions.get(id)
        if (!session) {
          return () => undefined
        }
        session.exitListeners.add(callback)
        if (session.exitPayload) {
          callback(session.exitPayload)
        }
        return () => session.exitListeners.delete(callback)
      },
      resize: async (id: string, size: { cols: number; rows: number }) => {
        const session = mobileTerminalSessions.get(id)
        if (!session || session.socket.readyState !== WebSocket.OPEN) {
          return false
        }
        session.socket.send(`\x1b[RESIZE:${size.cols};${size.rows}]`)
        return true
      },
      start: startMobileTerminal,
      write: async (id: string, data: string) => {
        const session = mobileTerminalSessions.get(id)
        if (!session || session.socket.readyState !== WebSocket.OPEN) {
          return false
        }
        session.socket.send(data)
        return true
      }
    },
    getBootstrapState: async () => ({
      active: false,
      completedAt: null,
      error: null,
      log: [],
      manifest: null,
      stages: {},
      startedAt: null,
      unsupportedPlatform: null
    }),
    resetBootstrap: async () => ({ ok: false }),
    repairBootstrap: async () => ({ ok: false }),
    cancelBootstrap: async () => ({ cancelled: false, ok: false }),
    onBootstrapEvent: () => () => undefined,
    onBootProgress: () => () => undefined,
    onBackendExit: () => () => undefined,
    onPreviewFileChanged: () => () => undefined,
    getVersion: async () => ({
      appVersion: 'mobile',
      electronVersion: '',
      hermesRoot: '',
      nodeVersion: '',
      platform: Capacitor.getPlatform()
    })
  }

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: bridge
  })
  document.documentElement.dataset.hermesPlatform = 'mobile'
}
