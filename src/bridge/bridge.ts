import { ConfigLookup } from '../config';
import { MessageStore } from '../message-store';
import { TelegramSenderPool } from '../telegram/sender';
import { MaxSenderPool } from '../max/sender';
import { BridgeEvent } from '../types';
import { prependSenderName } from './formatting';
import { isWithinTgUploadLimit } from './media';

export class Bridge {
  constructor(
    private lookup: ConfigLookup,
    private messageStore: MessageStore,
    private tgSenders: TelegramSenderPool,
    private maxSenders: MaxSenderPool,
  ) {}

  async handleEvent(event: BridgeEvent): Promise<void> {
    try {
      if (event.direction === 'tg-to-max') {
        await this.handleTgToMax(event);
      } else {
        await this.handleMaxToTg(event);
      }
    } catch (err) {
      console.error(`[Bridge] Error handling ${event.direction} ${event.type}:`, err);
    }
  }

  private async handleTgToMax(event: BridgeEvent): Promise<void> {
    const { chatPair, user, senderDisplayName, type } = event;
    const maxChatId = chatPair.maxChatId;
    const senderMaxUserId = user?.maxUserId;
    const config = this.lookup.getConfig();

    // Prepare text with sender prefix if no user mapping (fallback)
    const shouldPrefix = !user && config.fallback?.messagePrefix;

    // Resolve reply target
    let replyToMaxMsgId: string | undefined;
    if (event.replyToSourceMsgId) {
      replyToMaxMsgId = this.messageStore.getMaxMsgId(
        chatPair.name,
        event.replyToSourceMsgId as number,
      );
    }

    switch (type) {
      case 'text': {
        const text = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text || '';
        const maxMsgId = await this.maxSenders.sendText(
          maxChatId, text, senderMaxUserId, replyToMaxMsgId,
        );
        if (maxMsgId) {
          this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
        }
        break;
      }

      case 'photo': {
        const caption = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text;
        if (event.media) {
          const maxMsgId = await this.maxSenders.sendPhoto(
            maxChatId, event.media, caption, senderMaxUserId, replyToMaxMsgId,
          );
          if (maxMsgId) {
            this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
          }
        } else {
          // Media download failed, send text only
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, `${event.text || ''} [Photo could not be transferred]`)
            : `${event.text || ''} [Photo could not be transferred]`;
          const maxMsgId = await this.maxSenders.sendText(maxChatId, text, senderMaxUserId, replyToMaxMsgId);
          if (maxMsgId) {
            this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
          }
        }
        break;
      }

      case 'video': {
        const caption = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text;
        if (event.media) {
          const maxMsgId = await this.maxSenders.sendVideo(
            maxChatId, event.media, caption, senderMaxUserId, replyToMaxMsgId,
          );
          if (maxMsgId) {
            this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
          }
        } else {
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, `${event.text || ''} [Video could not be transferred]`)
            : `${event.text || ''} [Video could not be transferred]`;
          const maxMsgId = await this.maxSenders.sendText(maxChatId, text, senderMaxUserId, replyToMaxMsgId);
          if (maxMsgId) {
            this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
          }
        }
        break;
      }

