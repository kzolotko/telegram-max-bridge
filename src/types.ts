export interface ChatPair {
  name: string;
  telegramChatId: number;
  maxChatId: number;
}

export interface UserMapping {
  name: string;
  telegramUserId: number;
  maxUserId: number;
  telegramBotToken: string;
  maxBotToken: string;
}

export interface FallbackConfig {
  telegramBotToken: string;
  maxBotToken: string;
  messagePrefix: boolean;
}

export interface AppConfig {
  telegram: {
    listenerBotToken: string;
  };
  max: {
    listenerBotToken: string;
    apiBaseUrl?: string;
  };
  chatPairs: ChatPair[];
  users: UserMapping[];
  fallback?: FallbackConfig;
}

export type Direction = 'tg-to-max' | 'max-to-tg';

export type BridgeEventType = 'text' | 'photo' | 'video' | 'file' | 'audio' | 'sticker' | 'edit' | 'delete';

export interface MediaInfo {
  buffer: Buffer;
  filename: string;
  mimeType: string;
}

export interface BridgeEvent {
  direction: Direction;
  chatPair: ChatPair;
  user: UserMapping | null;
  senderDisplayName: string;
  type: BridgeEventType;
  text?: string;
  media?: MediaInfo;
  replyToSourceMsgId?: number | string;
  editSourceMsgId?: number | string;
  deleteSourceMsgId?: number | string;
  sourceMsgId: number | string;
}
