import { useRef, useState } from 'react'
import { listModels } from '../api/client'
import type { ModelInfo, Provider } from '../types'

interface UseModelsState {
  models: ModelInfo[]
  loading: boolean
  error: string | null
}

interface UseModelsReturn extends UseModelsState {
  loadModels: (provider: Provider, apiKey: string) => void
  reset: () => void
}

export function useModels(): UseModelsReturn {
  const [state, setState] = useState<UseModelsState>({
    models: [],
    loading: false,
    error: null,
  })

  // Audit-F29 — staleness token. Switching provider mid-load fires a
  // second listModels; whichever RESOLVES last used to win, so a slow
  // provider-A response could clobber the freshly-selected provider-B
  // models (and let the user submit an invalid model id). Only the
  // request whose id still matches the latest issued one may write state.
  const requestIdRef = useRef(0)

  function loadModels(provider: Provider, apiKey: string): void {
    const requestId = ++requestIdRef.current
    setState({ models: [], loading: true, error: null })
    listModels(provider, apiKey)
      .then((models) => {
        if (requestId !== requestIdRef.current) return // stale — ignore
        setState({ models, loading: false, error: null })
      })
      .catch((err: Error) => {
        if (requestId !== requestIdRef.current) return // stale — ignore
        setState({ models: [], loading: false, error: err.message })
      })
  }

  function reset(): void {
    // Invalidate any in-flight request so its late resolution can't
    // repopulate state after a reset.
    requestIdRef.current++
    setState({ models: [], loading: false, error: null })
  }

  return { ...state, loadModels, reset }
}
