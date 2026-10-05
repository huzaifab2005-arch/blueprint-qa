const API_BASE = import.meta.env.VITE_API_URL ?? '';

// ── Types ──────────────────────────────────────────────────────────────────

export type DocumentStatus = 'pending' | 'processing' | 'completed' | 'failed';
export type IssueSeverity = 'low' | 'medium' | 'high';
export type IssueType =
  | 'missing_tag'
  | 'dimension_mismatch'
  | 'unlabeled_element'
  | 'inconsistent_annotation'
  | 'missing_scale'
  | 'incomplete_detail';

export interface Document {
  id: string;
  filename: string;
  file_path: string;
  page_count: number | null;
  status: DocumentStatus;
  uploaded_at: string;
  analyzed_at: string | null;
  issue_count: number;
}

export interface DocumentList {
  documents: Document[];
  total: number;
}

export interface Issue {
  id: string;
  document_id: string;
  page_number: number;
  issue_type: IssueType;
  severity: IssueSeverity;
  description: string;
  location_hint: string | null;
  raw_ocr_text: string | null;
  created_at: string;
}

export interface IssueSummary {
  total: number;
  by_severity: Record<string, number>;
  by_type: Record<string, number>;
}

// ── Helpers ────────────────────────────────────────────────────────────────

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail ?? `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ── Documents API ──────────────────────────────────────────────────────────

export async function uploadDocument(file: File): Promise<Document> {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(`${API_BASE}/api/documents/upload`, { method: 'POST', body: form });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail ?? `HTTP ${res.status}`);
  }
  return res.json();
}

export async function listDocuments(): Promise<DocumentList> {
  return request<DocumentList>('/api/documents');
}

export async function getDocument(id: string): Promise<Document> {
  return request<Document>(`/api/documents/${id}`);
}

export async function deleteDocument(id: string): Promise<void> {
  return request<void>(`/api/documents/${id}`, { method: 'DELETE' });
}

// ── Analysis API ───────────────────────────────────────────────────────────

export async function runAnalysis(documentId: string): Promise<Issue[]> {
  return request<Issue[]>(`/api/analysis/${documentId}/run`, { method: 'POST' });
}

export async function getIssues(documentId: string): Promise<Issue[]> {
  return request<Issue[]>(`/api/analysis/${documentId}/issues`);
}

export async function getIssueSummary(documentId: string): Promise<IssueSummary> {
  return request<IssueSummary>(`/api/analysis/${documentId}/summary`);
}

// ── Export ─────────────────────────────────────────────────────────────────

export function exportIssuesToCsv(issues: Issue[], filename: string): void {
  const header = ['id', 'page_number', 'issue_type', 'severity', 'description', 'location_hint'];
  const rows = issues.map((i) =>
    [
      i.id,
      i.page_number,
      i.issue_type,
      i.severity,
      `"${i.description.replace(/"/g, '""')}"`,
      `"${(i.location_hint ?? '').replace(/"/g, '""')}"`,
    ].join(',')
  );
  const csv = [header.join(','), ...rows].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${filename.replace(/\.pdf$/i, '')}_issues.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Drawing assistant API ──────────────────────────────────────────────────

export type IndexState = 'not_indexed' | 'indexing' | 'ready' | 'failed';

export interface IndexStatus {
  status: IndexState;
  total_pages: number | null;
  pages_indexed: number;
  error: string | null;
}

export interface DrawingPage {
  page_number: number;
  sheet_number: string | null;
  sheet_title: string | null;
  label: string;
  text_source: 'text_layer' | 'ocr' | 'none';
  char_count: number;
  width: number | null;
  height: number | null;
}

export interface Evidence {
  quote: string;
  location: string;
  confirmed: boolean;
}

export interface AnswerSource {
  page_number: number;
  sheet_number: string | null;
  sheet_title: string | null;
  label: string;
  note: string;
  evidence: Evidence[];
}

export type CountStatus = 'cross_checked' | 'single_source' | 'needs_verification' | 'not_found';

/** One counted object, as a box in fractions (0..1) of the displayed page. */
export interface CountMarker {
  page_number: number;
  x: number;
  y: number;
  w: number;
  h: number;
  label: string;
}

export interface CountMethod {
  method: 'tag_instances' | 'symbol' | 'schedule_qty' | 'schedule_rows' | 'vision';
  quantity: number;
  detail: string;
  per_page: Record<string, number>;
  breakdown: Record<string, number>;
}

export interface CountResult {
  status: CountStatus;
  quantity: number | null;
  object: string;
  primary: string | null;
  methods: CountMethod[];
  markers: CountMarker[];
  definitions: { page_number: number; label: string; text: string }[];
  blocking: string[];
  pages_searched: number[];
  markers_truncated: boolean;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  verified: boolean | null;
  confidence: 'high' | 'medium' | 'low' | null;
  sources: AnswerSource[];
  pages_searched: number[];
  warnings: string[];
  count_result: CountResult | null;
  created_at: string;
}

export function startIndexing(documentId: string): Promise<IndexStatus> {
  return request<IndexStatus>(`/api/assistant/${documentId}/index`, { method: 'POST' });
}

export function getIndexStatus(documentId: string): Promise<IndexStatus> {
  return request<IndexStatus>(`/api/assistant/${documentId}/index`);
}

