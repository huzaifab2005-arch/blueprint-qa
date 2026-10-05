<script lang="ts">
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import {
    createCalibration, createMeasurement, deleteCalibration, deleteMeasurement, getScale, getSnapPoints,
    listMeasurements, renameMeasurement,
  } from '$lib/api';
  import type { MeasureKind, Measurement, ScaleStatus, SheetScales } from '$lib/api';
  import { formatArea, formatLength } from '$lib/units';

  export let documentId: string;
  export let pageNumber: number;

  const dispatch = createEventDispatcher<{ exit: void }>();

  type Tool = 'none' | 'length' | 'polyline' | 'area' | 'calibrate';
  type Pt = [number, number];

  let tool: Tool = 'none';
  let svg: SVGSVGElement;
  let size = { w: 1, h: 1 };
  let ro: ResizeObserver | undefined;

  let scales: SheetScales | null = null;
  let scaleIndex: number | null = null;          // null = let the server pick by position
  let measurements: Measurement[] = [];
  let snapPts: Pt[] = [];
  let grid = new Map<string, Pt[]>();
  const CELL = 0.01;

  let pts: Pt[] = [];
  let snappedFlags: boolean[] = [];
  let cursor: Pt | null = null;
  let cursorSnapped = false;
  let altDown = false;
  let error = '';
  let busy = false;
  let calLength = '';
  let pendingCal: Pt[] | null = null;
  let focusId: string | null = null;
  let token = 0;

  const statusInfo: Record<ScaleStatus | 'none', { label: string; cls: string }> = {
    verified: { label: 'Verified against the sheet’s dimensions', cls: 'border-green-200 bg-green-50 text-green-800' },
    measured: { label: 'Measured from the sheet’s dimensions', cls: 'border-green-200 bg-green-50 text-green-800' },
    calibrated: { label: 'Calibrated by you', cls: 'border-green-200 bg-green-50 text-green-800' },
    stated: { label: 'Stated on the sheet, not confirmed', cls: 'border-amber-200 bg-amber-50 text-amber-800' },
    conflict: { label: 'Disagrees with the sheet’s dimensions', cls: 'border-red-200 bg-red-50 text-red-800' },
    none: { label: 'No scale found: calibrate before measuring', cls: 'border-gray-200 bg-gray-50 text-gray-700' },
  };

  $: current = scales && scales.scales.length
    ? scales.scales[scaleIndex ?? scales.primary ?? 0]
    : null;
  $: info = statusInfo[current?.status ?? 'none'];
  $: system = current?.system ?? 'imperial';
  $: drawing = tool !== 'none' && tool !== 'calibrate' ? tool : null;

  // The saved measurement list and scale belong to a sheet: reload when it changes.
  $: pageNumber, load();

  async function load() {
    const t = ++token;
    pts = []; snappedFlags = []; pendingCal = null; error = '';
    scales = null; scaleIndex = null; snapPts = []; grid = new Map();
    try {
      const [sc, ms] = await Promise.all([getScale(documentId, pageNumber), listMeasurements(documentId, pageNumber)]);
      if (t !== token) return;
      scales = sc; measurements = ms;
    } catch (e) {
      if (t === token) error = (e as Error).message;
    }
    getSnapPoints(documentId, pageNumber)
      .then((r) => {
        if (t !== token) return;
        snapPts = r.points;
        const g = new Map<string, Pt[]>();
        for (const p of r.points) {
          const k = `${Math.floor(p[0] / CELL)},${Math.floor(p[1] / CELL)}`;
          const a = g.get(k); if (a) a.push(p); else g.set(k, [p]);
        }
        grid = g;
      })
      .catch(() => {});                          // no PDF: measuring still works, just without snapping
  }

  function measure() {
    if (!svg) return;
    const r = svg.getBoundingClientRect();
    size = { w: Math.max(r.width, 1), h: Math.max(r.height, 1) };
  }

  onMount(() => {
    measure();
    ro = new ResizeObserver(measure);
    ro.observe(svg);
    window.addEventListener('keydown', onKey);
    window.addEventListener('keyup', onKeyUp);
  });
  onDestroy(() => {
    ro?.disconnect();
    window.removeEventListener('keydown', onKey);
    window.removeEventListener('keyup', onKeyUp);
  });

  // ── Pointer → page fractions, with snapping ──
  function toNorm(e: PointerEvent | MouseEvent): Pt {
    const r = svg.getBoundingClientRect();
    return [Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1), Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1)];
  }

  const SNAP_PX = 10;

  function nearestSnap(p: Pt): Pt | null {
    if (!grid.size) return null;
    const rx = Math.ceil(SNAP_PX / size.w / CELL), ry = Math.ceil(SNAP_PX / size.h / CELL);
    const cx = Math.floor(p[0] / CELL), cy = Math.floor(p[1] / CELL);
    let best: Pt | null = null, bd = SNAP_PX;
    for (let dx = -rx; dx <= rx; dx++) for (let dy = -ry; dy <= ry; dy++) {
      const cell = grid.get(`${cx + dx},${cy + dy}`);
      if (!cell) continue;
      for (const q of cell) {
        const d = Math.hypot((q[0] - p[0]) * size.w, (q[1] - p[1]) * size.h);
        if (d < bd) { bd = d; best = q; }
      }
    }
    return best;
  }

  function onMove(e: PointerEvent) {
    const p = toNorm(e);
    const s = altDown ? null : nearestSnap(p);
    cursor = s ?? p;
    cursorSnapped = !!s;
  }

  function onClick(e: MouseEvent) {
    if (tool === 'none' || busy) return;
    const p = toNorm(e);
    const s = altDown ? null : nearestSnap(p);
    const at: Pt = s ?? p;
    const last = pts[pts.length - 1];
    if (last && Math.hypot((last[0] - at[0]) * size.w, (last[1] - at[1]) * size.h) < 3) return;   // the 2nd click of a double-click
    pts = [...pts, at];
    snappedFlags = [...snappedFlags, !!s];
    error = '';
    if (tool === 'length' && pts.length === 2) finish();
    else if (tool === 'calibrate' && pts.length === 2) { pendingCal = pts; }
  }

  function onKey(e: KeyboardEvent) {
    if ((e.target as HTMLElement)?.tagName === 'INPUT') return;
    if (e.key === 'Alt') altDown = true;
    if (e.key === 'Escape') {
      if (pts.length || pendingCal) cancel(); else if (tool !== 'none') tool = 'none'; else dispatch('exit');
    } else if (e.key === 'Enter' && drawing) finish();
    else if (e.key === 'Backspace' && pts.length) { pts = pts.slice(0, -1); snappedFlags = snappedFlags.slice(0, -1); }
  }
  const onKeyUp = (e: KeyboardEvent) => { if (e.key === 'Alt') altDown = false; };

  function cancel() { pts = []; snappedFlags = []; pendingCal = null; calLength = ''; error = ''; }

  function pick(t: Tool) { cancel(); tool = tool === t ? 'none' : t; }

  async function finish() {
    if (!drawing && tool !== 'length') return;
    const kind = tool as MeasureKind;
    const need = kind === 'area' ? 3 : 2;
    if (pts.length < need) { error = kind === 'area' ? 'An area needs at least three points.' : 'Place at least two points.'; return; }
    busy = true;
    try {
      const m = await createMeasurement(documentId, pageNumber, {
        kind, points: pts, snapped: snappedFlags, scale_index: scaleIndex,
      });
      measurements = [...measurements, m];
      focusId = m.id;
      cancel();
    } catch (e) {
      error = (e as Error).message;
    } finally { busy = false; }
  }

  async function submitCalibration() {
    if (!pendingCal) return;
    busy = true;
    try {
      scales = await createCalibration(documentId, pageNumber, pendingCal, calLength);
      scaleIndex = null;
      cancel();
      tool = 'none';
    } catch (e) { error = (e as Error).message; } finally { busy = false; }
  }

  async function removeCalibration() {
    if (!current?.calibration_id) return;
    await deleteCalibration(documentId, current.calibration_id);
    scales = await getScale(documentId, pageNumber);
    scaleIndex = null;
  }

  async function remove(m: Measurement) {
    await deleteMeasurement(documentId, m.id);
    measurements = measurements.filter((x) => x.id !== m.id);
  }

  async function rename(m: Measurement, label: string) {
    if (label === m.label) return;
    const u = await renameMeasurement(documentId, m.id, label);
    measurements = measurements.map((x) => (x.id === m.id ? u : x));
  }

  // ── Live preview (the saved value comes from the server and is authoritative) ──
  function lengthIn(path: Pt[]): number {
    if (!scales || !current) return 0;
    let pt = 0;
    for (let i = 1; i < path.length; i++) {
      pt += Math.hypot((path[i][0] - path[i - 1][0]) * scales.width_pt, (path[i][1] - path[i - 1][1]) * scales.height_pt);
    }
    return (pt / 72) * current.ratio;
  }
  function areaSqIn(path: Pt[]): number {
    if (!scales || !current || path.length < 3) return 0;
    let s = 0;
    for (let i = 0; i < path.length; i++) {
      const [x1, y1] = path[i], [x2, y2] = path[(i + 1) % path.length];
      s += x1 * scales.width_pt * y2 * scales.height_pt - x2 * scales.width_pt * y1 * scales.height_pt;
    }
    return (Math.abs(s) / 2 / 72 ** 2) * current.ratio ** 2;
  }

  $: previewPath = drawing && cursor ? [...pts, cursor] : pts;
  $: preview = (() => {
    if (!current || previewPath.length < 2) return '';
    if (tool === 'area') return previewPath.length >= 3 ? formatArea(areaSqIn(previewPath), system) : '';
    return formatLength(lengthIn(previewPath), system);
  })();

  const px = (p: Pt) => `${p[0] * size.w},${p[1] * size.h}`;
  const mid = (a: Pt, b: Pt): Pt => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const centroid = (path: Pt[]): Pt => [path.reduce((s, p) => s + p[0], 0) / path.length, path.reduce((s, p) => s + p[1], 0) / path.length];
  const labelAt = (m: Measurement): Pt => (m.kind === 'area' ? centroid(m.points) : mid(m.points[0], m.points[m.points.length - 1]));
  const badge: Record<ScaleStatus, string> = {
    verified: 'bg-green-100 text-green-800', measured: 'bg-green-100 text-green-800', calibrated: 'bg-green-100 text-green-800',
    stated: 'bg-amber-100 text-amber-800', conflict: 'bg-red-100 text-red-800',
  };
