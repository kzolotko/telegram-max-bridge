import { Api, Bot, InputFile } from 'grammy';
import { UserMapping, FallbackConfig, MediaInfo } from '../types';

interface SenderBot {
  api: Api;
  botId: number;
}

export class TelegramSenderPool {
  private senders = new Map<number, SenderBot>(); // telegramUserId -> SenderBot
  private fallbackSender?: SenderBot;

  async init(users: UserMapping[], fallback?: FallbackConfig): Promise<void> {
    for (const user of users) {
      const bot = new Bot(user.telegramBotToken);
      const me = await bot.api.getMe();
      this.senders.set(user.telegramUserId, {
        api: bot.api,
        botId: me.id,
      });
      console.log(`[TG Sender] Initialized bot for ${user.name} (ID: ${me.id}, @${me.username})`);
    }

    if (fallback) {
      const bot = new Bot(fallback.telegramBotToken);
      const me = await bot.api.getMe();
      this.fallbackSender = {
        api: bot.api,
        botId: me.id,
      };
      console.log(`[TG Sender] Initialized fallback bot (ID: ${me.id}, @${me.username})`);
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

  private getSender(telegramUserId: number): SenderBot | undefined {
    return this.senders.get(telegramUserId);
  }

  private getSenderOrFallback(telegramUserId?: number): SenderBot | undefined {
    if (telegramUserId) {
      const sender = this.senders.get(telegramUserId);
      if (sender) return sender;
    }
    return this.fallbackSender;
  }

  async sendText(
    chatId: number,
    text: string,
    senderUserId?: number,
    replyToMsgId?: number,
  ): Promise<number | undefined> {
    const sender = this.getSenderOrFallback(senderUserId);
    if (!sender) return undefined;

    const msg = await sender.api.sendMessage(chatId, text, {
      reply_parameters: replyToMsgId ? { message_id: replyToMsgId } : undefined,
    });
    return msg.message_id;
  }

  async sendPhoto(
    chatId: number,
    media: MediaInfo,
    caption: string | undefined,
    senderUserId?: number,
    replyToMsgId?: number,
  ): Promise<number | undefined> {
    const sender = this.getSenderOrFallback(senderUserId);
    if (!sender) return undefined;

    const msg = await sender.api.sendPhoto(chatId, new InputFile(media.buffer, media.filename), {
      caption,
      reply_parameters: replyToMsgId ? { message_id: replyToMsgId } : undefined,
    });
    return msg.message_id;
  }

  async sendVideo(
    chatId: number,
    media: MediaInfo,
    caption: string | undefined,
    senderUserId?: number,
    replyToMsgId?: number,
  ): Promise<number | undefined> {
    const sender = this.getSenderOrFallback(senderUserId);
    if (!sender) return undefined;

    const msg = await sender.api.sendVideo(chatId, new InputFile(media.buffer, media.filename), {
      caption,
      reply_parameters: replyToMsgId ? { message_id: replyToMsgId } : undefined,
    });
    return msg.message_id;
  }

  async sendDocument(
    chatId: number,
    media: MediaInfo,
    caption: string | undefined,
    senderUserId?: number,
    replyToMsgId?: number,
  ): Promise<number | undefined> {
    const sender = this.getSenderOrFallback(senderUserId);
    if (!sender) return undefined;

    const msg = await sender.api.sendDocument(chatId, new InputFile(media.buffer, media.filename), {
      caption,
      reply_parameters: replyToMsgId ? { message_id: replyToMsgId } : undefined,
    });
    return msg.message_id;
  }

  async sendAudio(
    chatId: number,
    media: MediaInfo,
    caption: string | undefined,
    senderUserId?: number,
    replyToMsgId?: number,
  ): Promise<number | undefined> {
    const sender = this.getSenderOrFallback(senderUserId);
    if (!sender) return undefined;

    const msg = await sender.api.sendAudio(chatId, new InputFile(media.buffer, media.filename), {
      caption,
      reply_parameters: replyToMsgId ? { message_id: replyToMsgId } : undefined,
    });
    return msg.message_id;
  }

  async editMessage(
    chatId: number,
    messageId: number,
    text: string,
    senderUserId?: number,
  ): Promise<void> {
    const sender = this.getSenderOrFallback(senderUserId);
    if (!sender) return;

    await sender.api.editMessageText(chatId, messageId, text);
  }

  async deleteMessage(
    chatId: number,
    messageId: number,
    senderUserId?: number,
  ): Promise<void> {
    const sender = this.getSenderOrFallback(senderUserId);
    if (!sender) return;

    await sender.api.deleteMessage(chatId, messageId);
  }
}
