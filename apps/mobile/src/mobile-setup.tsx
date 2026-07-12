import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'

import { connectMobile } from './mobile-bridge'

interface Provider {
  display_name: string
  name: string
  supports_password?: boolean
}

function MobileSetup(): React.JSX.Element {
  const [baseUrl, setBaseUrl] = useState('')
  const [providers, setProviders] = useState<Provider[]>([])
  const [provider, setProvider] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loadingProviders, setLoadingProviders] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  const loadProviders = async () => {
    const normalized = baseUrl.trim().replace(/\/+$/, '')
    if (!normalized) {
      return
    }

    setLoadingProviders(true)
    setError('')
    try {
      const response = await fetch(`${normalized}/api/auth/providers`)
      if (!response.ok) {
        throw new Error(`Hermes gateway did not respond (${response.status}).`)
      }
      const body = (await response.json()) as { providers?: Provider[] }
      const passwordProviders = (body.providers || []).filter(item => item.supports_password)
      if (passwordProviders.length === 0) {
        throw new Error('This Hermes gateway does not expose a password sign-in provider for the mobile app.')
      }
      setProviders(passwordProviders)
      setProvider(passwordProviders[0].name)
    } catch (reason) {
      setProviders([])
      setProvider('')
      setError(reason instanceof Error ? reason.message : 'Could not reach Hermes.')
    } finally {
      setLoadingProviders(false)
    }
  }

  useEffect(() => {
    const timeout = window.setTimeout(() => void loadProviders(), 450)
    return () => window.clearTimeout(timeout)
  }, [baseUrl])

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!provider) {
      setError('Enter your Hermes address and wait for the sign-in provider.')
      return
    }

    setSubmitting(true)
    setError('')
    try {
      await connectMobile({ baseUrl, password, provider, username })
      window.location.reload()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not connect to Hermes.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="mobile-setup">
      <section className="mobile-setup__content">
        <p className="mobile-setup__eyebrow">Hermes Mobile</p>
        <h1>Connect your private Hermes</h1>
        <p className="mobile-setup__copy">
          Use the HTTPS address reachable from this phone over your Tailnet.
        </p>
        <form className="mobile-setup__form" onSubmit={event => void submit(event)}>
          <label>
            Hermes address
            <input
              autoCapitalize="none"
              autoComplete="url"
              inputMode="url"
              onChange={event => setBaseUrl(event.target.value)}
              placeholder="https://hermes.your-tailnet.ts.net"
              required
              type="url"
              value={baseUrl}
            />
          </label>
          <label>
            Sign-in provider
            <input
              autoCapitalize="none"
              list="hermes-mobile-providers"
              onChange={event => setProvider(event.target.value)}
              placeholder={loadingProviders ? 'Discovering provider...' : 'Provider name'}
              required
              value={provider}
            />
            <datalist id="hermes-mobile-providers">
              {providers.map(item => <option key={item.name} label={item.display_name} value={item.name} />)}
            </datalist>
          </label>
          <label>
            Username
            <input autoCapitalize="none" autoComplete="username" onChange={event => setUsername(event.target.value)} required value={username} />
          </label>
          <label>
            Password
            <input autoComplete="current-password" onChange={event => setPassword(event.target.value)} required type="password" value={password} />
          </label>
          {error ? <p className="mobile-setup__error" role="alert">{error}</p> : null}
          <button disabled={submitting} type="submit">{submitting ? 'Connecting...' : 'Connect'}</button>
        </form>
      </section>
    </main>
  )
}

export function renderMobileSetup(): void {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <MobileSetup />
    </StrictMode>
  )
}
