import { loadConfig, ConfigLookup } from './config';
import { MessageStore } from './message-store';
import { EchoGuard } from './bridge/echo-guard';
import { TelegramSenderPool } from './telegram/sender';
import { MaxSenderPool } from './max/sender';
import { TelegramListener } from './telegram/listener';
import { MaxListener } from './max/listener';
import { Bridge } from './bridge/bridge';

async function main(): Promise<void> {
  console.log('[Bridge] Loading configuration...');
  const config = loadConfig();
  const lookup = new ConfigLookup(config);

  console.log(`[Bridge] ${config.chatPairs.length} chat pair(s), ${config.users.length} user mapping(s)`);

  // Initialize message store
  const messageStore = new MessageStore();
  messageStore.start();

  // Initialize echo guard
  const echoGuard = new EchoGuard();

  // Initialize sender pools
  console.log('[Bridge] Initializing Telegram sender bots...');
  const tgSenders = new TelegramSenderPool();
  await tgSenders.init(config.users, config.fallback);

  console.log('[Bridge] Initializing MAX sender bots...');
  const maxSenders = new MaxSenderPool();
  await maxSenders.init(config.users, config.fallback, config.max.apiBaseUrl);

  // Register all bot IDs in echo guard
  for (const id of tgSenders.getAllBotIds()) {
    echoGuard.addBotId(id);
  }
  for (const id of maxSenders.getAllBotIds()) {
    echoGuard.addBotId(id);
  }

  // Create bridge
  const bridge = new Bridge(lookup, messageStore, tgSenders, maxSenders);
  const handleEvent = bridge.handleEvent.bind(bridge);

  // Initialize listeners
  console.log('[Bridge] Starting Telegram listener...');
  const tgListener = new TelegramListener(
    config.telegram.listenerBotToken,
    lookup,
    echoGuard,
    handleEvent,
  );
  const tgListenerId = await tgListener.start();
  echoGuard.addBotId(tgListenerId);

  console.log('[Bridge] Starting MAX listener...');
  const maxListener = new MaxListener(
    config.max.listenerBotToken,
    lookup,
    echoGuard,
    handleEvent,
    config.max.apiBaseUrl,
  );
  const maxListenerId = await maxListener.start();
  echoGuard.addBotId(maxListenerId);

  console.log('[Bridge] Ready! Bridging messages between Telegram and MAX.');

  // Graceful shutdown
  const shutdown = (): void => {
    console.log('[Bridge] Shutting down...');
    tgListener.stop();
    maxListener.stop();
    messageStore.stop();
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  console.error('[Bridge] Fatal error:', err);
  process.exit(1);
});
