<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { getIndexStatus, listPages, searchDocument, startIndexing, thumbnailUrl } from '$lib/api';
  import type { DrawingPage, IndexStatus, SearchResponse, SearchResult } from '$lib/api';
  import PageViewer from './PageViewer.svelte';

  export let documentId: string;

  const EXAMPLES = ['RTU-1', 'ceiling height', 'go to 1.3', '"recessed fixture"'];

  let status: IndexStatus | null = null;
  let pages: DrawingPage[] = [];
  let query = '';
  let response: SearchResponse | null = null;
  let searching = false;
  let error = '';
  let loading = true;
  let timer: ReturnType<typeof setInterval> | null = null;
  let debounce: ReturnType<typeof setTimeout> | null = null;
  let seq = 0;
  let viewer: { page: number; query: string; matchPages: number[] } | null = null;

  $: ready = status?.status === 'ready';
  $: progress = status?.total_pages ? Math.round((status.pages_indexed / status.total_pages) * 100) : 0;
  $: results = response?.results ?? [];

  async function refresh() {
    status = await getIndexStatus(documentId);
    if (status.status === 'ready') {
      stopPolling();
      pages = await listPages(documentId);
    } else if (status.status === 'indexing') {
      pages = await listPages(documentId).catch(() => pages);
    } else {
      stopPolling();
    }
  }

  function startPolling() {
    if (!timer) timer = setInterval(() => refresh().catch((e) => (error = (e as Error).message)), 2000);
  }

  function stopPolling() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  async function beginIndexing() {
    error = '';
    try {
      status = await startIndexing(documentId);
      startPolling();
    } catch (e) {
      error = (e as Error).message;
    }
  }

  onMount(async () => {
    try {
      status = await getIndexStatus(documentId);
      if (status.status === 'not_indexed') await beginIndexing();
      else if (status.status === 'indexing') startPolling();
      else if (status.status === 'ready') pages = await listPages(documentId);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  });

  onDestroy(() => {
    stopPolling();
    if (debounce) clearTimeout(debounce);
  });

  function onInput() {
    if (debounce) clearTimeout(debounce);
    debounce = setTimeout(runSearch, 220);
  }

  async function runSearch() {
    const q = query.trim();
    const mine = ++seq;
    if (!q) {
      response = null;
      searching = false;
      return;
    }
    searching = true;
    try {
      const r = await searchDocument(documentId, q);
      if (mine === seq) {
        response = r;
        error = '';
      }
    } catch (e) {
      if (mine === seq) error = (e as Error).message;
    } finally {
      if (mine === seq) searching = false;
    }
  }

  function setQuery(q: string) {
    query = q;
    runSearch();
  }

  function clear() {
    query = '';
    response = null;
  }

  function openResult(r: SearchResult) {
    // A sheet-number jump has no text to highlight; a text result carries the query.
    const q = r.kind === 'text' ? (response?.query ?? query).trim() : '';
    viewer = { page: r.page_number, query: q, matchPages: results.filter((x) => x.kind === 'text').map((x) => x.page_number) };
  }

  function openSheet(n: number) {
    viewer = { page: n, query: '', matchPages: [] };
  }

  async function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && results.length) {
      if (debounce) { clearTimeout(debounce); await runSearch(); }
      if (results.length) openResult(results[0]);
    }
  }

  /** Split a snippet into plain and matched parts using the server's character spans. */
  function parts(text: string, spans: [number, number][]): { t: string; hit: boolean }[] {
    const out: { t: string; hit: boolean }[] = [];
    let pos = 0;
    for (const [s, e] of [...spans].sort((a, b) => a[0] - b[0])) {
      if (s < pos) continue;
      if (s > pos) out.push({ t: text.slice(pos, s), hit: false });
      out.push({ t: text.slice(s, e), hit: true });
      pos = e;
    }
    if (pos < text.length) out.push({ t: text.slice(pos), hit: false });
    return out;
  }
</script>

