import { useEffect, useRef, useState } from "react";

import { request } from "./api";
import { now as clockNow } from "./clock";

export interface ApiState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

// Responses kept for Back and Forward, and for views opened again soon
// (openedAt), newest last. The map keeps up to five for a visit (all-history
// traceroute links are 1.6 MB of JSON on live), the links page one to three.
const KEPT_RESPONSES = 20;
const kept = new Map<string, unknown>();
// Requests startEarly began before their page's code loaded, under the same
// key as kept, until a useApi waiting for one sees it answered.
const started = new Map<string, Promise<unknown>>();
// A view opened again this soon shows what was loaded for it before: links
// and long-link rankings change little in minutes, and they take about a
// second to load on live.
const REOPEN_MS = 10 * 60 * 1000;
const opened = new Map<string, number>();

/**
 * When the view `view` was opened, to pass to useApi as `keptFor`: the last
 * time it was, if under REOPEN_MS ago, so that a new visit shows what was
 * loaded then without loading it again. `fresh` starts it now.
 */
export function openedAt(view: string, fresh = false): number {
  const now = clockNow();
  const last = opened.get(view);
  if (!fresh && last !== undefined && now - last < REOPEN_MS) return last;
  opened.set(view, now);
  return now;
}

/** Forgets every kept response and opened view, as a page reload does. */
export function forgetKept(): void {
  kept.clear();
  started.clear();
  opened.clear();
}

/**
 * Starts loading what a page will ask useApi for with the same `url` and
 * `keptFor`, before the page's code has loaded; useApi then waits for this
 * request instead of making its own. It is never aborted.
 */
export function startEarly(url: string, keptFor: number): void {
  const key = `${keptFor} ${url}`;
  if (kept.has(key) || started.has(key)) return;
  const answer = request(url, new AbortController().signal).then((data) => {
    keep(key, data);
    return data;
  });
  started.set(key, answer);
  // A failed one is retried by useApi.
  answer.catch(() => started.delete(key));
}

/**
 * Loads a URL when it changes and aborts on change or unmount. Null loads nothing.
 *
 * `keptFor`, when given, is when the page's view was opened, kept in its
 * history entry: the response is kept under it and the URL, so Back and
 * Forward show the view again without loading it, while a new visit, opened
 * at another time, loads afresh.
 */
export function useApi<T>(url: string | null, keptFor?: number): ApiState<T> {
  const [result, setResult] = useState<{
    url: string;
    data: T | null;
    error: string | null;
  } | null>(null);
  const key =
    url === null || keptFor === undefined ? null : `${keptFor} ${url}`;
  // A kept response shows at once, and is held here so that it stays shown
  // once newer ones push it out.
  if (url !== null && key !== null && result?.url !== url && kept.has(key)) {
    setResult({ url, data: kept.get(key) as T, error: null });
  }

  useEffect(() => {
    if (url === null) return;
    // An early request may have been answered since this render; waiting
    // for it still shows its answer.
    const early = key === null ? undefined : started.get(key);
    if (early === undefined && key !== null && kept.has(key)) {
      keep(key, kept.get(key));
      return;
    }
    const controller = new AbortController();
    (
      (early as Promise<T> | undefined) ?? request<T>(url, controller.signal)
    ).then(
      (data) => {
        if (early !== undefined && key !== null) started.delete(key);
        if (controller.signal.aborted) return;
        if (key !== null) keep(key, data);
        setResult({ url, data, error: null });
      },
      (error: unknown) => {
        if (!controller.signal.aborted) {
          setResult({ url, data: null, error: errorMessage(error) });
        }
      },
    );
    return () => controller.abort();
  }, [url, key]);

  // A result for an earlier URL is stale; show loading until the new one arrives.
  const current = result?.url === url ? result : null;
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: url !== null && current === null,
  };
}

export interface PagedApiState<P> extends ApiState<P> {
  loadingMore: boolean;
  /** A failed "load more"; the pages already shown stay. */
  loadMoreError: string | null;
  loadMore: () => void;
}

/**
 * useApi for a list that pages with `before_id`. `combine` appends the next
 * page's rows to the pages already loaded.
 */
export function usePagedApi<P extends { next_before_id: number | null }>(
  url: string | null,
  combine: (loaded: P, next: P) => P,
): PagedApiState<P> {
  const first = useApi<P>(url);
  // Later pages, valid only while the first page they extend is still shown.
  const [more, setMore] = useState<{
    first: P;
    data: P;
    error: string | null;
    loading: boolean;
  } | null>(null);
  const loadMoreRequest = useRef<AbortController | null>(null);
  useEffect(() => () => loadMoreRequest.current?.abort(), [url]);

  const extra = more !== null && more.first === first.data ? more : null;
  const data = extra?.data ?? first.data;

  function loadMore() {
    const base = first.data;
    if (url === null || base === null || data === null || extra?.loading) {
      return;
    }
    if (data.next_before_id === null) return;
    loadMoreRequest.current?.abort();
    const controller = new AbortController();
    loadMoreRequest.current = controller;
    setMore({ first: base, data, error: null, loading: true });
    request<P>(withBeforeId(url, data.next_before_id), controller.signal).then(
      (next) =>
        setMore({
          first: base,
          data: combine(data, next),
          error: null,
          loading: false,
        }),
      (error: unknown) => {
        if (!controller.signal.aborted) {
          setMore({
            first: base,
            data,
            error: errorMessage(error),
            loading: false,
          });
        }
      },
    );
  }

  return {
    ...first,
    data,
    loadingMore: extra?.loading ?? false,
    loadMoreError: extra?.error ?? null,
    loadMore,
  };
}

/** The URL of the page before beforeId in a list that pages with `before_id`. */
export function withBeforeId(url: string, beforeId: number): string {
  const next = new URL(url, window.location.origin);
  next.searchParams.set("before_id", String(beforeId));
  return next.pathname + next.search;
}

/** Keeps a response as the newest, dropping the oldest past the limit. */
function keep(key: string, data: unknown): void {
  kept.delete(key);
  kept.set(key, data);
  for (const oldest of kept.keys()) {
    if (kept.size <= KEPT_RESPONSES) break;
    kept.delete(oldest);
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "SigSleuth could not load this history.";
}
