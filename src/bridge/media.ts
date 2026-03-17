// Media helper utilities
// Currently media download is handled directly in listener modules.
// This module provides shared constants and utility functions.

export const TG_MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024; // 20MB Telegram Bot API limit
export const TG_MAX_UPLOAD_SIZE = 50 * 1024 * 1024; // 50MB Telegram upload limit
export const MAX_MAX_UPLOAD_SIZE = 4 * 1024 * 1024 * 1024; // 4GB MAX limit

export function isWithinTgDownloadLimit(size: number): boolean {
  return size <= TG_MAX_DOWNLOAD_SIZE;
}

export function isWithinTgUploadLimit(size: number): boolean {
  return size <= TG_MAX_UPLOAD_SIZE;
}