      case 'file': {
        const caption = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text;
        if (event.media) {
          const maxMsgId = await this.maxSenders.sendDocument(
            maxChatId, event.media, caption, senderMaxUserId, replyToMaxMsgId,
          );
          if (maxMsgId) {
            this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
          }
        } else {
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, `${event.text || ''} [File could not be transferred]`)
            : `${event.text || ''} [File could not be transferred]`;
          const maxMsgId = await this.maxSenders.sendText(maxChatId, text, senderMaxUserId, replyToMaxMsgId);
          if (maxMsgId) {
            this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
          }
        }
        break;
      }

      case 'audio': {
        const caption = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text;
        if (event.media) {
          const maxMsgId = await this.maxSenders.sendAudio(
            maxChatId, event.media, caption, senderMaxUserId, replyToMaxMsgId,
          );
          if (maxMsgId) {
            this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
          }
        } else {
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, `${event.text || ''} [Audio could not be transferred]`)
            : `${event.text || ''} [Audio could not be transferred]`;
          const maxMsgId = await this.maxSenders.sendText(maxChatId, text, senderMaxUserId, replyToMaxMsgId);
          if (maxMsgId) {
            this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
          }
        }
        break;
      }

      case 'sticker': {
        const text = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '[Sticker]')
          : event.text || '[Sticker]';
        const maxMsgId = await this.maxSenders.sendText(maxChatId, text, senderMaxUserId, replyToMaxMsgId);
        if (maxMsgId) {
          this.messageStore.store(chatPair.name, event.sourceMsgId as number, maxMsgId);
        }
        break;
      }

      case 'edit': {
        if (!event.editSourceMsgId) break;
        const maxMsgId = this.messageStore.getMaxMsgId(chatPair.name, event.editSourceMsgId as number);
        if (maxMsgId) {
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, event.text || '')
            : event.text || '';
          await this.maxSenders.editMessage(maxMsgId, text, senderMaxUserId);
        }
        break;
      }

      // Delete from TG is not supported (Bot API doesn't send delete events)
    }
  }

  private async handleMaxToTg(event: BridgeEvent): Promise<void> {
    const { chatPair, user, senderDisplayName, type } = event;
    const tgChatId = chatPair.telegramChatId;
    const senderTgUserId = user?.telegramUserId;
    const config = this.lookup.getConfig();

    const shouldPrefix = !user && config.fallback?.messagePrefix;

    // Resolve reply target
    let replyToTgMsgId: number | undefined;
    if (event.replyToSourceMsgId) {
      replyToTgMsgId = this.messageStore.getTgMsgId(
        chatPair.name,
        event.replyToSourceMsgId as string,
      );
    }

    switch (type) {
      case 'text': {
        const text = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text || '';
        const tgMsgId = await this.tgSenders.sendText(
          tgChatId, text, senderTgUserId, replyToTgMsgId,
        );
        if (tgMsgId) {
          this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
        }
        break;
      }

      case 'photo': {
        const caption = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text;
        if (event.media && isWithinTgUploadLimit(event.media.buffer.length)) {
          const tgMsgId = await this.tgSenders.sendPhoto(
            tgChatId, event.media, caption, senderTgUserId, replyToTgMsgId,
          );
          if (tgMsgId) {
            this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
          }
        } else {
          const reason = event.media ? 'File too large for Telegram' : 'Photo could not be transferred';
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, `${event.text || ''} [${reason}]`)
            : `${event.text || ''} [${reason}]`;
          const tgMsgId = await this.tgSenders.sendText(tgChatId, text, senderTgUserId, replyToTgMsgId);
          if (tgMsgId) {
            this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
          }
        }
        break;
      }

      case 'video': {
        const caption = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text;
        if (event.media && isWithinTgUploadLimit(event.media.buffer.length)) {
          const tgMsgId = await this.tgSenders.sendVideo(
            tgChatId, event.media, caption, senderTgUserId, replyToTgMsgId,
          );
          if (tgMsgId) {
            this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
          }
        } else {
          const reason = event.media ? 'File too large for Telegram' : 'Video could not be transferred';
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, `${event.text || ''} [${reason}]`)
            : `${event.text || ''} [${reason}]`;
          const tgMsgId = await this.tgSenders.sendText(tgChatId, text, senderTgUserId, replyToTgMsgId);
          if (tgMsgId) {
            this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
          }
        }
        break;
      }

      case 'file': {
        const caption = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text;
        if (event.media && isWithinTgUploadLimit(event.media.buffer.length)) {
          const tgMsgId = await this.tgSenders.sendDocument(
            tgChatId, event.media, caption, senderTgUserId, replyToTgMsgId,
          );
          if (tgMsgId) {
            this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
          }
        } else {
          const reason = event.media ? 'File too large for Telegram' : 'File could not be transferred';
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, `${event.text || ''} [${reason}]`)
            : `${event.text || ''} [${reason}]`;
          const tgMsgId = await this.tgSenders.sendText(tgChatId, text, senderTgUserId, replyToTgMsgId);
          if (tgMsgId) {
            this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
          }
        }
        break;
      }

      case 'audio': {
        const caption = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '')
          : event.text;
        if (event.media && isWithinTgUploadLimit(event.media.buffer.length)) {
          const tgMsgId = await this.tgSenders.sendAudio(
            tgChatId, event.media, caption, senderTgUserId, replyToTgMsgId,
          );
          if (tgMsgId) {
            this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
          }
        } else {
          const reason = event.media ? 'File too large for Telegram' : 'Audio could not be transferred';
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, `${event.text || ''} [${reason}]`)
            : `${event.text || ''} [${reason}]`;
          const tgMsgId = await this.tgSenders.sendText(tgChatId, text, senderTgUserId, replyToTgMsgId);
          if (tgMsgId) {
            this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
          }
        }
        break;
      }

      case 'sticker': {
        const text = shouldPrefix
          ? prependSenderName(senderDisplayName, event.text || '[Sticker]')
          : event.text || '[Sticker]';
        const tgMsgId = await this.tgSenders.sendText(tgChatId, text, senderTgUserId, replyToTgMsgId);
        if (tgMsgId) {
          this.messageStore.store(chatPair.name, tgMsgId, event.sourceMsgId as string);
        }
        break;
      }

      case 'edit': {
        if (!event.editSourceMsgId) break;
        const tgMsgId = this.messageStore.getTgMsgId(chatPair.name, event.editSourceMsgId as string);
        if (tgMsgId) {
          const text = shouldPrefix
            ? prependSenderName(senderDisplayName, event.text || '')
            : event.text || '';
          await this.tgSenders.editMessage(tgChatId, tgMsgId, text, senderTgUserId);
        }
        break;
      }

      case 'delete': {
        if (!event.deleteSourceMsgId) break;
        const tgMsgId = this.messageStore.getTgMsgId(chatPair.name, event.deleteSourceMsgId as string);
        if (tgMsgId) {
          // For delete, we try all senders since we don't know who sent the original
          // The bot that sent the message should be able to delete it
          await this.tgSenders.deleteMessage(tgChatId, tgMsgId, user?.telegramUserId);
        }
        break;
      }
    }
  }
}