export function listPages(documentId: string): Promise<DrawingPage[]> {
  return request<DrawingPage[]>(`/api/assistant/${documentId}/pages`);
}

export function pageImageUrl(documentId: string, pageNumber: number): string {
  return `${API_BASE}/api/assistant/${documentId}/pages/${pageNumber}/image`;
}

export function getMessages(documentId: string): Promise<ChatMessage[]> {
  return request<ChatMessage[]>(`/api/assistant/${documentId}/messages`);
}

export function askQuestion(
  documentId: string,
  question: string
): Promise<{ question: ChatMessage; answer: ChatMessage }> {
  return request(`/api/assistant/${documentId}/ask`, {
    method: 'POST',
    body: JSON.stringify({ question }),
  });
}

export function clearMessages(documentId: string): Promise<void> {
  return request<void>(`/api/assistant/${documentId}/messages`, { method: 'DELETE' });
}

// ── Search and visual navigation ───────────────────────────────────────────

export interface Snippet {
  text: string;
  spans: [number, number][];
}

export interface SearchResult {
  page_number: number;
  label: string;
  sheet_number: string | null;
  sheet_title: string | null;
  score: number;
  match_count: number;
  kind: 'text' | 'sheet' | 'page';
  title_match: boolean;
  snippets: Snippet[];
}

export interface SearchResponse {
  query: string;
  mode: 'all' | 'partial' | 'none';
  suggestion: string | null;
  total_pages: number;
  results: SearchResult[];
}

/** A region of a page as fractions (0..1) of the displayed page. */
export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
  text: string;
}

export interface SheetReference {
  target_page: number;
  target_label: string;
  target_title: string | null;
  text: string;
  box: Box;
}

export function searchDocument(documentId: string, q: string, limit = 40): Promise<SearchResponse> {
  return request<SearchResponse>(
    `/api/assistant/${documentId}/search?q=${encodeURIComponent(q)}&limit=${limit}`
  );
}

export function getHighlights(
  documentId: string,
  pageNumber: number,
  q: string,
  phrases: string[] = []
): Promise<{ page_number: number; boxes: Box[] }> {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  for (const p of phrases.slice(0, 10)) params.append('phrase', p);
  return request(`/api/assistant/${documentId}/pages/${pageNumber}/highlights?${params}`);
}

export function getReferences(documentId: string, pageNumber: number): Promise<SheetReference[]> {
  return request<SheetReference[]>(`/api/assistant/${documentId}/pages/${pageNumber}/references`);
}

export function thumbnailUrl(documentId: string, pageNumber: number, width = 240): string {
  return `${API_BASE}/api/assistant/${documentId}/pages/${pageNumber}/thumbnail?w=${width}`;
}

// ── Measurement ────────────────────────────────────────────────────────────

export type ScaleStatus = 'verified' | 'measured' | 'calibrated' | 'stated' | 'conflict';

export interface SheetScale {
  index: number;
  ratio: number;
  text: string;
  source: 'stated' | 'measured' | 'calibrated';
  status: ScaleStatus;
  support: number;
  system: 'imperial' | 'metric';
  calibration_id: string | null;
}

export interface SheetScales {
  page_number: number;
  width_pt: number;
  height_pt: number;
  scales: SheetScale[];
  primary: number | null;
  notes: string[];
  dimension_samples: number;
}

export type MeasureKind = 'length' | 'polyline' | 'area';

export interface Measurement {
  id: string;
  page_number: number;
  kind: MeasureKind;
  label: string;
  points: [number, number][];
  value_in: number | null;
  value_sqin: number | null;
  display: string;
  display_other: string;
  perimeter_display: string | null;
  uncertainty_in: number;
  uncertainty_display: string;
  scale_ratio: number;
  scale_text: string;
  scale_status: ScaleStatus;
  warnings: string[];
  created_at: string;
}

export const getScale = (documentId: string, page: number) =>
  request<SheetScales>(`/api/measure/${documentId}/pages/${page}/scale`);

export const getSnapPoints = (documentId: string, page: number) =>
  request<{ page_number: number; count: number; points: [number, number][] }>(
    `/api/measure/${documentId}/pages/${page}/snap`
  );

export const listMeasurements = (documentId: string, page: number) =>
  request<Measurement[]>(`/api/measure/${documentId}/measurements?page=${page}`);

export const createMeasurement = (
  documentId: string,
  page: number,
  body: { kind: MeasureKind; points: [number, number][]; snapped: boolean[]; label?: string; scale_index?: number | null }
) =>
  request<Measurement>(`/api/measure/${documentId}/pages/${page}/measurements`, {
    method: 'POST',
    body: JSON.stringify(body),
  });

export const renameMeasurement = (documentId: string, id: string, label: string) =>
  request<Measurement>(`/api/measure/${documentId}/measurements/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ label }),
  });

export const deleteMeasurement = (documentId: string, id: string) =>
  request<void>(`/api/measure/${documentId}/measurements/${id}`, { method: 'DELETE' });

export const createCalibration = (documentId: string, page: number, points: [number, number][], length: string) =>
  request<SheetScales>(`/api/measure/${documentId}/pages/${page}/calibrations`, {
    method: 'POST',
    body: JSON.stringify({ points, length }),
  });

export const deleteCalibration = (documentId: string, id: string) =>
  request<void>(`/api/measure/${documentId}/calibrations/${id}`, { method: 'DELETE' });
