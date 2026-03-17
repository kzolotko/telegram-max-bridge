// Text formatting conversion between Telegram and MAX.
// Both support Markdown and HTML, so for v1 we pass text as-is.
// This module is a placeholder for future formatting conversion if needed.

export function formatForMax(text: string): string {
  return text;
}

export function formatForTelegram(text: string): string {
  return text;
}

export function prependSenderName(name: string, text: string): string {
  return `[${name}]: ${text}`;
}
