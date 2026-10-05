<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount, tick } from 'svelte';
  import MeasureLayer from './MeasureLayer.svelte';
  import { getHighlights, getReferences, pageImageUrl } from '$lib/api';
  import type { Box, CountMarker, DrawingPage, Evidence, SheetReference } from '$lib/api';

  export let documentId: string;
  export let pages: DrawingPage[];
  export let pageNumber: number;
  export let evidence: Evidence[] = [];
  /** Counted objects to outline on the sheet (all pages; the current page's are drawn). */
  export let markers: CountMarker[] = [];
  /** A search query: its matches are highlighted on every sheet that is opened. */
  export let query = '';
  /** Sheets that matched the query, in order, for "next sheet with matches". */
  export let matchPages: number[] = [];

  const dispatch = createEventDispatcher<{ close: void; navigate: number }>();

  type Zoom = 'fit' | 1 | 2 | 3;
  let zoom: Zoom = 'fit';
  let imgError = false;
  let loaded = false;
  let showMarkers = true;
  let showHighlights = true;
  let highlights: Box[] = [];
  let refs: SheetReference[] = [];
  let focus = 0;
  let navNote = '';
  let history: number[] = [];
  let focusEl: SVGRectElement | undefined;
  let loadToken = 0;

  $: current = pages.find((p) => p.page_number === pageNumber);
  $: idx = pages.findIndex((p) => p.page_number === pageNumber);
  $: pageMarkers = markers.filter((m) => m.page_number === pageNumber);
  // Cited quotes (from an answer's sources) are highlighted too; only ones that were
  // found in the sheet's own text, so a doubtful quote is never drawn as if located.
  $: phrases = evidence.filter((e) => e.confirmed).map((e) => e.quote);
  $: labelOf = (n: number) => pages.find((p) => p.page_number === n)?.label ?? `Page ${n}`;
  $: nextMatchPage = matchPages.find((n) => n > pageNumber) ?? matchPages.find((n) => n !== pageNumber);

  // Reset view state and load positions whenever the page (or query) changes.
  $: pageNumber, query, resetAndLoad();

  function resetAndLoad() {
    imgError = false;
    loaded = false;
    zoom = 'fit';
    focus = 0;
    highlights = [];
    refs = [];
    navNote = '';
    const token = ++loadToken;
    const wantsHighlights = !!query || phrases.length > 0;
    // Positions come from the PDF; if it is no longer stored these quietly stay empty.
    if (wantsHighlights) {
      getHighlights(documentId, pageNumber, query, phrases)
        .then((r) => { if (token === loadToken) highlights = r.boxes; })
        .catch((e) => { if (token === loadToken) navNote = (e as Error).message; });
    }
    getReferences(documentId, pageNumber)
      .then((r) => { if (token === loadToken) refs = r; })
      .catch(() => {});
  }

  function go(delta: number) {
    const next = pages[idx + delta];
    if (next) dispatch('navigate', next.page_number);
  }

  /** Follow a reference to another sheet, remembering where we came from. */
  function follow(target: number) {
    history = [...history, pageNumber];
    dispatch('navigate', target);
  }

  function back() {
    const prev = history[history.length - 1];
    if (prev === undefined) return;
    history = history.slice(0, -1);
    dispatch('navigate', prev);
  }

  async function moveFocus(delta: number) {
    if (!highlights.length) return;
    focus = (focus + delta + highlights.length) % highlights.length;
    await tick();
    focusEl?.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
  }

  async function zoomToMatch() {
    if (!highlights.length) return;
    zoom = 2;
    await tick();
    await tick();
    focusEl?.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
  }

  let measuring = false;

  function onKey(e: KeyboardEvent) {
    if (measuring) return;   // the measure layer owns the keyboard (Esc, Enter, Backspace)
    if ((e.target as HTMLElement)?.tagName === 'INPUT') return;
    if (e.key === 'Escape') dispatch('close');
    else if (e.key === 'ArrowRight') go(1);
    else if (e.key === 'ArrowLeft') go(-1);
    else if (e.key === 'n') moveFocus(1);
    else if (e.key === 'p') moveFocus(-1);
    else if (e.key === 'Backspace' && history.length) back();
  }

  onMount(() => window.addEventListener('keydown', onKey));
  onDestroy(() => window.removeEventListener('keydown', onKey));
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="fixed inset-0 z-50 flex flex-col bg-gray-800" on:click|self={() => dispatch('close')}>
  <div class="flex flex-wrap items-center gap-3 bg-white px-4 py-2 shadow">
    <div class="min-w-[14rem] flex-1">
      <p class="truncate text-sm font-semibold text-gray-900">
        {current?.label ?? `Page ${pageNumber}`}
        {#if current?.sheet_title}<span class="font-normal text-gray-500"> — {current.sheet_title}</span>{/if}
      </p>
      <p class="text-xs text-gray-400">PDF page {pageNumber}{pages.length ? ` of ${pages.length}` : ''}</p>
    </div>
    <div class="flex flex-wrap items-center gap-1">
      {#if history.length > 0}
        <button class="btn-secondary text-xs" on:click={back} title="Back to the sheet you followed a reference from">
          ‹ Back to {labelOf(history[history.length - 1])}
        </button>
        <span class="mx-1 h-5 w-px bg-gray-200"></span>
      {/if}
      <button class="btn-secondary text-xs" on:click={() => go(-1)} disabled={idx <= 0} aria-label="Previous page">‹ Prev</button>
      <button class="btn-secondary text-xs" on:click={() => go(1)} disabled={idx < 0 || idx >= pages.length - 1} aria-label="Next page">Next ›</button>
      <span class="mx-1 h-5 w-px bg-gray-200"></span>
      {#if highlights.length > 0}
        <button class="btn-secondary text-xs" on:click={() => moveFocus(-1)} aria-label="Previous match" title="Previous match (p)">‹</button>
        <span class="text-xs tabular-nums text-gray-700" data-testid="match-counter" aria-live="polite">
          Match {focus + 1}/{highlights.length}
        </span>
        <button class="btn-secondary text-xs" on:click={() => moveFocus(1)} aria-label="Next match" title="Next match (n)">›</button>
        <button class="btn-secondary text-xs" on:click={zoomToMatch} title="Zoom in on this match">Zoom to match</button>
        <button
          class="btn-secondary text-xs {showHighlights ? 'ring-2 ring-yellow-400' : ''}"
          on:click={() => (showHighlights = !showHighlights)} aria-pressed={showHighlights}
        >Highlights</button>
      {:else if query}
        <span class="text-xs text-gray-400" data-testid="no-matches">No match for “{query}” on this sheet</span>
      {/if}
      {#if query && nextMatchPage !== undefined && nextMatchPage !== pageNumber}
        <button class="btn-secondary text-xs" on:click={() => dispatch('navigate', nextMatchPage)}
          title="Jump to the next sheet that matched">Next sheet with a match ›</button>
      {/if}
      {#if markers.length > 0}
        <button
          class="btn-secondary text-xs {showMarkers ? 'ring-2 ring-red-400' : ''}"
          on:click={() => (showMarkers = !showMarkers)}
          aria-pressed={showMarkers}
          title="Outline each counted object on the sheet"
        >Markers ({pageMarkers.length})</button>
      {/if}
      <button class="btn-secondary text-xs {measuring ? 'ring-2 ring-orange-400' : ''}" on:click={() => (measuring = !measuring)}
        aria-pressed={measuring} title="Measure lengths and areas on this sheet">Measure</button>
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

  {#if refs.length > 0}
    <div class="max-h-20 overflow-y-auto border-b border-blue-200 bg-blue-50 px-4 py-1.5 text-xs text-blue-900" data-testid="refs-bar">
      <span class="font-semibold">This sheet refers to:</span>
      {#each [...new Map(refs.map((r) => [r.target_page, r])).values()] as r}
        <button
          class="ml-1.5 rounded border border-blue-300 bg-white px-1.5 py-0.5 hover:bg-blue-100"
          on:click={() => follow(r.target_page)}
          title={r.text}
        >{r.target_label}{r.target_title ? ` · ${r.target_title}` : ''}</button>
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
      <div
        class="relative mx-auto w-fit bg-white shadow-xl"
        style={zoom === 'fit' ? 'max-width:100%' : `width:${zoom * 100}%`}
      >
        <img
          src={pageImageUrl(documentId, pageNumber)}
          alt={`Drawing page ${pageNumber}`}
          class="block"
          style={zoom === 'fit'
            ? 'max-width:100%;max-height:calc(100vh - 9rem);width:auto;height:auto'
            : 'width:100%;height:auto'}
          on:load={() => (loaded = true)}
          on:error={() => (imgError = true)}
        />
        {#if loaded && showMarkers && pageMarkers.length > 0}
          <svg
            class="pointer-events-none absolute inset-0 h-full w-full"
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            aria-label={`${pageMarkers.length} counted objects outlined`}
          >
            {#each pageMarkers as m}
              <rect
                x={m.x - 0.003} y={m.y - 0.003 * 1.5} width={m.w + 0.006} height={m.h + 0.009}
                fill="rgba(239,68,68,0.12)" stroke="#dc2626" stroke-width="2"
                vector-effect="non-scaling-stroke" rx="0.002"
              >
                {#if m.label}<title>{m.label}</title>{/if}
              </rect>
            {/each}
          </svg>
        {/if}
        {#if loaded && showHighlights && highlights.length > 0}
          <svg
            class="pointer-events-none absolute inset-0 h-full w-full"
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            aria-label={`${highlights.length} matches highlighted`}
            data-testid="highlights"
          >
            {#each highlights as b, i}
              {#if i === focus}
                <rect
                  bind:this={focusEl}
                  x={b.x - 0.004} y={b.y - 0.006} width={b.w + 0.008} height={b.h + 0.012}
                  fill="rgba(250,204,21,0.45)" stroke="#ea580c" stroke-width="3"
                  vector-effect="non-scaling-stroke" rx="0.002"
                />
              {:else}
                <rect
                  x={b.x - 0.003} y={b.y - 0.004} width={b.w + 0.006} height={b.h + 0.008}
                  fill="rgba(250,204,21,0.35)" stroke="#ca8a04" stroke-width="1.5"
                  vector-effect="non-scaling-stroke" rx="0.002"
                />
              {/if}
            {/each}
          </svg>
        {/if}
        {#if loaded && !measuring}
          {#each refs as r}
            <button
              class="absolute cursor-pointer rounded-sm border-2 border-dashed border-blue-500 bg-blue-500/10 hover:bg-blue-500/30"
              style="left:{(r.box.x - 0.004) * 100}%;top:{(r.box.y - 0.006) * 100}%;width:{(r.box.w + 0.008) * 100}%;height:{(r.box.h + 0.012) * 100}%"
              title={`Go to ${r.target_label}${r.target_title ? ' · ' + r.target_title : ''}`}
              aria-label={`Go to sheet ${r.target_label}`}
              data-testid="ref-hotspot"
              on:click={() => follow(r.target_page)}
            ></button>
          {/each}
        {/if}
        {#if loaded && measuring}
          <MeasureLayer {documentId} {pageNumber} on:exit={() => (measuring = false)} />
        {/if}
      </div>
    {/if}
  </div>
</div>
