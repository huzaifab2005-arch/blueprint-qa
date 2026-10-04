<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { pageImageUrl } from '$lib/api';
  import type { DrawingPage, Evidence } from '$lib/api';

  export let documentId: string;
  export let pages: DrawingPage[];
  export let pageNumber: number;
  export let evidence: Evidence[] = [];

  const dispatch = createEventDispatcher<{ close: void; navigate: number }>();

  type Zoom = 'fit' | 1 | 2 | 3;
  let zoom: Zoom = 'fit';
  let imgError = false;
  let loaded = false;

  $: current = pages.find((p) => p.page_number === pageNumber);
  $: idx = pages.findIndex((p) => p.page_number === pageNumber);
  // Reset view state whenever the page changes.
  $: pageNumber, ((imgError = false), (loaded = false), (zoom = 'fit'));

  function go(delta: number) {
    const next = pages[idx + delta];
    if (next) dispatch('navigate', next.page_number);
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Escape') dispatch('close');
    else if (e.key === 'ArrowRight') go(1);
    else if (e.key === 'ArrowLeft') go(-1);
  }

  onMount(() => window.addEventListener('keydown', onKey));
  onDestroy(() => window.removeEventListener('keydown', onKey));
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="fixed inset-0 z-50 flex flex-col bg-gray-800" on:click|self={() => dispatch('close')}>
  <div class="flex flex-wrap items-center gap-3 bg-white px-4 py-2 shadow">
    <div class="min-w-0 flex-1">
      <p class="truncate text-sm font-semibold text-gray-900">
        {current?.label ?? `Page ${pageNumber}`}
        {#if current?.sheet_title}<span class="font-normal text-gray-500"> — {current.sheet_title}</span>{/if}
      </p>
      <p class="text-xs text-gray-400">PDF page {pageNumber}{pages.length ? ` of ${pages.length}` : ''}</p>
    </div>
    <div class="flex items-center gap-1">
      <button class="btn-secondary text-xs" on:click={() => go(-1)} disabled={idx <= 0} aria-label="Previous page">‹ Prev</button>
      <button class="btn-secondary text-xs" on:click={() => go(1)} disabled={idx < 0 || idx >= pages.length - 1} aria-label="Next page">Next ›</button>
      <span class="mx-1 h-5 w-px bg-gray-200"></span>
      {#each [['fit', 'Fit'], [1, '100%'], [2, '200%'], [3, '300%']] as [z, label]}
        <button class="btn-secondary text-xs {zoom === z ? 'ring-2 ring-blue-500' : ''}" on:click={() => (zoom = z as Zoom)}>{label}</button>
      {/each}
      <a class="btn-secondary text-xs" href={pageImageUrl(documentId, pageNumber)} target="_blank" rel="noreferrer">Open image</a>
      <button class="btn-primary text-xs" on:click={() => dispatch('close')}>Close</button>
    </div>
  </div>

  {#if evidence.length > 0}
    <div class="max-h-28 overflow-y-auto border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-900">
      <span class="font-semibold">Cited on this sheet:</span>
      {#each evidence as ev}
        <span class="ml-2 inline-block rounded bg-white px-1.5 py-0.5 font-mono">
          {ev.quote}{#if ev.location}<span class="text-amber-600"> ({ev.location})</span>{/if}
        </span>
      {/each}
    </div>
  {/if}

  <div class="flex-1 overflow-auto p-4" on:click|self={() => dispatch('close')}>
    {#if imgError}
      <div class="mx-auto mt-16 max-w-md rounded-lg bg-white p-6 text-center text-sm text-gray-600">
        This page image is no longer available. Re-index the document to regenerate it.
      </div>
    {:else}
      {#if !loaded}
        <p class="text-center text-sm text-white">Loading page…</p>
      {/if}
      <img
        src={pageImageUrl(documentId, pageNumber)}
        alt={`Drawing page ${pageNumber}`}
        class="mx-auto bg-white shadow-xl {zoom === 'fit' ? 'max-h-full max-w-full object-contain' : ''}"
        style={zoom === 'fit' ? '' : `width:${zoom * 100}%;max-width:none`}
        on:load={() => (loaded = true)}
        on:error={() => (imgError = true)}
      />
    {/if}
  </div>
</div>
