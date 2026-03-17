export class EchoGuard {
  private botIds = new Set<number>();
  private botUsernames = new Set<string>();

  addBotId(id: number): void {
    this.botIds.add(id);
  }

  addBotUsername(username: string): void {
    this.botUsernames.add(username.toLowerCase());
  }

  isBot(userId: number): boolean {
    return this.botIds.has(userId);
  }

  isBotByUsername(username: string): boolean {
    return this.botUsernames.has(username.toLowerCase());
  }
}
