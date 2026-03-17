import { Bot } from 'grammy';
import { Message } from 'grammy/types';
import { EchoGuard } from '../bridge/echo-guard';
import { ConfigLookup } from '../config';
import { BridgeEvent, MediaInfo } from '../types';

export class TelegramListener {
  private bot: Bot;

  constructor(
    token: string,
    private lookup: ConfigLookup,
    private echoGuard: EchoGuard,
    private onEvent: (event: BridgeEvent) => Promise<void>,
  ) {
    this.bot = new Bot(token);
    this.setupHandlers();
  }

  private setupHandlers(): void {
    this.bot.on('message', async (ctx) => {
      try {
        await this.handleMessage(ctx.message);
      } catch (err) {
        console.error('[TG Listener] Error handling message:', err);
      }
    });

    this.bot.on('edited_message', async (ctx) => {
      try {
        await this.handleEditedMessage(ctx.editedMessage);
      } catch (err) {
        console.error('[TG Listener] Error handling edited message:', err);
      }
    });
  }

  private async handleMessage(msg: Message): Promise<void> {
    const senderId = msg.from?.id;
    if (!senderId || this.echoGuard.isBot(senderId)) return;

    const chatPair = this.lookup.getChatPairByTgChat(msg.chat.id);
    if (!chatPair) return;

    const user = this.lookup.getUserByTgId(senderId);
    const senderDisplayName = this.getSenderName(msg);

    // Determine reply
    let replyToSourceMsgId: number | undefined;
    if (msg.reply_to_message) {
      replyToSourceMsgId = msg.reply_to_message.message_id;
    }

    // Determine content type and build event
    if (msg.photo && msg.photo.length > 0) {
      const photo = msg.photo[msg.photo.length - 1]; // largest size
      const media = await this.downloadFile(photo.file_id);
      await this.onEvent({
        direction: 'tg-to-max',
        chatPair,
        user: user || null,
        senderDisplayName,
        type: 'photo',
        text: msg.caption || undefined,
        media,
        replyToSourceMsgId,
        sourceMsgId: msg.message_id,
      });
    } else if (msg.video) {
      const media = await this.downloadFile(msg.video.file_id);
      await this.onEvent({
        direction: 'tg-to-max',
        chatPair,
        user: user || null,
        senderDisplayName,
        type: 'video',
        text: msg.caption || undefined,
        media: media || undefined,
        replyToSourceMsgId,
        sourceMsgId: msg.message_id,
      });
    } else if (msg.document) {
      const media = await this.downloadFile(msg.document.file_id);
      await this.onEvent({
        direction: 'tg-to-max',
        chatPair,
        user: user || null,
        senderDisplayName,
        type: 'file',
        text: msg.caption || undefined,
        media: media
          ? { ...media, filename: msg.document.file_name || media.filename }
          : undefined,
        replyToSourceMsgId,
        sourceMsgId: msg.message_id,
      });
    } else if (msg.audio) {
      const media = await this.downloadFile(msg.audio.file_id);
      await this.onEvent({
        direction: 'tg-to-max',
        chatPair,
        user: user || null,
        senderDisplayName,
        type: 'audio',
        text: msg.caption || undefined,
        media,
        replyToSourceMsgId,
        sourceMsgId: msg.message_id,
      });
    } else if (msg.sticker) {
      await this.onEvent({
        direction: 'tg-to-max',
        chatPair,
        user: user || null,
        senderDisplayName,
        type: 'sticker',
        text: `[Sticker: ${msg.sticker.emoji || ''}]`,
        replyToSourceMsgId,
        sourceMsgId: msg.message_id,
      });
    } else if (msg.text) {
      await this.onEvent({
        direction: 'tg-to-max',
        chatPair,
        user: user || null,
        senderDisplayName,
        type: 'text',
        text: msg.text,
        replyToSourceMsgId,
        sourceMsgId: msg.message_id,
      });
    }
  }

  private async handleEditedMessage(msg: Message): Promise<void> {
    const senderId = msg.from?.id;
    if (!senderId || this.echoGuard.isBot(senderId)) return;

    const chatPair = this.lookup.getChatPairByTgChat(msg.chat.id);
    if (!chatPair) return;

    const user = this.lookup.getUserByTgId(senderId);
    const senderDisplayName = this.getSenderName(msg);

    await this.onEvent({
      direction: 'tg-to-max',
      chatPair,
      user: user || null,
      senderDisplayName,
      type: 'edit',
      text: msg.text || msg.caption || undefined,
      editSourceMsgId: msg.message_id,
      sourceMsgId: msg.message_id,
    });
  }

  private getSenderName(msg: Message): string {
    if (msg.from) {
      const parts = [msg.from.first_name];
      if (msg.from.last_name) parts.push(msg.from.last_name);
      return parts.join(' ');
    }
    return 'Unknown';
  }

  private async downloadFile(fileId: string): Promise<MediaInfo | undefined> {
    try {
      const file = await this.bot.api.getFile(fileId);
      if (!file.file_path) return undefined;

      const url = `https://api.telegram.org/file/bot${this.bot.token}/${file.file_path}`;
      const response = await fetch(url);
      if (!response.ok) return undefined;

      const buffer = Buffer.from(await response.arrayBuffer());
      const filename = file.file_path.split('/').pop() || 'file';
      const mimeType = response.headers.get('content-type') || 'application/octet-stream';

      return { buffer, filename, mimeType };
    } catch (err) {
      console.error('[TG Listener] Failed to download file:', err);
      return undefined;
    }
  }

  async start(): Promise<number> {
    const me = await this.bot.api.getMe();
    console.log(`[TG Listener] Starting as @${me.username} (ID: ${me.id})`);
    this.bot.start();
    return me.id;
  }

  stop(): void {
    this.bot.stop();
  }
}
