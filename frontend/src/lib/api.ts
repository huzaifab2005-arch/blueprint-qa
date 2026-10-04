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

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  verified: boolean | null;
  confidence: 'high' | 'medium' | 'low' | null;
  sources: AnswerSource[];
  pages_searched: number[];
  warnings: string[];
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
