<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import {
    addCountToTakeoff, askQuestion, clearMessages, getIndexStatus, getMessages, listPages, startIndexing,
  } from '$lib/api';
  import type {
    AnswerSource, ChatMessage, CountMarker, CountResult, CountStatus, DrawingPage, Evidence, IndexStatus,
  } from '$lib/api';
  import PageViewer from './PageViewer.svelte';

  export let documentId: string;

  const EXAMPLES = [
    'Which page contains the lighting schedule?',
    'What model is RTU-1?',
    'How many RTUs?',
    'How many 2x4 lights?',
  ];

  let status: IndexStatus | null = null;
  let pages: DrawingPage[] = [];
  let messages: ChatMessage[] = [];
  let input = '';
  let asking = false;
  let error = '';
  let loading = true;
  let showIndex = false;
  let viewer: { page: number; evidence: Evidence[]; markers: CountMarker[] } | null = null;
  let scroller: HTMLDivElement;
  let timer: ReturnType<typeof setInterval> | null = null;

  $: ready = status?.status === 'ready';
  $: progress =
    status?.total_pages ? Math.round((status.pages_indexed / status.total_pages) * 100) : 0;

  async function loadReady() {
    [pages, messages] = await Promise.all([listPages(documentId), getMessages(documentId)]);
    await scrollDown();
  }

  async function refreshStatus() {
    status = await getIndexStatus(documentId);
    if (status.status === 'indexing') {
      // Pages appear as they are indexed, so the sheet list fills in live.
      pages = await listPages(documentId).catch(() => pages);
    } else {
      stopPolling();
      if (status.status === 'ready') await loadReady();
    }
  }

  function startPolling() {
    if (timer) return;
    timer = setInterval(() => refreshStatus().catch((e) => (error = (e as Error).message)), 2000);
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
      if (status.status === 'not_indexed') {
        await beginIndexing();
      } else if (status.status === 'indexing') {
        startPolling();
      } else if (status.status === 'ready') {
        await loadReady();
      }
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  });

  onDestroy(stopPolling);

  async function scrollDown() {
    await tick();
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  async function send(question = input) {
    const q = question.trim();
    if (!q || asking || !ready) return;
    error = '';
    asking = true;
    input = '';
    // Show the question immediately; the server persists the turn once it succeeds.
    const pending: ChatMessage = {
      id: `pending-${Date.now()}`, role: 'user', content: q, verified: null, confidence: null,
      sources: [], pages_searched: [], warnings: [], count_result: null, created_at: new Date().toISOString(),
    };
    messages = [...messages, pending];
    await scrollDown();
    try {
      const res = await askQuestion(documentId, q);
      messages = [...messages.filter((m) => m.id !== pending.id), res.question, res.answer];
    } catch (e) {
      messages = messages.filter((m) => m.id !== pending.id);
      input = q;
      error = (e as Error).message;
    } finally {
      asking = false;
      await scrollDown();
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  async function clearChat() {
    if (!confirm('Clear this conversation?')) return;
    await clearMessages(documentId);
    messages = [];
  }

  async function reindex() {
    if (!confirm('Re-index this document? This re-reads every page and can take a few minutes.')) return;
    pages = [];
    await beginIndexing();
  }

  function open(page: number, evidence: Evidence[] = [], markers: CountMarker[] = []) {
    viewer = { page, evidence, markers };
  }

  function openSource(s: AnswerSource, m?: ChatMessage) {
    open(s.page_number, s.evidence, m?.count_result?.markers ?? []);
  }

  /** Open a sheet that has counted objects on it, with all markers loaded. */
  let added: Record<string, string> = {};      // message id -> 'added' or an error
  async function addToTakeoff(m: ChatMessage) {
    try {
      await addCountToTakeoff(documentId, { message_id: m.id });
      added = { ...added, [m.id]: 'added' };
    } catch (e) {
      added = { ...added, [m.id]: (e as Error).message };
    }
  }

  function openCounted(c: CountResult, page?: number) {
    const first = page ?? c.markers[0]?.page_number ?? Number(Object.keys(c.methods[0]?.per_page ?? {})[0]);
    if (first) open(first, [], c.markers);
  }

  const statusInfo: Record<CountStatus, { label: string; badge: string; note: string }> = {
    cross_checked: {
      label: 'Cross-checked',
      badge: 'bg-green-100 text-green-800',
      note: 'Two independent readings of the drawing agree. This is still a count read from the drawing, not a guarantee.',
    },
    single_source: {
      label: 'One source: verify',
      badge: 'bg-amber-100 text-amber-800',
      note: 'Only one reading of the drawing produced this number. Check the marked sheet.',
    },
    needs_verification: {
      label: 'Needs verification',
      badge: 'bg-red-100 text-red-800',
      note: 'Do not rely on this number until it is checked against the drawing.',
    },
    not_found: {
      label: 'Not counted',
      badge: 'bg-gray-100 text-gray-700',
      note: 'Nothing countable was found. That does not prove there are none.',
    },
  };

  const methodNames: Record<string, string> = {
    tag_instances: 'Tag labels on the plan',
    symbol: 'Legend symbol matched on the plan',
    schedule_qty: 'Schedule quantity column',
    schedule_rows: 'Schedule rows',
    vision: 'Vision-model estimate',
  };

  function pageLabel(n: number): string {
    return pages.find((p) => p.page_number === n)?.label ?? `Page ${n}`;
  }

  const confidenceStyle: Record<string, string> = {
    high: 'bg-green-100 text-green-800',
    medium: 'bg-amber-100 text-amber-800',
    low: 'bg-red-100 text-red-800',
  };
</script>

{#if loading}
  <div class="card p-10 text-center text-sm text-gray-500">Loading assistant…</div>
{:else}
  <div class="grid gap-6 lg:grid-cols-[1fr_20rem]">
    <!-- Conversation -->
    <section class="card flex min-h-[32rem] flex-col">
      <div class="flex items-center justify-between border-b border-gray-100 px-5 py-3">
        <div>
          <h2 class="text-sm font-bold text-gray-800">Ask about this drawing set</h2>
          <p class="text-xs text-gray-400">Answers cite the sheets they come from.</p>
        </div>
        <div class="flex gap-2">
          {#if messages.length > 0}
            <button class="btn-secondary text-xs" on:click={clearChat}>Clear chat</button>
          {/if}
        </div>
      </div>

      {#if status?.status === 'indexing' || status?.status === 'not_indexed'}
        <div class="m-5 rounded-lg border border-blue-200 bg-blue-50 p-4">
          <p class="text-sm font-medium text-blue-900">
            Reading the drawings… {status?.pages_indexed ?? 0}{status?.total_pages ? ` / ${status.total_pages}` : ''} pages
          </p>
          <div class="mt-2 h-2 w-full rounded-full bg-blue-100">
            <div class="h-2 rounded-full bg-blue-500 transition-all" style="width: {progress}%"></div>
          </div>
          <p class="mt-2 text-xs text-blue-700">
            Each page is rendered and its text extracted so questions can be matched to the right sheets. Large sets take a few minutes; you can leave this tab open.
          </p>
        </div>
      {:else if status?.status === 'failed'}
        <div class="m-5 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          <p class="font-medium">Indexing failed.</p>
          <p class="mt-1 break-words text-xs">{status.error}</p>
          <button class="btn-primary mt-3 text-xs" on:click={beginIndexing}>Retry indexing</button>
        </div>
      {/if}

      {#if ready && status?.error}
        <p class="mx-5 mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{status.error}</p>
      {/if}

      <div bind:this={scroller} class="flex-1 space-y-4 overflow-y-auto px-5 py-4" aria-live="polite">
        {#if ready && messages.length === 0}
          <div class="py-6 text-center">
            <p class="text-sm text-gray-500">Try a question:</p>
            <div class="mt-3 flex flex-wrap justify-center gap-2">
              {#each EXAMPLES as ex}
                <button
                  class="rounded-full border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-700 hover:border-blue-400 hover:text-blue-700"
                  on:click={() => send(ex)}
                >{ex}</button>
              {/each}
            </div>
          </div>
        {/if}

        {#each messages as m (m.id)}
          {#if m.role === 'user'}
            <div class="flex justify-end">
              <div class="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-blue-600 px-4 py-2 text-sm text-white">{m.content}</div>
            </div>
          {:else}
            <div class="flex">
              <div class="max-w-[92%] rounded-2xl rounded-bl-sm border border-gray-200 bg-white px-4 py-3 text-sm text-gray-800">
                <div class="mb-2 flex flex-wrap items-center gap-2">
                  {#if m.count_result}
                    <span class="rounded-full bg-blue-50 px-2 py-0.5 text-[11px] font-semibold text-blue-700">Object count</span>
                  {:else if m.verified}
                    <span class="rounded-full bg-green-100 px-2 py-0.5 text-[11px] font-semibold text-green-800">Found in drawings</span>
                    {#if m.confidence}
                      <span class="rounded-full px-2 py-0.5 text-[11px] font-semibold {confidenceStyle[m.confidence]}">{m.confidence} confidence</span>
                    {/if}
                  {:else}
                    <span class="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-800">Not verified</span>
                  {/if}
                </div>

                {#if m.count_result}
                  {@const c = m.count_result}
                  {@const info = statusInfo[c.status]}
                  <div class="mb-3 rounded-lg border border-gray-200 bg-gray-50 p-3" data-testid="count-card">
                    <div class="flex flex-wrap items-baseline gap-3">
                      {#if c.quantity !== null}
                        <span class="text-4xl font-extrabold tabular-nums text-gray-900">
                          {c.status === 'needs_verification' ? '≈ ' : ''}{c.quantity}
                        </span>
                        <span class="text-sm text-gray-600">{c.object}{c.quantity === 1 ? '' : 's'}</span>
                      {:else}
                        <span class="text-lg font-semibold text-gray-500">No count</span>
                      {/if}
                      <span class="rounded-full px-2 py-0.5 text-[11px] font-semibold {info.badge}">{info.label}</span>
                    </div>
                    <p class="mt-1 text-xs text-gray-500">{info.note}</p>

                    {#if c.blocking.length > 0}
                      <div class="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-800">
                        <p class="font-semibold">Why this needs verification</p>
                        <ul class="ml-4 list-disc">
                          {#each c.blocking as b}<li>{b}</li>{/each}
                        </ul>
                      </div>
                    {/if}

                    {#if c.methods.length > 0}
                      <div class="mt-2">
                        <p class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">How it was counted</p>
                        <ul class="mt-1 space-y-0.5">
                          {#each c.methods as meth}
                            <li class="text-xs text-gray-600">
                              <span class="font-semibold tabular-nums text-gray-800">{meth.quantity}</span>
                              <span class="text-gray-400">·</span>
                              {methodNames[meth.method] ?? meth.method}
                              {#if meth.method === c.primary}<span class="ml-1 rounded bg-blue-50 px-1 text-[10px] text-blue-700">headline</span>{/if}
                              <span class="block pl-5 text-gray-400">{meth.detail}</span>
                            </li>
                          {/each}
                        </ul>
                      </div>
                    {/if}

                    {#if c.methods[0] && Object.keys(c.methods[0].per_page).length > 0}
                      <div class="mt-2 flex flex-wrap items-center gap-1.5">
                        <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">By sheet</span>
                        {#each Object.entries(c.methods[0].per_page) as [pg, n]}
                          <button
                            class="rounded-md border border-blue-200 bg-white px-2 py-0.5 text-xs text-blue-700 hover:bg-blue-50"
                            on:click={() => openCounted(c, Number(pg))}
                          >{pageLabel(Number(pg))}: {n}</button>
                        {/each}
                      </div>
                    {/if}

                    {#if c.methods.length > 0}
                      <button class="btn-primary mt-3 text-xs" on:click={() => openCounted(c)}>
                        View marked sheet{c.markers.length ? ` (${c.markers.length} outlined)` : ''}
                      </button>
                      {#if c.markers.length === 0}
                        <p class="mt-1 text-[11px] text-gray-400">This count has no per-object markers; open the sheet to check it by eye.</p>
                      {/if}
                    {/if}
                    {#if c.quantity !== null && !m.id.startsWith('pending-')}
                      <div class="mt-2 flex items-center gap-2">
                        <button class="btn-secondary text-xs" on:click={() => addToTakeoff(m)} disabled={added[m.id] === 'added'}
                          data-testid="add-to-takeoff">{added[m.id] === 'added' ? 'Added to takeoff ✓' : 'Add to takeoff'}</button>
                        {#if added[m.id] && added[m.id] !== 'added'}<span class="text-xs text-red-600">{added[m.id]}</span>{/if}
                      </div>
                    {/if}
                    {#if c.markers_truncated}
                      <p class="mt-1 text-[11px] text-gray-400">Only the first markers are shown.</p>
                    {/if}
                  </div>
                {/if}

                <p class="whitespace-pre-wrap leading-relaxed">{m.content}</p>

                {#if m.sources.length > 0}
                  <div class="mt-3 border-t border-gray-100 pt-2">
                    <p class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">Source</p>
                    <div class="mt-1.5 flex flex-wrap gap-1.5">
                      {#each m.sources as s}
                        <button
                          class="rounded-md border border-blue-200 bg-blue-50 px-2 py-1 text-xs font-medium text-blue-700 hover:bg-blue-100"
                          title="Open this sheet"
                          on:click={() => openSource(s, m)}
                        >
                          {s.label}{s.sheet_title ? ` · ${s.sheet_title}` : ''}
                          {#if s.sheet_number}<span class="text-blue-400"> (p.{s.page_number})</span>{/if}
                        </button>
                      {/each}
                    </div>
                    <ul class="mt-2 space-y-1">
                      {#each m.sources as s}
                        {#each s.evidence as ev}
                          <li class="text-xs text-gray-500">
                            <span class="font-medium text-gray-600">{s.label}:</span>
                            <span class="font-mono">“{ev.quote}”</span>
                            {#if ev.location}<span> — {ev.location}</span>{/if}
                            {#if !ev.confirmed}
                              <span class="text-amber-600" title="This quote was not found in the text extracted from the page">· unconfirmed</span>
                            {/if}
                          </li>
                        {/each}
                      {/each}
                    </ul>
                  </div>
                {:else if m.pages_searched.length > 0}
                  <div class="mt-3 border-t border-gray-100 pt-2">
                    <p class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">Sheets checked</p>
                    <div class="mt-1.5 flex flex-wrap gap-1.5">
                      {#each m.pages_searched as n}
                        <button
                          class="rounded-md border border-gray-200 bg-gray-50 px-2 py-1 text-xs text-gray-600 hover:bg-gray-100"
                          on:click={() => open(n)}
                        >{pageLabel(n)}</button>
                      {/each}
                    </div>
                  </div>
                {/if}

                {#each m.warnings as w}
                  <p class="mt-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">{w}</p>
                {/each}
              </div>
            </div>
          {/if}
        {/each}

        {#if asking}
          <div class="flex">
            <div class="rounded-2xl rounded-bl-sm border border-gray-200 bg-white px-4 py-3 text-sm text-gray-500">
              Finding the relevant sheets and reading them… this can take a little while.
            </div>
          </div>
        {/if}
      </div>

      {#if error}
        <p class="mx-5 mb-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>
      {/if}

      <form class="flex items-end gap-2 border-t border-gray-100 p-4" on:submit|preventDefault={() => send()}>
        <textarea
          bind:value={input}
          on:keydown={onKeydown}
          rows="2"
          disabled={!ready || asking}
          placeholder={ready ? 'e.g. What model is RTU-1?' : 'Available once indexing finishes'}
          aria-label="Ask a question about the drawings"
          class="flex-1 resize-none rounded-lg border border-gray-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50"
        ></textarea>
        <button class="btn-primary" type="submit" disabled={!ready || asking || !input.trim()}>Ask</button>
      </form>
    </section>

    <!-- Sheet index -->
    <aside class="card h-fit p-4">
      <div class="flex items-center justify-between">
        <h3 class="text-sm font-bold text-gray-800">Sheets ({pages.length})</h3>
        <button class="text-xs text-blue-600 hover:underline lg:hidden" on:click={() => (showIndex = !showIndex)}>
          {showIndex ? 'Hide' : 'Show'}
        </button>
      </div>
      <div class="mt-3 max-h-[28rem] overflow-y-auto {showIndex ? '' : 'hidden lg:block'}">
        {#if pages.length === 0}
          <p class="text-xs text-gray-400">No pages indexed yet.</p>
        {/if}
        <ul class="space-y-1">
          {#each pages as p (p.page_number)}
            <li>
              <button
                class="flex w-full items-baseline gap-2 rounded px-2 py-1 text-left text-xs hover:bg-blue-50"
                on:click={() => open(p.page_number)}
              >
                <span class="w-14 flex-shrink-0 font-mono font-semibold text-gray-800">{p.label}</span>
                <span class="min-w-0 flex-1 truncate text-gray-500">{p.sheet_title ?? ''}</span>
                {#if p.text_source === 'none'}
                  <span class="text-amber-600" title="No text could be extracted from this page">no text</span>
                {:else if p.text_source === 'ocr'}
                  <span class="text-gray-300" title="Text read with OCR">OCR</span>
                {/if}
              </button>
            </li>
          {/each}
        </ul>
      </div>
      {#if ready}
        <button class="btn-secondary mt-3 w-full text-xs" on:click={reindex}>Re-index document</button>
      {/if}
    </aside>
  </div>
{/if}

{#if viewer}
  <PageViewer
    {documentId}
    {pages}
    pageNumber={viewer.page}
    evidence={viewer.evidence}
    markers={viewer.markers}
    on:close={() => (viewer = null)}
    on:navigate={(e) => (viewer = { page: e.detail, evidence: [], markers: viewer?.markers ?? [] })}
  />
{/if}
