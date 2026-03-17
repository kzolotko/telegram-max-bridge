import { Bot as MaxBot, Context } from '@maxhub/max-bot-api';
import { EchoGuard } from '../bridge/echo-guard';
import { ConfigLookup } from '../config';
import { BridgeEvent, MediaInfo } from '../types';

export class MaxListener {
  private bot: MaxBot;

  constructor(
    token: string,
    private lookup: ConfigLookup,
    private echoGuard: EchoGuard,
    private onEvent: (event: BridgeEvent) => Promise<void>,
    apiBaseUrl?: string,
  ) {
    this.bot = new MaxBot(token, apiBaseUrl ? {
      clientOptions: { baseUrl: apiBaseUrl },
    } : undefined);
    this.setupHandlers();
  }

  private setupHandlers(): void {
    this.bot.on('message_created', async (ctx) => {
      try {
        await this.handleMessage(ctx);
      } catch (err) {
        console.error('[MAX Listener] Error handling message:', err);
      }
    });

    this.bot.on('message_edited', async (ctx) => {
      try {
        await this.handleEditedMessage(ctx);
      } catch (err) {
        console.error('[MAX Listener] Error handling edited message:', err);
      }
    });

    this.bot.on('message_removed', async (ctx) => {
      try {
        await this.handleRemovedMessage(ctx);
      } catch (err) {
        console.error('[MAX Listener] Error handling removed message:', err);
      }
    });
  }

  private async handleMessage(ctx: Context): Promise<void> {
    const message = ctx.message;
    if (!message) return;

    const senderId = message.sender?.user_id;
    if (!senderId || this.echoGuard.isBot(senderId)) return;

    const chatId = ctx.chatId as number | undefined;
    if (!chatId) return;

    const chatPair = this.lookup.getChatPairByMaxChat(chatId);
    if (!chatPair) return;

    const user = this.lookup.getUserByMaxId(senderId);
    const senderDisplayName = message.sender?.name || 'Unknown';
    const msgId = message.body.mid;

    // Check for reply
    let replyToSourceMsgId: string | undefined;
    if (message.link?.type === 'reply') {
      replyToSourceMsgId = message.link.message.mid;
    }

    // Check attachments
    const attachments = message.body.attachments || [];
    const text = message.body.text || undefined;

    for (const att of attachments) {
      if (att.type === 'image') {
        const media = await this.downloadMedia(att.payload.url, att.payload.token);
        await this.onEvent({
          direction: 'max-to-tg',
          chatPair,
          user: user || null,
          senderDisplayName,
          type: 'photo',
          text,
          media,
          replyToSourceMsgId,
          sourceMsgId: msgId,
        });
        return; // one event per message
      }

      if (att.type === 'video') {
        const media = await this.downloadMedia(att.payload.url, att.payload.token);
        await this.onEvent({
          direction: 'max-to-tg',
          chatPair,
          user: user || null,
          senderDisplayName,
          type: 'video',
          text,
          media,
          replyToSourceMsgId,
          sourceMsgId: msgId,
        });
        return;
      }

      if (att.type === 'file') {
        const media = await this.downloadMedia(att.payload.url, att.payload.token);
        if (media) {
          media.filename = att.filename || media.filename;
        }
        await this.onEvent({
          direction: 'max-to-tg',
          chatPair,
          user: user || null,
          senderDisplayName,
          type: 'file',
          text,
          media,
          replyToSourceMsgId,
          sourceMsgId: msgId,
        });
        return;
      }

      if (att.type === 'audio') {
        const media = await this.downloadMedia(att.payload.url, att.payload.token);
        await this.onEvent({
          direction: 'max-to-tg',
          chatPair,
          user: user || null,
          senderDisplayName,
          type: 'audio',
          text,
          media,
          replyToSourceMsgId,
          sourceMsgId: msgId,
        });
        return;
      }

      if (att.type === 'sticker') {
        await this.onEvent({
          direction: 'max-to-tg',
          chatPair,
          user: user || null,
          senderDisplayName,
          type: 'sticker',
          text: `[Sticker]`,
          replyToSourceMsgId,
          sourceMsgId: msgId,
        });
        return;
      }
    }

    // Text-only message
    if (text) {
      await this.onEvent({
        direction: 'max-to-tg',
        chatPair,
        user: user || null,
        senderDisplayName,
        type: 'text',
        text,
        replyToSourceMsgId,
        sourceMsgId: msgId,
      });
    }
  }

  private async handleEditedMessage(ctx: Context): Promise<void> {
    const message = ctx.message;
    if (!message) return;

    const senderId = message.sender?.user_id;
    if (!senderId || this.echoGuard.isBot(senderId)) return;

    const chatId = ctx.chatId as number | undefined;
    if (!chatId) return;

    const chatPair = this.lookup.getChatPairByMaxChat(chatId);
    if (!chatPair) return;

    const user = this.lookup.getUserByMaxId(senderId);
    const senderDisplayName = message.sender?.name || 'Unknown';

    await this.onEvent({
      direction: 'max-to-tg',
      chatPair,
      user: user || null,
      senderDisplayName,
      type: 'edit',
      text: message.body.text || undefined,
      editSourceMsgId: message.body.mid,
      sourceMsgId: message.body.mid,
    });
  }

  private async handleRemovedMessage(ctx: Context): Promise<void> {
    // message_removed update has message_id in the update object
    const update = ctx.update as any;
    const messageId: string | undefined = update.message_id || ctx.messageId;
    if (!messageId) return;

    const chatId = ctx.chatId as number | undefined;
    if (!chatId) return;

    const chatPair = this.lookup.getChatPairByMaxChat(chatId);
    if (!chatPair) return;

    await this.onEvent({
      direction: 'max-to-tg',
      chatPair,
      user: null,
      senderDisplayName: 'Unknown',
      type: 'delete',
      deleteSourceMsgId: messageId,
      sourceMsgId: messageId,
    });
  }

  private async downloadMedia(url: string, token: string): Promise<MediaInfo | undefined> {
    try {
      const response = await fetch(url, {
        headers: { token },
      });
      if (!response.ok) return undefined;

      const buffer = Buffer.from(await response.arrayBuffer());
      const filename = url.split('/').pop() || 'file';
      const mimeType = response.headers.get('content-type') || 'application/octet-stream';

      return { buffer, filename, mimeType };
    } catch (err) {
      console.error('[MAX Listener] Failed to download media:', err);
      return undefined;
    }
  }

  async start(): Promise<number> {
    const info = await this.bot.api.getMyInfo();
    console.log(`[MAX Listener] Starting as @${info.username} (ID: ${info.user_id})`);
    await this.bot.start();
    return info.user_id;
  }

  stop(): void {
    this.bot.stop();
  }
}
