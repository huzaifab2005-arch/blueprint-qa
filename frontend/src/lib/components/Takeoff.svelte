<script lang="ts">
  import { onMount } from 'svelte';
  import {
    addCountToTakeoff, addManualToTakeoff, addMeasurementsToTakeoff, deleteTakeoffItem, generateTakeoff, getTakeoff,
    listMeasurements, refreshTakeoffItem, takeoffCsvUrl, updateTakeoffItem,
  } from '$lib/api';
  import type { Discipline, Measurement, Takeoff, TakeoffItem } from '$lib/api';

  export let documentId: string;

  let data: Takeoff | null = null;
  let measurements: Measurement[] = [];
  let loading = true;
  let error = '';
  let busy = false;
  let open: string | null = null;           // line whose basis/warnings are expanded

  let countQ = '';
  let countCat = '';
  let pick: Record<string, boolean> = {};
  let measDesc = '';
  let measCat = '';
  let man = { description: '', quantity: '', unit: 'EA', category: '' };

  const badge: Record<string, { label: string; cls: string }> = {
    verified: { label: 'Verified', cls: 'bg-green-100 text-green-800' },
    needs_verification: { label: 'Needs verification', cls: 'bg-amber-100 text-amber-800' },
    manual: { label: 'Manual', cls: 'bg-gray-100 text-gray-700' },
  };
  const kindName: Record<string, string> = { count: 'Count', measurement: 'Measured', discipline: 'Takeoff', manual: 'Entered' };
  const disciplines: [Discipline, string][] = [['lighting', 'Lighting'], ['hvac', 'HVAC'], ['plumbing', 'Plumbing fixtures']];
  let genNote = '';
  const generate = (d: Discipline) => run(async () => {
    genNote = '';
    const r = await generateTakeoff(documentId, d);
    genNote = r.items.length
      ? `${r.label}: ${r.added} added, ${r.updated} updated${r.removed ? `, ${r.removed} removed` : ''}${r.kept_edited ? `, ${r.kept_edited} kept as you edited them` : ''}.`
      : r.notes.join(' ');
  });

  async function load() {
    try {
      [data, measurements] = await Promise.all([getTakeoff(documentId), listMeasurements(documentId)]);
    } catch (e) { error = (e as Error).message; } finally { loading = false; }
  }
  onMount(load);

  async function run(fn: () => Promise<unknown>) {
    busy = true; error = '';
    try { await fn(); await load(); } catch (e) { error = (e as Error).message; } finally { busy = false; }
  }

  const addCount = () => run(async () => { await addCountToTakeoff(documentId, { question: countQ }, countCat); countQ = ''; });
  const addMeas = () => run(async () => {
    await addMeasurementsToTakeoff(documentId, Object.keys(pick).filter((k) => pick[k]), measDesc, measCat);
    pick = {}; measDesc = '';
  });
  const addManual = () => run(async () => {
    await addManualToTakeoff(documentId, { description: man.description, quantity: Number(man.quantity), unit: man.unit, category: man.category });
    man = { ...man, description: '', quantity: '' };
  });
  const patch = (it: TakeoffItem, p: Parameters<typeof updateTakeoffItem>[2]) => run(() => updateTakeoffItem(documentId, it.id, p));
  const refresh = (it: TakeoffItem) => run(() => refreshTakeoffItem(documentId, it.id));
  const remove = (it: TakeoffItem) => run(() => deleteTakeoffItem(documentId, it.id));

  const fmt = (n: number) => (Number.isInteger(n) ? String(n) : n.toLocaleString(undefined, { maximumFractionDigits: 2 }));
  $: unverified = data ? data.lines - data.verified_lines : 0;
  $: selected = Object.values(pick).filter(Boolean).length;
  $: measLabel = (m: Measurement) => `${m.label || (m.kind === 'area' ? 'Area' : 'Length')} · ${m.display} · p.${m.page_number} · ${m.scale_status}`;
</script>

