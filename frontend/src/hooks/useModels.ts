import { useState } from 'react'
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

  function loadModels(provider: Provider, apiKey: string): void {
    setState({ models: [], loading: true, error: null })
    listModels(provider, apiKey)
      .then((models) => setState({ models, loading: false, error: null }))
      .catch((err: Error) => setState({ models: [], loading: false, error: err.message }))
  }

  function reset(): void {
    setState({ models: [], loading: false, error: null })
  }

  return { ...state, loadModels, reset }
}