</script>

<!-- Drawing surface: only intercepts the pointer while a tool is active -->
<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<svg
  bind:this={svg}
  class="absolute inset-0 h-full w-full {tool !== 'none' ? 'cursor-crosshair' : 'pointer-events-none'}"
  viewBox="0 0 {size.w} {size.h}"
  on:pointermove={onMove}
  on:pointerleave={() => (cursor = null)}
  on:click={onClick}
  on:dblclick={() => drawing && finish()}
  data-testid="measure-surface"
>
  {#each measurements as m (m.id)}
    {@const path = m.points.map(px).join(' ')}
    {@const hot = focusId === m.id}
    {#if m.kind === 'area'}
      <polygon points={path} fill="rgba(37,99,235,0.15)" stroke={hot ? '#1d4ed8' : '#2563eb'} stroke-width={hot ? 3 : 2} />
    {:else}
      <polyline points={path} fill="none" stroke={hot ? '#1d4ed8' : '#2563eb'} stroke-width={hot ? 3 : 2} />
      {#each m.points as p}<circle cx={p[0] * size.w} cy={p[1] * size.h} r="3" fill="#2563eb" />{/each}
    {/if}
    {@const at = labelAt(m)}
    <text x={at[0] * size.w} y={at[1] * size.h - 6} text-anchor="middle" font-size="13" font-weight="700" fill="#1e3a8a"
      stroke="white" stroke-width="3.5" paint-order="stroke">{m.label ? m.label + ': ' : ''}{m.display}</text>
  {/each}

  {#if pts.length > 0}
    {#if tool === 'area'}
      <polygon points={previewPath.map(px).join(' ')} fill="rgba(234,88,12,0.12)" stroke="#ea580c" stroke-width="2" stroke-dasharray="5 4" />
    {:else}
      <polyline points={previewPath.map(px).join(' ')} fill="none" stroke="#ea580c" stroke-width="2" stroke-dasharray="5 4" />
    {/if}
    {#each pts as p, i}<circle cx={p[0] * size.w} cy={p[1] * size.h} r="4" fill={snappedFlags[i] ? '#16a34a' : '#ea580c'} stroke="white" stroke-width="1.5" />{/each}
  {/if}
  {#if cursor && tool !== 'none'}
    <circle cx={cursor[0] * size.w} cy={cursor[1] * size.h} r={cursorSnapped ? 7 : 4}
      fill="none" stroke={cursorSnapped ? '#16a34a' : '#6b7280'} stroke-width="2" />
    {#if preview && tool !== 'calibrate'}
      <text x={cursor[0] * size.w + 12} y={cursor[1] * size.h - 10} font-size="13" font-weight="700" fill="#9a3412"
        stroke="white" stroke-width="3.5" paint-order="stroke">{preview}</text>
    {/if}
  {/if}
</svg>

<!-- Panel -->
<aside class="fixed bottom-4 right-4 z-[60] flex max-h-[75vh] w-80 flex-col overflow-hidden rounded-xl border border-gray-200 bg-white text-sm shadow-2xl" data-testid="measure-panel">
  <div class="flex items-center justify-between border-b border-gray-100 px-3 py-2">
    <span class="font-bold text-gray-800">Measure</span>
    <button class="text-xs text-gray-500 hover:text-gray-800" on:click={() => dispatch('exit')}>Done</button>
  </div>

  <div class="overflow-y-auto p-3">
    <!-- Scale: always in view, because everything below depends on it -->
    <div class="rounded-lg border px-2.5 py-2 text-xs {info.cls}" data-testid="scale-banner">
      <p class="font-semibold">{current ? current.text : 'No scale found'}</p>
      <p class="mt-0.5">{info.label}{current && current.support && current.status !== 'calibrated' ? ` · ${current.support} dimension${current.support === 1 ? '' : 's'} agree` : ''}</p>
      {#if scales && scales.scales.length > 1}
        <label class="mt-1.5 block">
          <span class="text-[11px] opacity-75">Scale to use</span>
          <select class="mt-0.5 w-full rounded border border-gray-300 bg-white px-1 py-0.5 text-xs text-gray-800" bind:value={scaleIndex}>
            <option value={null}>Nearest to what I measure</option>
            {#each scales.scales as s}<option value={s.index}>{s.text} ({s.status})</option>{/each}
          </select>
        </label>
      {/if}
      {#if current?.calibration_id}
        <button class="mt-1 text-[11px] underline" on:click={removeCalibration}>Remove this calibration</button>
      {/if}
    </div>
    {#if scales}
      {#each scales.notes as n}<p class="mt-1.5 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-800">{n}</p>{/each}
    {/if}

    <!-- Tools -->
    <div class="mt-3 grid grid-cols-2 gap-1.5">
      <button class="btn-secondary text-xs {tool === 'length' ? 'ring-2 ring-orange-400' : ''}" disabled={!current} on:click={() => pick('length')}>Length</button>
      <button class="btn-secondary text-xs {tool === 'polyline' ? 'ring-2 ring-orange-400' : ''}" disabled={!current} on:click={() => pick('polyline')}>Path</button>
      <button class="btn-secondary text-xs {tool === 'area' ? 'ring-2 ring-orange-400' : ''}" disabled={!current} on:click={() => pick('area')}>Area</button>
      <button class="btn-secondary text-xs {tool === 'calibrate' ? 'ring-2 ring-orange-400' : ''}" on:click={() => pick('calibrate')}>Calibrate</button>
    </div>
    <p class="mt-1.5 text-[11px] text-gray-500">
      {#if tool === 'none'}Pick a tool, then click on the sheet. Clicks snap to the drawing’s own corners (green); hold Alt to place freely.
      {:else if tool === 'length'}Click both ends.
      {:else if tool === 'polyline'}Click each corner; Enter or double-click to finish; Backspace undoes.
      {:else if tool === 'area'}Click each corner; Enter or double-click to close; Backspace undoes.
      {:else}Click both ends of a length you know, then enter it.{/if}
    </p>

    {#if pendingCal}
      <form class="mt-2 flex gap-1.5" on:submit|preventDefault={submitCalibration}>
        <input bind:value={calLength} placeholder={`known length, e.g. 12'-7", 24", 1200 mm`} aria-label="Known length"
          class="min-w-0 flex-1 rounded border border-gray-300 px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
        <button class="btn-primary text-xs" type="submit" disabled={busy || !calLength.trim()}>Set scale</button>
      </form>
    {/if}
    {#if error}<p class="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-700" role="alert">{error}</p>{/if}

    <!-- Saved measurements -->
    <h3 class="mt-4 text-[11px] font-semibold uppercase tracking-wide text-gray-400">This sheet ({measurements.length})</h3>
    {#if measurements.length === 0}<p class="mt-1 text-xs text-gray-400">Nothing measured yet.</p>{/if}
    <ul class="mt-1 space-y-2">
      {#each measurements as m (m.id)}
        <li class="rounded-lg border border-gray-200 p-2 {focusId === m.id ? 'ring-2 ring-blue-300' : ''}" data-testid="measurement-item">
          <div class="flex items-baseline justify-between gap-2">
            <button class="text-left" on:click={() => (focusId = m.id)}>
              <span class="text-base font-bold tabular-nums text-gray-900">{m.display}</span>
              {#if m.uncertainty_display}<span class="ml-1 text-[11px] text-gray-500">{m.uncertainty_display}</span>{/if}
            </button>
            <button class="text-xs text-gray-400 hover:text-red-600" aria-label="Delete measurement" on:click={() => remove(m)}>✕</button>
          </div>
          <p class="text-[11px] text-gray-500">
            {m.display_other}{m.kind === 'area' && m.perimeter_display ? ` · perimeter ${m.perimeter_display}` : ''}
          </p>
          <input class="mt-1 w-full rounded border border-transparent px-1 py-0.5 text-xs hover:border-gray-200 focus:border-blue-300 focus:outline-none"
            placeholder="Add a label" value={m.label} on:change={(e) => rename(m, e.currentTarget.value)} aria-label="Label" />
          <p class="mt-0.5 text-[11px]">
            <span class="rounded-full px-1.5 py-0.5 font-semibold {badge[m.scale_status]}">{m.scale_status}</span>
            <span class="text-gray-400"> {m.scale_text}</span>
          </p>
          {#each m.warnings as w}<p class="mt-1 rounded bg-amber-50 px-1.5 py-1 text-[11px] text-amber-800">{w}</p>{/each}
        </li>
      {/each}
    </ul>
  </div>
</aside>
