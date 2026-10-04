<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import type { Document, Issue, IssueSummary } from '$lib/api';
  import { runAnalysis, getDocument, getIssues, getIssueSummary, exportIssuesToCsv } from '$lib/api';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import IssueList from '$lib/components/IssueList.svelte';
  import Assistant from '$lib/components/Assistant.svelte';
  import DrawingSearch from '$lib/components/DrawingSearch.svelte';

  let tab: 'qa' | 'assistant' | 'search' = 'qa';

  let document: Document | null = null;
  let issues: Issue[] = [];
  let summary: IssueSummary | null = null;
  let loadError = '';
  let loading = true;

  onMount(async () => {
    const id = $page.params.id;
    try {
      const [doc, iss, sum] = await Promise.all([
        getDocument(id),
        getIssues(id).catch(() => [] as Issue[]),
        getIssueSummary(id).catch(() => null),
      ]);
      document = doc;
      issues = iss;
      summary = sum;
    } catch (err) {
      loadError = (err as Error).message;
    } finally {
      loading = false;
    }
  });

  let analyzing = false;
  let analyzeError = '';

  function formatDate(iso: string) {
    return new Date(iso).toLocaleDateString('en-US', {
      month: 'long', day: 'numeric', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  }

  async function startAnalysis() {
    if (!document) return;
    analyzeError = '';
    analyzing = true;
    document = { ...document, status: 'processing' };
    try {
      const result = await runAnalysis(document.id);
      issues = result;
      const updatedSummary = await getIssueSummary(document.id);
      summary = updatedSummary;
      document = {
        ...document,
        status: 'completed',
        issue_count: result.length,
        analyzed_at: new Date().toISOString(),
      };
    } catch (err) {
      analyzeError = (err as Error).message;
      document = { ...document, status: 'failed' };
    } finally {
      analyzing = false;
    }
  }

  async function rerunAnalysis() {
    issues = [];
    summary = null;
    await startAnalysis();
  }

  const severityColors: Record<string, string> = {
    high:   'bg-red-100 text-red-800',
    medium: 'bg-amber-100 text-amber-800',
    low:    'bg-green-100 text-green-800',
  };

  const issueTypeLabels: Record<string, string> = {
    missing_tag:             'Missing Tag',
    dimension_mismatch:      'Dimension Mismatch',
    unlabeled_element:       'Unlabeled Element',
    inconsistent_annotation: 'Inconsistent Annotation',
    missing_scale:           'Missing Scale',
    incomplete_detail:       'Incomplete Detail',
  };
</script>

<svelte:head>
  <title>{document ? `${document.filename} — Blueprint QA` : 'Blueprint QA'}</title>
</svelte:head>

{#if loading}
  <div class="flex items-center justify-center py-24">
    <svg class="h-10 w-10 animate-spin text-blue-500" fill="none" viewBox="0 0 24 24">
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
    </svg>
  </div>
{:else if loadError}
  <div class="card p-8 text-center text-red-600">{loadError}</div>
{:else if document}
<div class="flex flex-col gap-8">
  <!-- Breadcrumb -->
  <nav class="text-sm text-gray-500">
    <a href="/" class="hover:text-blue-600 transition-colors">Documents</a>
    <span class="mx-1.5">/</span>
    <span class="text-gray-800 font-medium truncate">{document.filename}</span>
  </nav>

  <!-- Header card -->
  <div class="card p-6 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
    <div class="flex items-start gap-3 min-w-0">
      <svg class="h-8 w-8 flex-shrink-0 text-red-500 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
        <path fill-rule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 6a1 1 0 011-1h6a1 1 0 110 2H7a1 1 0 01-1-1zm1 3a1 1 0 100 2h6a1 1 0 100-2H7z" clip-rule="evenodd"/>
      </svg>
      <div class="min-w-0">
        <h1 class="text-lg font-bold text-gray-900 truncate">{document.filename}</h1>
        <div class="flex flex-wrap gap-x-3 gap-y-1 mt-1 text-xs text-gray-500">
          {#if document.page_count}
            <span>{document.page_count} page{document.page_count !== 1 ? 's' : ''}</span>
          {/if}
          <span>Uploaded {formatDate(document.uploaded_at)}</span>
          {#if document.analyzed_at}
            <span>Analyzed {formatDate(document.analyzed_at)}</span>
          {/if}
        </div>
      </div>
    </div>

    <div class="flex items-center gap-3 flex-shrink-0">
      <StatusBadge status={document.status} />
      {#if document.status === 'completed'}
        <button class="btn-secondary text-xs" on:click={rerunAnalysis} disabled={analyzing}>
          Re-run Analysis
        </button>
        <button
          class="btn-secondary text-xs"
          on:click={() => exportIssuesToCsv(issues, document.filename)}
          disabled={issues.length === 0}
          title="Export issues as CSV"
        >
          <svg class="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
          </svg>
          Export CSV
        </button>
      {:else if document.status === 'pending' || document.status === 'failed'}
        <button class="btn-primary text-xs" on:click={startAnalysis} disabled={analyzing}>
          {document.status === 'failed' ? 'Retry Analysis' : 'Run Analysis'}
        </button>
      {/if}
    </div>
  </div>

  <!-- Tabs -->
  <div class="flex gap-1 border-b border-gray-200" role="tablist">
    <button
      role="tab" aria-selected={tab === 'qa'}
      class="-mb-px border-b-2 px-4 py-2 text-sm font-medium {tab === 'qa' ? 'border-blue-600 text-blue-700' : 'border-transparent text-gray-500 hover:text-gray-800'}"
      on:click={() => (tab = 'qa')}
    >QA Report</button>
    <button
      role="tab" aria-selected={tab === 'assistant'}
      class="-mb-px border-b-2 px-4 py-2 text-sm font-medium {tab === 'assistant' ? 'border-blue-600 text-blue-700' : 'border-transparent text-gray-500 hover:text-gray-800'}"
      on:click={() => (tab = 'assistant')}
    >Drawing Assistant</button>
    <button
      role="tab" aria-selected={tab === 'search'}
      class="-mb-px border-b-2 px-4 py-2 text-sm font-medium {tab === 'search' ? 'border-blue-600 text-blue-700' : 'border-transparent text-gray-500 hover:text-gray-800'}"
      on:click={() => (tab = 'search')}
    >Search &amp; Sheets</button>
  </div>

  {#if tab === 'assistant'}
    <Assistant documentId={document.id} />
  {/if}

  {#if tab === 'search'}
    <DrawingSearch documentId={document.id} />
  {/if}

  {#if tab === 'qa'}
  {#if analyzeError}
    <div class="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
      {analyzeError}
    </div>
  {/if}

  <!-- Processing state -->
  {#if document.status === 'processing'}
    <div class="card p-12 flex flex-col items-center gap-4 text-center">
      <svg class="h-12 w-12 animate-spin text-blue-500" fill="none" viewBox="0 0 24 24">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
      </svg>
      <div>
        <p class="text-base font-semibold text-gray-700">Analyzing drawing…</p>
        <p class="text-sm text-gray-400 mt-1">Running OCR and AI analysis on each page. This may take a minute.</p>
      </div>
    </div>
  {/if}

  <!-- Pending state -->
  {#if document.status === 'pending'}
    <div class="card p-12 flex flex-col items-center gap-4 text-center">
      <svg class="h-14 w-14 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/>
      </svg>
      <div>
        <p class="text-base font-semibold text-gray-700">Ready for analysis</p>
        <p class="text-sm text-gray-400 mt-1">Click "Run Analysis" to start QA inspection.</p>
      </div>
      <button class="btn-primary" on:click={startAnalysis}>
        Run Analysis
      </button>
    </div>
  {/if}

  <!-- Failed state -->
  {#if document.status === 'failed'}
    <div class="card p-12 flex flex-col items-center gap-4 text-center">
      <svg class="h-14 w-14 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
      </svg>
      <div>
        <p class="text-base font-semibold text-gray-700">Analysis failed</p>
        <p class="text-sm text-gray-400 mt-1">
          {analyzeError || 'An error occurred during QA analysis. You can retry below.'}
        </p>
      </div>
      <button class="btn-primary" on:click={startAnalysis} disabled={analyzing}>
        Retry Analysis
      </button>
    </div>
  {/if}

  <!-- Completed state -->
  {#if document.status === 'completed'}
    {#if summary}
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <div class="card p-4 text-center">
          <p class="text-3xl font-extrabold text-gray-900">{summary.total}</p>
          <p class="text-xs text-gray-500 mt-1">Total Issues</p>
        </div>
        {#each ['high','medium','low'] as sev}
          <div class="card p-4 text-center">
            <p class="text-3xl font-extrabold {sev === 'high' ? 'text-red-600' : sev === 'medium' ? 'text-amber-500' : 'text-green-600'}">
              {summary.by_severity[sev] ?? 0}
            </p>
            <p class="text-xs text-gray-500 mt-1 capitalize">{sev} Severity</p>
          </div>
        {/each}
      </div>

      {#if Object.keys(summary.by_type).length > 0}
        <div class="card p-5">
          <h3 class="text-sm font-semibold text-gray-700 mb-3">Issues by Type</h3>
          <div class="flex flex-wrap gap-2">
            {#each Object.entries(summary.by_type).sort((a, b) => b[1] - a[1]) as [type, count]}
              <span class="inline-flex items-center gap-1.5 rounded-full bg-blue-50 border border-blue-200 px-3 py-1 text-xs font-medium text-blue-700">
                {issueTypeLabels[type] ?? type}
                <span class="bg-blue-200 text-blue-800 rounded-full px-1.5 py-0.5 text-xs font-bold">{count}</span>
              </span>
            {/each}
          </div>
        </div>
      {/if}
    {/if}

    {#if issues.length === 0}
      <div class="card p-12 text-center">
        <svg class="mx-auto h-14 w-14 text-green-400 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
        </svg>
        <p class="text-base font-semibold text-gray-700">No issues found</p>
        <p class="text-sm text-gray-400 mt-1">This drawing passed QA inspection.</p>
      </div>
    {:else}
      <div>
        <h2 class="text-base font-bold text-gray-800 mb-4">Issues ({issues.length})</h2>
        <IssueList {issues} />
      </div>
    {/if}
  {/if}
  {/if}
</div>
{/if}
