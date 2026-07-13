/**
 * Controllable EventSource stand-in for hook tests.
 *
 * jsdom has no EventSource; useJobStream opens one per job. This fake
 * records listeners so a test can dispatch named SSE events (and drive
 * onopen/onerror) deterministically. Install it via `installFakeEventSource`.
 */
import { vi } from 'vitest'

type Listener = (e: MessageEvent) => void

export class FakeEventSource {
  static instances: FakeEventSource[] = []

  url: string
  readyState = 0
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  private listeners = new Map<string, Listener[]>()
  closed = false

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, fn: Listener): void {
    const arr = this.listeners.get(name) ?? []
    arr.push(fn)
    this.listeners.set(name, arr)
  }

  /** Deliver a named SSE event with a JSON payload. */
  dispatch(name: string, data: unknown = {}): void {
    const evt = { data: JSON.stringify(data) } as MessageEvent
    for (const fn of this.listeners.get(name) ?? []) fn(evt)
  }

  open(): void {
    this.readyState = 1
    this.onopen?.()
  }

  error(): void {
    this.onerror?.()
  }

  close(): void {
    this.closed = true
    this.readyState = 2
  }

  static reset(): void {
    FakeEventSource.instances = []
  }

  static last(): FakeEventSource {
    const arr = FakeEventSource.instances
    const i = arr[arr.length - 1]
    if (!i) throw new Error('no FakeEventSource created')
    return i
  }
}

export function installFakeEventSource(): void {
  FakeEventSource.reset()
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
}
