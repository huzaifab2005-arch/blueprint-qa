<script lang="ts">
  import { onMount } from 'svelte';
  import { dismissFinding, getChecks, getIndexStatus, listPages, restoreFinding, startIndexing } from '$lib/api';
  import type { CountMarker, DrawingPage, Finding, SetChecks } from '$lib/api';
  import PageViewer from './PageViewer.svelte';

  export let documentId: string;

  let data: SetChecks | null = null;
  let pages: DrawingPage[] = [];
  let loading = true;
  let error = '';
  let notIndexed = false;
  let showDismissed = false;
  let viewer: { page: number; markers: CountMarker[] } | null = null;

  const checkName: Record<string, string> = {
    duplicate_sheet: 'Duplicate sheet number',
    index_mismatch: 'Drawing list',
    broken_reference: 'Missing reference',
    scale_conflict: 'Scale',
    dimension_mismatch: 'Dimension',
  };
  const sev: Record<string, string> = {
    high: 'bg-red-100 text-red-800', medium: 'bg-amber-100 text-amber-800', low: 'bg-gray-100 text-gray-700',
  };

  async function load() {
    loading = true; error = ''; notIndexed = false;
    try {
      const [d, p] = await Promise.all([getChecks(documentId, showDismissed), listPages(documentId).catch(() => [])]);
      data = d; pages = p;
    } catch (e) {
      const msg = (e as Error).message;
      if (/not indexed/i.test(msg)) notIndexed = true; else error = msg;
    } finally { loading = false; }
  }
  onMount(load);

  async function index() {
    await startIndexing(documentId);
    for (let i = 0; i < 300; i++) {
      const s = await getIndexStatus(documentId);
      if (s.status === 'ready') return load();
      if (s.status === 'failed') { error = s.error ?? 'Indexing failed.'; return; }
      await new Promise((r) => setTimeout(r, 2000));
    }
  }

  const toggle = async (f: Finding) => {
    await (f.dismissed ? restoreFinding(documentId, f.id) : dismissFinding(documentId, f.id));
    await load();
  };
  const view = (f: Finding) => {
    viewer = {
      page: f.page_number,
      markers: f.box ? [{ page_number: f.page_number, ...f.box, label: checkName[f.check] ?? f.check }] : [],
    };
  };
</script>

<div class="space-y-3" data-testid="set-checks">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <div>
      <h2 class="text-lg font-bold text-gray-900">Set checks</h2>
      <p class="text-xs text-gray-500">Cross-sheet checks that point a reviewer at things worth a look.</p>
    </div>
    <label class="flex items-center gap-1.5 text-xs text-gray-600">
      <input type="checkbox" bind:checked={showDismissed} on:change={load} /> Show dismissed
    </label>
  </div>

  {#if error}<p class="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">{error}</p>{/if}
  {#if loading}
    <p class="text-sm text-gray-400">Checking the set…</p>
  {:else if notIndexed}
    <div class="rounded-lg border border-gray-200 bg-white p-4 text-sm text-gray-700">
      The set needs to be indexed before it can be checked.
      <button class="btn-primary ml-2 text-xs" on:click={index}>Index now</button>
    </div>
  {:else if data}
    <p class="text-sm font-medium text-gray-800" data-testid="checks-summary">{data.summary}</p>
    {#if data.pages_skipped}<p class="text-xs text-amber-700">{data.pages_skipped} sheet(s) could not be read and were skipped.</p>{/if}

    <ul class="space-y-2">
      {#each data.findings as f (f.id)}
        <li class="rounded-lg border border-gray-200 bg-white p-3 {f.dismissed ? 'opacity-60' : ''}" data-testid="finding">
          <div class="flex flex-wrap items-center gap-2">
            <span class="rounded-full px-2 py-0.5 text-[11px] font-semibold {sev[f.severity]}">{f.severity}</span>
            <span class="text-xs font-semibold text-gray-700">{checkName[f.check] ?? f.check}</span>
            <span class="text-xs text-gray-400">· {f.label} · {f.confidence} confidence</span>
          </div>
          <p class="mt-1 text-sm text-gray-800">{f.message}</p>
          {#if f.evidence.length}
            <ul class="mt-1 text-xs text-gray-500">{#each f.evidence as e}<li>{e}</li>{/each}</ul>
          {/if}
          <div class="mt-2 flex gap-2">
            <button class="btn-secondary text-xs" on:click={() => view(f)}>View sheet</button>
            <button class="text-xs text-gray-500 hover:text-gray-800" on:click={() => toggle(f)}>{f.dismissed ? 'Restore' : 'Dismiss'}</button>
          </div>
        </li>
      {/each}
    </ul>
    <p class="text-[11px] text-gray-400">
      Checks run: {data.checks_run.map((c) => checkName[c] ?? c).join(', ')}. {data.disclaimer}
    </p>
  {/if}
</div>

{#if viewer}
  <PageViewer
    {documentId} {pages} pageNumber={viewer.page} markers={viewer.markers}
    on:close={() => (viewer = null)}
    on:navigate={(e) => (viewer = { page: e.detail, markers: [] })}
  />
{/if}
