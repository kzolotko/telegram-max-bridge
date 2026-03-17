import * as fs from 'fs';
import * as path from 'path';
import * as yaml from 'js-yaml';
import { AppConfig, ChatPair, UserMapping } from './types';

interface RawConfig {
  telegram?: {
    listener_bot_token?: string;
  };
  max?: {
    listener_bot_token?: string;
    api_base_url?: string;
  };
  chat_pairs?: Array<{
    name?: string;
    telegram_chat_id?: number;
    max_chat_id?: number;
  }>;
  users?: Array<{
    name?: string;
    telegram_user_id?: number;
    max_user_id?: number;
    telegram_bot_token?: string;
    max_bot_token?: string;
  }>;
  fallback?: {
    telegram_bot_token?: string;
    max_bot_token?: string;
    message_prefix?: boolean;
  };
}

export function loadConfig(configPath?: string): AppConfig {
  const filePath = configPath || path.resolve(process.cwd(), 'config.yaml');

  if (!fs.existsSync(filePath)) {
    throw new Error(`Config file not found: ${filePath}`);
  }

  const raw = yaml.load(fs.readFileSync(filePath, 'utf-8')) as RawConfig;

  if (!raw.telegram?.listener_bot_token) {
    throw new Error('Missing telegram.listener_bot_token');
  }
  if (!raw.max?.listener_bot_token) {
    throw new Error('Missing max.listener_bot_token');
  }
  if (!raw.chat_pairs?.length) {
    throw new Error('At least one chat_pair is required');
  }

  const chatPairs: ChatPair[] = raw.chat_pairs.map((cp, i) => {
    if (!cp.name) throw new Error(`chat_pairs[${i}].name is required`);
    if (!cp.telegram_chat_id) throw new Error(`chat_pairs[${i}].telegram_chat_id is required`);
    if (!cp.max_chat_id) throw new Error(`chat_pairs[${i}].max_chat_id is required`);
    return {
      name: cp.name,
      telegramChatId: cp.telegram_chat_id,
      maxChatId: cp.max_chat_id,
    };
  });

  const users: UserMapping[] = (raw.users || []).map((u, i) => {
    if (!u.name) throw new Error(`users[${i}].name is required`);
    if (!u.telegram_user_id) throw new Error(`users[${i}].telegram_user_id is required`);
    if (!u.max_user_id) throw new Error(`users[${i}].max_user_id is required`);
    if (!u.telegram_bot_token) throw new Error(`users[${i}].telegram_bot_token is required`);
    if (!u.max_bot_token) throw new Error(`users[${i}].max_bot_token is required`);
    return {
      name: u.name,
      telegramUserId: u.telegram_user_id,
      maxUserId: u.max_user_id,
      telegramBotToken: u.telegram_bot_token,
      maxBotToken: u.max_bot_token,
    };
  });

  if (users.length > 10) {
    throw new Error('Maximum 10 users supported');
  }

  return {
    telegram: { listenerBotToken: raw.telegram.listener_bot_token },
    max: {
      listenerBotToken: raw.max.listener_bot_token,
      apiBaseUrl: raw.max.api_base_url,
    },
    chatPairs,
    users,
    fallback: raw.fallback?.telegram_bot_token && raw.fallback?.max_bot_token
      ? {
          telegramBotToken: raw.fallback.telegram_bot_token,
          maxBotToken: raw.fallback.max_bot_token,
          messagePrefix: raw.fallback.message_prefix !== false,
        }
      : undefined,
  };
}

// Lookup helpers
export class ConfigLookup {
  private tgChatToPair = new Map<number, ChatPair>();
  private maxChatToPair = new Map<number, ChatPair>();
  private tgUserToMapping = new Map<number, UserMapping>();
  private maxUserToMapping = new Map<number, UserMapping>();

  constructor(private config: AppConfig) {
    for (const pair of config.chatPairs) {
      this.tgChatToPair.set(pair.telegramChatId, pair);
      this.maxChatToPair.set(pair.maxChatId, pair);
    }
    for (const user of config.users) {
      this.tgUserToMapping.set(user.telegramUserId, user);
      this.maxUserToMapping.set(user.maxUserId, user);
    }
  }

  getChatPairByTgChat(chatId: number): ChatPair | undefined {
    return this.tgChatToPair.get(chatId);
  }

  getChatPairByMaxChat(chatId: number): ChatPair | undefined {
    return this.maxChatToPair.get(chatId);
  }

  getUserByTgId(userId: number): UserMapping | undefined {
    return this.tgUserToMapping.get(userId);
  }

  getUserByMaxId(userId: number): UserMapping | undefined {
    return this.maxUserToMapping.get(userId);
  }

  getConfig(): AppConfig {
    return this.config;
  }
}