<div class="space-y-4" data-testid="takeoff">
  <div class="flex flex-wrap items-center justify-between gap-2">
    <div>
      <h2 class="text-lg font-bold text-gray-900">Quantity takeoff</h2>
      <p class="text-xs text-gray-500">Every line shows where its number came from and whether the drawing confirmed it.</p>
    </div>
    <a class="btn-secondary text-xs {data && data.lines ? '' : 'pointer-events-none opacity-50'}" href={takeoffCsvUrl(documentId)} download>Export CSV</a>
  </div>

  <div class="rounded-lg border border-gray-200 bg-white p-3" data-testid="generate-bar">
    <p class="text-sm font-semibold text-gray-800">Generate a takeoff from the drawings</p>
    <div class="mt-2 flex flex-wrap items-center gap-2">
      {#each disciplines as [key, label]}
        <button class="btn-primary text-xs" disabled={busy} on:click={() => generate(key)} data-testid="generate-{key}">{busy ? 'Working…' : label}</button>
      {/each}
      <span class="text-[11px] text-gray-400">Reads schedules and legends, then counts each item on the plans. Run again to refresh; lines you edited are kept.</span>
    </div>
    {#if genNote}<p class="mt-2 text-xs text-gray-600" data-testid="generate-note">{genNote}</p>{/if}
  </div>

  {#if error}<p class="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">{error}</p>{/if}

  {#if loading}
    <p class="text-sm text-gray-400">Loading…</p>
  {:else if data}
    {#if data.lines > 0}
      <div class="grid gap-2 sm:grid-cols-2 lg:grid-cols-4" data-testid="takeoff-totals">
        {#each Object.entries(data.by_unit) as [unit, t]}
          <div class="rounded-lg border border-gray-200 bg-white p-3">
            <p class="text-[11px] font-semibold uppercase tracking-wide text-gray-400">Total · {unit}</p>
            <p class="text-2xl font-extrabold tabular-nums text-gray-900">{fmt(t.quantity)}</p>
            <p class="text-xs text-gray-500">{fmt(t.verified_quantity)} of it verified · order {fmt(t.with_waste)} with waste</p>
          </div>
        {/each}
      </div>
      {#if unverified > 0}
        <p class="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800" data-testid="unverified-banner">
          {unverified} of {data.lines} line{data.lines === 1 ? '' : 's'} {unverified === 1 ? 'is' : 'are'} not verified by the drawing
          ({data.needs_verification_lines} need verification, {data.manual_lines} manual). Check them on the sheets before relying on the totals.
        </p>
      {/if}

      <div class="overflow-x-auto rounded-lg border border-gray-200 bg-white">
        <table class="w-full text-sm">
          <thead class="bg-gray-50 text-left text-[11px] uppercase tracking-wide text-gray-500">
            <tr><th class="px-3 py-2">Item</th><th class="px-2">Description</th><th class="px-2">Model / spec</th><th class="px-2">Qty</th><th class="px-2">Unit</th><th class="px-2">Waste %</th>
              <th class="px-2">Order</th><th class="px-2">Status</th><th class="px-2">Source</th><th class="px-2"></th></tr>
          </thead>
          <tbody>
            {#each data.items as it (it.id)}
              <tr class="border-t border-gray-100 align-top" data-testid="takeoff-line">
                <td class="px-3 py-2">
                  <input class="w-full min-w-40 rounded border border-transparent px-1 font-medium hover:border-gray-200 focus:border-blue-300 focus:outline-none"
                    value={it.description} aria-label="Description" on:change={(e) => patch(it, { description: e.currentTarget.value })} />
                  <input class="mt-0.5 w-full rounded border border-transparent px-1 text-xs text-gray-500 hover:border-gray-200 focus:border-blue-300 focus:outline-none"
                    placeholder="Category" value={it.category} aria-label="Category" on:change={(e) => patch(it, { category: e.currentTarget.value })} />
                </td>
                <td class="max-w-56 px-2 py-2 text-xs text-gray-600" title={it.details}>{it.details ? (it.details.length > 90 ? it.details.slice(0, 90) + '…' : it.details) : '—'}</td>
                <td class="max-w-48 px-2 py-2 text-xs text-gray-800" title={it.model}>{it.model || '—'}</td>
                <td class="px-2 py-2">
                  <input type="number" min="0" step="any" class="w-20 rounded border border-gray-200 px-1 py-0.5 tabular-nums" aria-label="Quantity"
                    value={it.quantity} on:change={(e) => patch(it, { quantity: Number(e.currentTarget.value) })} />
                  {#if it.uncertainty}<span class="block text-[11px] text-gray-400">± {fmt(it.uncertainty)}</span>{/if}
                  {#if it.computed_quantity !== null && it.computed_quantity !== it.quantity}
                    <span class="block text-[11px] text-amber-700">drawing: {fmt(it.computed_quantity)}</span>
                  {/if}
                </td>
                <td class="px-2 py-2 text-gray-700">{it.unit}</td>
                <td class="px-2 py-2">
                  <input type="number" min="0" max="100" step="any" class="w-16 rounded border border-gray-200 px-1 py-0.5 tabular-nums" aria-label="Waste percent"
                    value={it.waste_pct} on:change={(e) => patch(it, { waste_pct: Number(e.currentTarget.value) })} />
                </td>
                <td class="px-2 py-2 font-semibold tabular-nums">{fmt(it.order_quantity)}</td>
                <td class="px-2 py-2">
                  <span class="rounded-full px-2 py-0.5 text-[11px] font-semibold {badge[it.status].cls}">{badge[it.status].label}</span>
                  {#if it.confidence}<span class="block text-[11px] text-gray-400">{it.confidence} confidence</span>{/if}
                </td>
                <td class="px-2 py-2 text-xs text-gray-600">
                  {kindName[it.source_kind]}
                  {#if it.source}<span class="block font-medium text-gray-800" data-testid="line-source">{it.source}</span>{/if}
                  <button class="text-blue-700 underline" on:click={() => (open = open === it.id ? null : it.id)}>
                    {open === it.id ? 'hide' : 'details'}{it.warnings.length ? ` (${it.warnings.length} note${it.warnings.length === 1 ? '' : 's'})` : ''}
                  </button>
                </td>
                <td class="whitespace-nowrap px-2 py-2 text-right text-xs">
                  {#if it.source_kind === 'count' || it.source_kind === 'measurement'}<button class="text-gray-500 hover:text-blue-700" disabled={busy} on:click={() => refresh(it)} title="Recompute from the drawing">↻</button>{/if}
                  <button class="ml-2 text-gray-400 hover:text-red-600" disabled={busy} aria-label="Delete line" on:click={() => remove(it)}>✕</button>
                </td>
              </tr>
              {#if open === it.id}
                <tr class="bg-gray-50 text-xs text-gray-600"><td colspan="10" class="px-3 py-2">
                  <p>{it.basis}</p>
                  {#each it.sources as s}<p class="text-gray-500">{s.label}: {s.note}</p>{/each}
                  {#each it.warnings as w}<p class="mt-1 rounded bg-amber-50 px-2 py-1 text-amber-800">{w}</p>{/each}
                </td></tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
      <p class="text-[11px] text-gray-400">{data.disclaimer}</p>
    {:else}
      <p class="rounded-lg border border-dashed border-gray-300 bg-white px-4 py-6 text-center text-sm text-gray-500">
        No lines yet. Add a count, some measurements or a manual entry below, or use “Add to takeoff” on a count answer in the Drawing Assistant.
      </p>
    {/if}

    <div class="grid gap-3 lg:grid-cols-3">
      <form class="rounded-lg border border-gray-200 bg-white p-3" on:submit|preventDefault={addCount}>
        <h3 class="text-sm font-semibold text-gray-800">Count objects</h3>
        <input class="mt-2 w-full rounded border border-gray-300 px-2 py-1 text-sm" placeholder="e.g. 2x4 lights" bind:value={countQ} aria-label="What to count" />
        <input class="mt-1.5 w-full rounded border border-gray-300 px-2 py-1 text-sm" placeholder="Category (optional)" bind:value={countCat} />
        <button class="btn-primary mt-2 text-xs" disabled={busy || countQ.trim().length < 2}>Count and add</button>
      </form>

      <form class="rounded-lg border border-gray-200 bg-white p-3" on:submit|preventDefault={addMeas}>
        <h3 class="text-sm font-semibold text-gray-800">From measurements</h3>
        {#if measurements.length === 0}
          <p class="mt-2 text-xs text-gray-400">No saved measurements. Use Measure in the sheet viewer first.</p>
        {:else}
          <ul class="mt-2 max-h-32 space-y-1 overflow-y-auto text-xs">
            {#each measurements as m}
              <li><label class="flex items-start gap-1.5"><input type="checkbox" bind:checked={pick[m.id]} class="mt-0.5" />
                <span>{measLabel(m)}</span></label></li>
            {/each}
          </ul>
          <input class="mt-2 w-full rounded border border-gray-300 px-2 py-1 text-sm" placeholder="Line description" bind:value={measDesc} aria-label="Line description" />
          <input class="mt-1.5 w-full rounded border border-gray-300 px-2 py-1 text-sm" placeholder="Category (optional)" bind:value={measCat} />
          <button class="btn-primary mt-2 text-xs" disabled={busy || selected === 0 || !measDesc.trim()}>Add {selected || ''} measurement{selected === 1 ? '' : 's'}</button>
        {/if}
      </form>

      <form class="rounded-lg border border-gray-200 bg-white p-3" on:submit|preventDefault={addManual}>
        <h3 class="text-sm font-semibold text-gray-800">Manual entry</h3>
        <input class="mt-2 w-full rounded border border-gray-300 px-2 py-1 text-sm" placeholder="Description" bind:value={man.description} aria-label="Manual description" />
        <div class="mt-1.5 flex gap-1.5">
          <input class="w-24 rounded border border-gray-300 px-2 py-1 text-sm" type="number" min="0" step="any" placeholder="Qty" bind:value={man.quantity} aria-label="Manual quantity" />
          <input class="w-20 rounded border border-gray-300 px-2 py-1 text-sm" placeholder="Unit" bind:value={man.unit} aria-label="Manual unit" />
          <input class="min-w-0 flex-1 rounded border border-gray-300 px-2 py-1 text-sm" placeholder="Category" bind:value={man.category} />
        </div>
        <button class="btn-primary mt-2 text-xs" disabled={busy || !man.description.trim() || man.quantity === '' || !man.unit.trim()}>Add</button>
        <p class="mt-1 text-[11px] text-gray-400">Marked “Manual”: not read from the drawing.</p>
      </form>
    </div>
  {/if}
</div>
