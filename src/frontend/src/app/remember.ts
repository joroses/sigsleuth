import { useCallback, useEffect, useRef, useState } from "react";

/*
 * Two ways a page remembers what the viewer chose, both per browser and best
 * effort (storage may be unavailable, and then the choice just applies):
 *
 * - A setting (useSetting) is kept in localStorage: filters, sorts and
 *   windows, still chosen when the page is opened again from the menu.
 * - A view (useViewState) is kept in the browser history entry: the search
 *   typed, the page of a table, the selected or expanded thing, the map's
 *   camera and the scroll position. Back and Forward bring it back; following
 *   a link opens a fresh entry, so a new visit starts with nothing selected.
 *
 * A shared link may set a setting for the entry it opens (useLinkedSetting).
 */

const SETTING_PREFIX = "sigsleuth.";

/**
 * A choice kept in localStorage under `sigsleuth.<key>`. A saved value
 * `valid` rejects (by default, one of another type than `initial`) is ignored.
 * When `initial` is an object, the saved one is laid over it field by field
 * (see fillFields), so a setting that gained a field keeps the others.
 */
export function useSetting<T>(
  key: string,
  initial: T,
  valid: (value: unknown) => boolean = (value) =>
    typeof value === typeof initial,
) {
  const [value, setValue] = useState<T>(() =>
    savedSetting(key, initial, valid),
  );
  useEffect(() => saveSetting(key, value), [key, value]);
  return [value, setValue] as const;
}

/**
 * A setting a link may set for the history entry it opens (`linked`, when
 * given): shown there, and again on Back, but not kept. A choice made after
 * replaces it and is kept as useSetting keeps one; nothing is kept before.
 */
export function useLinkedSetting<T>(
  key: string,
  initial: T,
  linked: T | undefined,
  valid?: (value: unknown) => boolean,
) {
  const [saved] = useState<T>(() => savedSetting(key, initial, valid));
  const [fromLink, setFromLink] = useViewState<{ value: T } | null>(
    `linked.${key}`,
    linked === undefined ? null : { value: linked },
  );
  const [chosen, setChosen] = useState<{ value: T } | null>(null);
  useEffect(() => {
    if (chosen !== null) saveSetting(key, chosen.value);
  }, [key, chosen]);
  const choose = useCallback(
    (next: T) => {
      setFromLink(null);
      setChosen({ value: next });
    },
    [setFromLink],
  );
  const value =
    chosen !== null ? chosen.value : fromLink !== null ? fromLink.value : saved;
  return [value, choose] as const;
}

/** What useSetting starts with: the kept value, else `initial`. */
export function savedSetting<T>(
  key: string,
  initial: T,
  valid: (value: unknown) => boolean = (value) =>
    typeof value === typeof initial,
): T {
  const saved = fillFields(initial, loadSetting(key));
  return saved !== undefined && valid(saved) ? (saved as T) : initial;
}

/**
 * `saved` laid over an object `initial`: each field of `initial` takes the
 * saved field of the same type (any type when the default is null), and
 * other saved fields are dropped. Not an object when `initial` is one: undefined.
 */
