// Read-through TTL cache with stale-on-error (spec §5.6, §8). In-memory only.

export interface Cached<T> {
  value: T;
  fetchedAt: number;
  stale: boolean;
}

export class TtlCache<T> {
  private entry: Cached<T> | null = null;
  private inflight: Promise<T> | null = null;
  private readonly ttlMs: number;
  private readonly loader: () => Promise<T>;
  private readonly now: () => number;

  constructor(ttlMs: number, loader: () => Promise<T>, now: () => number = Date.now) {
    this.ttlMs = ttlMs;
    this.loader = loader;
    this.now = now;
  }

  /** Return a fresh value, a cached value within TTL, or (on loader failure) the last-good value flagged stale. */
  async get(): Promise<Cached<T>> {
    const e = this.entry;
    if (e && !e.stale && this.now() - e.fetchedAt < this.ttlMs) return e;

    return this.refresh();
  }

  /** Force a refresh while sharing the same in-flight loader with request-driven reads. */
  async refresh(): Promise<Cached<T>> {
    // Collapse concurrent refreshes.
    this.inflight ??= this.loader();
    try {
      const value = await this.inflight;
      this.entry = { value, fetchedAt: this.now(), stale: false };
      return this.entry;
    } catch (err) {
      if (this.entry) {
        // Serve last-good, flagged stale — don't overwrite fetchedAt so it keeps retrying.
        this.entry = { ...this.entry, stale: true };
        return this.entry;
      }
      throw err;
    } finally {
      this.inflight = null;
    }
  }

  peek(): Cached<T> | null {
    return this.entry;
  }
}
