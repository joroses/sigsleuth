// SIGSLEUTH_FIXED_NOW at build time stops the page's clock, as it stops the
// API's, for a demo of a saved database.
const fixedNow = Date.parse(import.meta.env.SIGSLEUTH_FIXED_NOW ?? "");

/** The current time in milliseconds since the Unix epoch. */
export function now(): number {
  return Number.isNaN(fixedNow) ? Date.now() : fixedNow;
}