function fillFields(initial: unknown, saved: unknown): unknown {
  if (!isRecord(initial)) return saved;
  if (!isRecord(saved)) return undefined;
  return Object.fromEntries(
    Object.entries(initial).map(([field, fallback]) => {
      const kept = fillFields(fallback, saved[field]);
      return [
        field,
        kept !== undefined &&
        (fallback === null || typeof kept === typeof fallback)
          ? kept
          : fallback,
      ];
    }),
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function saveSetting(key: string, value: unknown): void {
  try {
    localStorage.setItem(SETTING_PREFIX + key, JSON.stringify(value));
  } catch {
    // Not remembered; the choice still applies.
  }
}

/** The setting kept under `key`, or undefined; see useSetting. */
export function loadSetting(key: string): unknown {
  try {
    const saved = localStorage.getItem(SETTING_PREFIX + key);
    return saved === null ? undefined : JSON.parse(saved);
  } catch {
    return undefined;
  }
}

/**
 * A view kept in this history entry under `key`; see the top of this file.
 * Like useState, `initial` may be a function, called only for a new entry.
 */
export function useViewState<T>(key: string, initial: T | (() => T)) {
  const [value, setValue] = useState<T>(() => {
    const saved = readView(key);
    if (saved !== undefined) return saved as T;
    return initial instanceof Function ? initial() : initial;
  });
  useEffect(() => writeView(key, value), [key, value]);
  return [value, setValue] as const;
}

/** What this history entry kept under `key`, if anything. */
export function readView(key: string): unknown {
  const state: unknown = window.history.state;
  return typeof state === "object" && state !== null
    ? (state as Record<string, unknown>)[key]
    : undefined;
}

/** Keeps `value` in this history entry, beside what else it keeps. */
export function writeView(key: string, value: unknown): void {
  const state: unknown = window.history.state;
  try {
    window.history.replaceState(
      {
        ...(typeof state === "object" && state !== null ? state : {}),
        [key]: value,
      },
      "",
    );
  } catch {
    // Not remembered; Back opens the page as new.
  }
}

const SCROLL_KEY = "scrollTop";
// How long a page may take to load enough to scroll back to where it was.
const SCROLL_RESTORE_MS = 4000;

/**
 * Keeps how far the element given the returned ref is scrolled in each history entry, and scrolls back
 * there when an entry is shown again. Pages load their data after they
 * appear, so it keeps trying as the content grows, until it gets there, the
 * viewer scrolls, or a few seconds pass. `entry` changes with each page shown.
 */
export function useRememberedScroll(entry: string) {
  const element = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const target = element.current;
    if (target === null) return;
    const wanted = readView(SCROLL_KEY);
    let restoring = typeof wanted === "number" && wanted > 0;
    let timer = 0;
    const stopRestoring = () => {
      restoring = false;
    };
    const restore = () => {
      if (!restoring || typeof wanted !== "number") return;
      target.scrollTop = wanted;
      if (Math.abs(target.scrollTop - wanted) < 2) restoring = false;
    };
    // Older browsers (and tests) without ResizeObserver restore only when
    // the page is swapped in.
    const resize =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(restore);
    const watchChildren = () => {
      for (const child of Array.from(target.children)) resize?.observe(child);
    };
    watchChildren();
    // Child pages are swapped in by React; watch new ones too.
    const children = new MutationObserver(() => {
      watchChildren();
      restore();
    });
    children.observe(target, { childList: true });
    if (!restoring) target.scrollTop = 0;
    restore();
    const giveUp = window.setTimeout(stopRestoring, SCROLL_RESTORE_MS);
    const saveNow = () => {
      window.clearTimeout(timer);
      if (!restoring) writeView(SCROLL_KEY, target.scrollTop);
    };
    // Browsers limit how often the history entry may change, so scrolling
    // saves once it pauses, and a click (which may follow a link) saves at once.
    const save = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(saveNow, 150);
    };
    target.addEventListener("scroll", save);
    window.addEventListener("click", saveNow, true);
    // The viewer scrolling takes over from restoring.
    target.addEventListener("wheel", stopRestoring);
    target.addEventListener("touchmove", stopRestoring);
    target.addEventListener("keydown", stopRestoring);
    return () => {
      resize?.disconnect();
      children.disconnect();
      window.clearTimeout(giveUp);
      window.clearTimeout(timer);
      target.removeEventListener("scroll", save);
      window.removeEventListener("click", saveNow, true);
      target.removeEventListener("wheel", stopRestoring);
      target.removeEventListener("touchmove", stopRestoring);
      target.removeEventListener("keydown", stopRestoring);
    };
  }, [entry]);
  return element;
}
