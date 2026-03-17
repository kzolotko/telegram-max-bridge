interface StoredEntry {
  tgMsgId: number;
  maxMsgId: string;
  timestamp: number;
}

const TTL_MS = 24 * 60 * 60 * 1000; // 24 hours
const CLEANUP_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes

export class MessageStore {
  // key: "chatPairName:tgMsgId" -> maxMsgId
  private tgToMax = new Map<string, string>();
  // key: "chatPairName:maxMsgId" -> tgMsgId
  private maxToTg = new Map<string, number>();
  // all entries for TTL cleanup
  private entries: StoredEntry[] = [];
  private cleanupTimer: ReturnType<typeof setInterval> | null = null;

  start(): void {
    this.cleanupTimer = setInterval(() => this.cleanup(), CLEANUP_INTERVAL_MS);
  }

  stop(): void {
    if (this.cleanupTimer) {
      clearInterval(this.cleanupTimer);
      this.cleanupTimer = null;
    }
  }

  store(chatPairName: string, tgMsgId: number, maxMsgId: string): void {
    const tgKey = `${chatPairName}:${tgMsgId}`;
    const maxKey = `${chatPairName}:${maxMsgId}`;
    this.tgToMax.set(tgKey, maxMsgId);
    this.maxToTg.set(maxKey, tgMsgId);
    this.entries.push({ tgMsgId, maxMsgId, timestamp: Date.now() });
  }

  getMaxMsgId(chatPairName: string, tgMsgId: number): string | undefined {
    return this.tgToMax.get(`${chatPairName}:${tgMsgId}`);
  }

  getTgMsgId(chatPairName: string, maxMsgId: string): number | undefined {
    return this.maxToTg.get(`${chatPairName}:${maxMsgId}`);
  }

  private cleanup(): void {
    const cutoff = Date.now() - TTL_MS;
    const remaining: StoredEntry[] = [];

    for (const entry of this.entries) {
      if (entry.timestamp < cutoff) {
        // Remove expired entries from both maps
        // We don't know chatPairName here, so we iterate maps
        // This is acceptable for the small scale (up to 10 users, few chats)
        for (const [key, val] of this.tgToMax) {
          if (key.endsWith(`:${entry.tgMsgId}`) && val === entry.maxMsgId) {
            this.tgToMax.delete(key);
            break;
          }
        }
        for (const [key, val] of this.maxToTg) {
          if (key.endsWith(`:${entry.maxMsgId}`) && val === entry.tgMsgId) {
            this.maxToTg.delete(key);
            break;
          }
        }
      } else {
        remaining.push(entry);
      }
    }

    this.entries = remaining;
    console.log(`[MessageStore] Cleanup: kept ${remaining.length} entries`);
  }
}