{#if loading}
  <div class="card p-10 text-center text-sm text-gray-500">Loading…</div>
{:else}
  <div class="flex flex-col gap-6">
    {#if status?.status === 'indexing' || status?.status === 'not_indexed'}
      <div class="rounded-lg border border-blue-200 bg-blue-50 p-4">
        <p class="text-sm font-medium text-blue-900">
          Reading the drawings… {status?.pages_indexed ?? 0}{status?.total_pages ? ` / ${status.total_pages}` : ''} pages
        </p>
        <div class="mt-2 h-2 w-full rounded-full bg-blue-100">
          <div class="h-2 rounded-full bg-blue-500 transition-all" style="width: {progress}%"></div>
        </div>
        <p class="mt-2 text-xs text-blue-700">Search works on the sheets read so far, and on all of them when this finishes.</p>
      </div>
    {:else if status?.status === 'failed'}
      <div class="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        <p class="font-medium">Indexing failed.</p>
        <p class="mt-1 break-words text-xs">{status.error}</p>
        <button class="btn-primary mt-3 text-xs" on:click={beginIndexing}>Retry indexing</button>
      </div>
    {/if}

    <!-- Search -->
    <section class="card p-4">
      <label for="drawing-search" class="text-sm font-bold text-gray-800">Search the drawings</label>
      <div class="mt-2 flex gap-2">
        <input
          id="drawing-search"
          type="search"
          bind:value={query}
          on:input={onInput}
          on:keydown={onKeydown}
          disabled={pages.length === 0}
          placeholder={pages.length === 0 ? 'Available once indexing starts' : 'A tag, a note, a size, or a sheet number, e.g. RTU-1, A.F.F., 10\'-8", go to 1.3'}
          class="flex-1 rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50"
          autocomplete="off"
        />
        {#if query}
          <button class="btn-secondary text-xs" on:click={clear}>Clear</button>
        {/if}
      </div>
      {#if !query && pages.length > 0}
        <div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-500">
          Try
          {#each EXAMPLES as ex}
            <button class="rounded-full border border-gray-200 px-2.5 py-1 hover:border-blue-400 hover:text-blue-700" on:click={() => setQuery(ex)}>{ex}</button>
          {/each}
        </div>
      {/if}
      {#if error}
        <p class="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>
      {/if}
    </section>

    {#if query.trim() && response}
      <section aria-live="polite" data-testid="search-results">
        {#if response.mode === 'none'}
          <div class="card p-6 text-center">
            <p class="text-sm font-medium text-gray-700">No sheet contains “{response.query}”.</p>
            {#if response.suggestion}
              <p class="mt-2 text-sm text-gray-500">
                Did you mean
                <button class="font-semibold text-blue-600 underline underline-offset-2" on:click={() => response && response.suggestion && setQuery(response.suggestion)}>{response.suggestion}</button>?
              </p>
            {/if}
            <p class="mt-2 text-xs text-gray-400">Search matches the text on the sheets. A symbol or note drawn as a picture is not searchable.</p>
          </div>
        {:else}
          <p class="mb-2 text-xs text-gray-500">
            {results.length} sheet{results.length === 1 ? '' : 's'} of {response.total_pages}
            {#if response.mode === 'partial'}
              <span class="ml-1 rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800">No sheet has every word; showing sheets with some of them</span>
            {/if}
            <span class="text-gray-400"> · Enter opens the first</span>
          </p>
          <ul class="flex flex-col gap-2">
            {#each results as r (r.page_number)}
              <li>
                <button class="card w-full p-3 text-left hover:border-blue-300" on:click={() => openResult(r)}>
                  <div class="flex flex-wrap items-baseline gap-x-2">
                    <span class="font-mono text-sm font-bold text-gray-900">{r.label}</span>
                    {#if r.sheet_title}<span class="text-sm text-gray-600">{r.sheet_title}</span>{/if}
                    <span class="ml-auto text-xs text-gray-400">
                      {#if r.kind === 'sheet'}Go to this sheet
                      {:else if r.kind === 'page'}PDF page {r.page_number}
                      {:else}{r.match_count} match{r.match_count === 1 ? '' : 'es'}{r.title_match ? ' · in title' : ''}{/if}
                    </span>
                  </div>
                  {#each r.snippets as sn}
                    <p class="mt-1 break-words text-xs text-gray-600">
                      {#each parts(sn.text, sn.spans) as p}{#if p.hit}<mark class="rounded bg-yellow-200 px-0.5">{p.t}</mark>{:else}{p.t}{/if}{/each}
                    </p>
                  {/each}
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    {:else if searching}
      <p class="text-sm text-gray-400">Searching…</p>
    {/if}

    <!-- Sheet grid -->
    {#if !query.trim() && pages.length > 0}
      <section>
        <h2 class="mb-3 text-sm font-bold text-gray-800">All sheets ({pages.length})</h2>
        <ul class="grid gap-3" style="grid-template-columns: repeat(auto-fill, minmax(170px, 1fr))" data-testid="sheet-grid">
          {#each pages as p (p.page_number)}
            <li>
              <button class="card block w-full overflow-hidden text-left hover:border-blue-400" on:click={() => openSheet(p.page_number)}>
                <div class="aspect-[3/2] bg-gray-100">
                  <img
                    src={thumbnailUrl(documentId, p.page_number)}
                    alt={`Sheet ${p.label}`}
                    loading="lazy"
                    class="h-full w-full object-contain"
                  />
                </div>
                <div class="px-2 py-1.5">
                  <p class="font-mono text-xs font-bold text-gray-900">{p.label}</p>
                  <p class="truncate text-[11px] text-gray-500">{p.sheet_title ?? ''}</p>
                  {#if p.text_source === 'none'}<p class="text-[10px] text-amber-600">no searchable text</p>{/if}
                </div>
              </button>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  </div>
{/if}

{#if viewer}
  <PageViewer
    {documentId}
    {pages}
    pageNumber={viewer.page}
    query={viewer.query}
    matchPages={viewer.matchPages}
    on:close={() => (viewer = null)}
    on:navigate={(e) => (viewer = viewer ? { ...viewer, page: e.detail } : null)}
  />
{/if}
