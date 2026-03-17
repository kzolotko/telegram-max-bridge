import { Bot as MaxBot, ImageAttachment, VideoAttachment, AudioAttachment, FileAttachment } from '@maxhub/max-bot-api';
import { UserMapping, FallbackConfig, MediaInfo } from '../types';

interface SenderBot {
  api: MaxBot['api'];
  botId: number;
}

export class MaxSenderPool {
  private senders = new Map<number, SenderBot>(); // maxUserId -> SenderBot
  private fallbackSender?: SenderBot;

  async init(users: UserMapping[], fallback?: FallbackConfig, apiBaseUrl?: string): Promise<void> {
    for (const user of users) {
      const bot = new MaxBot(user.maxBotToken, apiBaseUrl ? {
        clientOptions: { baseUrl: apiBaseUrl },
      } : undefined);
      const info = await bot.api.getMyInfo();
      this.senders.set(user.maxUserId, {
        api: bot.api,
        botId: info.user_id,
      });
      console.log(`[MAX Sender] Initialized bot for ${user.name} (ID: ${info.user_id}, @${info.username})`);
    }

    if (fallback) {
      const bot = new MaxBot(fallback.maxBotToken, apiBaseUrl ? {
        clientOptions: { baseUrl: apiBaseUrl },
      } : undefined);
      const info = await bot.api.getMyInfo();
      this.fallbackSender = {
        api: bot.api,
        botId: info.user_id,
      };
      console.log(`[MAX Sender] Initialized fallback bot (ID: ${info.user_id}, @${info.username})`);
    }
  }

  getAllBotIds(): number[] {
    const ids: number[] = [];
    for (const sender of this.senders.values()) {
      ids.push(sender.botId);
    }
    if (this.fallbackSender) {
      ids.push(this.fallbackSender.botId);
    }
    return ids;
  }

  private getSenderOrFallback(maxUserId?: number): SenderBot | undefined {
    if (maxUserId) {
      const sender = this.senders.get(maxUserId);
      if (sender) return sender;
    }
    return this.fallbackSender;
  }

  async sendText(
    chatId: number,
    text: string,
    senderMaxUserId?: number,
    replyToMsgId?: string,
  ): Promise<string | undefined> {
    const sender = this.getSenderOrFallback(senderMaxUserId);
    if (!sender) return undefined;

    const msg = await sender.api.sendMessageToChat(chatId, text, {
      link: replyToMsgId ? { type: 'reply', mid: replyToMsgId } : undefined,
    });
    return msg.body.mid;
  }

  async sendPhoto(
    chatId: number,
    media: MediaInfo,
    caption: string | undefined,
    senderMaxUserId?: number,
    replyToMsgId?: string,
  ): Promise<string | undefined> {
    const sender = this.getSenderOrFallback(senderMaxUserId);
    if (!sender) return undefined;

    const attachment = await sender.api.uploadImage({ source: media.buffer });
    const msg = await sender.api.sendMessageToChat(chatId, caption || '', {
      attachments: [attachment as unknown as Parameters<typeof sender.api.sendMessageToChat>[2] extends { attachments?: infer A } ? A extends (infer E)[] ? E : never : never],
      link: replyToMsgId ? { type: 'reply', mid: replyToMsgId } : undefined,
    });
    return msg.body.mid;
  }

  async sendVideo(
    chatId: number,
    media: MediaInfo,
    caption: string | undefined,
    senderMaxUserId?: number,
    replyToMsgId?: string,
  ): Promise<string | undefined> {
    const sender = this.getSenderOrFallback(senderMaxUserId);
    if (!sender) return undefined;

    const attachment = await sender.api.uploadVideo({ source: media.buffer });
    const msg = await sender.api.sendMessageToChat(chatId, caption || '', {
      attachments: [attachment as any],
      link: replyToMsgId ? { type: 'reply', mid: replyToMsgId } : undefined,
    });
    return msg.body.mid;
  }

  async sendDocument(
    chatId: number,
    media: MediaInfo,
    caption: string | undefined,
    senderMaxUserId?: number,
    replyToMsgId?: string,
  ): Promise<string | undefined> {
    const sender = this.getSenderOrFallback(senderMaxUserId);
    if (!sender) return undefined;

    const attachment = await sender.api.uploadFile({ source: media.buffer });
    const msg = await sender.api.sendMessageToChat(chatId, caption || '', {
      attachments: [attachment as any],
      link: replyToMsgId ? { type: 'reply', mid: replyToMsgId } : undefined,
    });
    return msg.body.mid;
  }

  async sendAudio(
    chatId: number,
    media: MediaInfo,
    caption: string | undefined,
    senderMaxUserId?: number,
    replyToMsgId?: string,
  ): Promise<string | undefined> {
    const sender = this.getSenderOrFallback(senderMaxUserId);
    if (!sender) return undefined;

    const attachment = await sender.api.uploadAudio({ source: media.buffer });
    const msg = await sender.api.sendMessageToChat(chatId, caption || '', {
      attachments: [attachment as any],
      link: replyToMsgId ? { type: 'reply', mid: replyToMsgId } : undefined,
    });
    return msg.body.mid;
  }

  async editMessage(
    messageId: string,
    text: string,
    senderMaxUserId?: number,
  ): Promise<void> {
    const sender = this.getSenderOrFallback(senderMaxUserId);
    if (!sender) return;

    await sender.api.editMessage(messageId, { text });
  }

  async deleteMessage(
    messageId: string,
    senderMaxUserId?: number,
  ): Promise<void> {
    const sender = this.getSenderOrFallback(senderMaxUserId);
    if (!sender) return;

    await sender.api.deleteMessage(messageId);
  }
}
