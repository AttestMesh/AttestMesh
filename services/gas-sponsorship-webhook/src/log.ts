/**
 * Tiny level-gated structured logger (spec §4 LOG_LEVEL; §9 logging levels).
 *
 * Workers send `console.*` to the tail/`wrangler tail` stream. We emit one JSON
 * line per event so ops dashboards can parse fields like the policy `reason`.
 */

import type { LogLevel } from "./env.js";

export interface Logger {
  info(event: string, fields?: Record<string, unknown>): void;
  warn(event: string, fields?: Record<string, unknown>): void;
  error(event: string, fields?: Record<string, unknown>): void;
}

const ORDER: Record<LogLevel, number> = { info: 0, warn: 1, error: 2 };

function emit(level: LogLevel, event: string, fields?: Record<string, unknown>): void {
  const line = JSON.stringify({ level, event, ...fields });
  if (level === "error") console.error(line);
  else if (level === "warn") console.warn(line);
  else console.log(line);
}

/** Build a logger that suppresses events below `minLevel`. */
export function createLogger(minLevel: LogLevel): Logger {
  const threshold = ORDER[minLevel];
  const at = (level: LogLevel) =>
    ORDER[level] >= threshold
      ? (event: string, fields?: Record<string, unknown>) => emit(level, event, fields)
      : () => {};
  return { info: at("info"), warn: at("warn"), error: at("error") };
}
