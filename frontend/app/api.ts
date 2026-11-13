// Browser traffic always uses the same-origin runtime BFF. The private FastAPI target and its
// service token are resolved only by the Next server at request time.
import {
  parseSetupStatus,
  type InstallationMode,
  type SetupStatus,
} from "./setup/setup-status.ts";
import {
  applyUnauthorizedNavigation,
  type ParseResponseOptions,
  PRESERVE_UNAUTHORIZED_RESPONSE,
} from "./api-response-policy.ts";
import {
  parseWebSessionStatus,
  type WebSessionStatus,
} from "./auth-status.ts";

export type { WebSessionStatus } from "./auth-status.ts";

export const API_BASE = "/api/v2";

import { apiFetch } from "./api-fetch.ts";


export type SetupState =
  | "NEW"
  | "MIGRATING"
  | "AWAITING_OWNER"
  | "CONFIGURING"
  | "VERIFYING_RUNTIME"
  | "READY"
  | "RECOVERABLE_FAILURE"
  | "BLOCKED";

export async function setupLocalOwner(input: {
  bootstrap_token: string;
  installation_mode: InstallationMode;
  workspace_name: string;
  expected_state_version: number;
  password: string;
  llm_credential?: LlmCredentialWrite;
}): Promise<{
  setup_state: SetupState;
  state_version: number;
  installation_mode: InstallationMode;
  replayed: boolean;
  llm_credential: Omit<LlmCredentialMetadata, "updated_at"> | null;
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/setup/owner`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
    PRESERVE_UNAUTHORIZED_RESPONSE,
  );
}

export async function getSetupStatus(): Promise<SetupStatus> {
  const payload = await parseResponse<unknown>(
    await apiFetch(`${API_BASE}/setup/status`, { cache: "no-store" }),
    PRESERVE_UNAUTHORIZED_RESPONSE,
  );
  const status = parseSetupStatus(payload);
  if (status === null) throw new Error("配置状态响应无效。");
  return status;
}

export type LlmCredentialWrite = {
  provider: string;
  api_key: string;
  api_base: string;
  model: string;
};

export type LlmCredentialMetadata = {
  id: string;
  provider: string;
  api_base: string | null;
  model: string | null;
  configured: boolean;
  masked_suffix: string | null;
  requires_reentry: boolean;
  updated_at: string;
};

export type DefaultLlmCredential = {
  storage_mode: "protected" | "environment";
  credential: LlmCredentialMetadata | null;
};

export async function getDefaultLlmCredential(): Promise<DefaultLlmCredential> {
  return parseResponse(await apiFetch(`${API_BASE}/credentials/default`, { cache: "no-store" }));
}

export async function replaceDefaultLlmCredential(
  value: LlmCredentialWrite,
): Promise<LlmCredentialMetadata> {
  return parseResponse(await apiFetch(`${API_BASE}/credentials/default`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  }));
}

export type PaperTask = {
  id: string;
  task_type: string;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  priority: number;
  attempt_count: number;
  max_attempts: number;
  error_code: string | null;
  error_message: string | null;
  download_progress: {
    phase: "DOWNLOADING" | "VALIDATING" | "RESTART_REQUIRED" | string;
    bytes_downloaded: number;
    bytes_total: number | null;
    bytes_remaining: number | null;
    percent: number | null;
    resumable: boolean;
    resumed_bytes: number;
    updated_at: string | null;
  } | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type PaperTaskDetail = {
  id: string;
  task_type: string;
  status: string;
  description: string;
  execution_kind: "network" | "local" | "llm";
  uses_llm: boolean;
  engine: string | null;
  provider: string | null;
  model: string | null;
  reasoning_effort: string | null;
  renderer: string | null;
  renderer_error: string | null;
  target_block_id: string | null;
  target_block_type: string | null;
  target_location: string | null;
  trigger_reason: string | null;
  complexity_reasons: string[];
  result_summary: string | null;
  safety_notes: string[];
  system_prompt: string | null;
  source_latex: string | null;
  source_text: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  estimated_cost_microusd: number | null;
  cache_hit: boolean | null;
  task_error_code: string | null;
  task_error_message: string | null;
};

export type PaperSource = {
  source_type: string;
  storage_key: string;
  sha256: string;
  byte_size: number;
  mime_type: string | null;
  retrieved_at: string;
};

export type Paper = {
  id: string;
  arxiv_id: string;
  arxiv_version: string;
  canonical_arxiv_id: string;
  title: string;
  abstract: string | null;
  abstract_block_id: string | null;
  abstract_render_payload: Record<string, unknown> | null;
  submitted_at: string | null;
  processing_stage: string;
  processing_health: string;
  reading_status: string;
  is_favorite: boolean;
  pinned_at: string | null;
  last_opened_at: string | null;
  created_at: string;
  updated_at: string;
  authors: Array<{ position: number; name: string }>;
  sources: PaperSource[];
  tasks: PaperTask[];
};

type ApiError = { detail?: unknown; code?: unknown; message?: unknown; request_id?: unknown };

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly requestId: string | null;

  constructor(
    status: number,
    message: string,
    metadata: { code?: string; requestId?: string } = {},
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = metadata.code ?? null;
    this.requestId = metadata.requestId ?? null;
  }
}

async function parseResponse<T>(
  response: Response,
  options: ParseResponseOptions = {},
): Promise<T> {
  if (!response.ok) {
    if (typeof window !== "undefined") {
      applyUnauthorizedNavigation(response.status, options, window.location);
    }
    const raw: unknown = await response.json().catch(() => ({}));
    const payload: ApiError = typeof raw === "object" && raw !== null && !Array.isArray(raw) ? raw : {};
    throw new ApiRequestError(
      response.status,
      typeof payload.message === "string" && payload.message.length > 0
        ? payload.message
        : typeof payload.detail === "string"
          ? payload.detail
          : `Request failed with HTTP ${response.status}.`,
      {
        code: typeof payload.code === "string" ? payload.code : undefined,
        requestId: typeof payload.request_id === "string" ? payload.request_id : undefined,
      },
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export type WebLoginResult = {
  authenticated: true;
  user_id: string;
  active_workspace_id: string;
  expires_at: string;
};

export async function loginWebSession(input: {
  username?: string;
  password: string;
  workspace_id?: string;
}): Promise<WebLoginResult> {
  return parseResponse(
    await apiFetch(`${API_BASE}/auth/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
    PRESERVE_UNAUTHORIZED_RESPONSE,
  );
}

export async function registerWebSession(input: {
  invite_token: string;
  username: string;
  display_name: string;
  password: string;
}): Promise<WebLoginResult> {
  return parseResponse(
    await apiFetch(`${API_BASE}/auth/registrations`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
    PRESERVE_UNAUTHORIZED_RESPONSE,
  );
}

export async function createAutomaticOwnerSession(): Promise<void> {
  return parseResponse<void>(
    await apiFetch("/api/auth/single-user-session", {
      method: "POST",
      credentials: "same-origin",
    }),
    PRESERVE_UNAUTHORIZED_RESPONSE,
  );
}

export async function getWebSessionStatus(): Promise<WebSessionStatus> {
  const payload = await parseResponse<unknown>(
    await apiFetch(`${API_BASE}/auth/status`, { cache: "no-store" }),
    PRESERVE_UNAUTHORIZED_RESPONSE,
  );
  const status = parseWebSessionStatus(payload);
  if (status === null) throw new Error("The authenticated session status response is invalid.");
  return status;
}

export async function logoutWebSession(): Promise<void> {
  await parseResponse<void>(await apiFetch(`${API_BASE}/auth/sessions/current`, { method: "DELETE" }));
}

export type ManagedUser = {
  id: string;
  username: string;
  display_name: string;
  status: "ACTIVE" | "DISABLED";
  workspace_id: string;
  workspace_role: string;
  is_installation_owner: boolean;
  created_at: string;
  updated_at: string;
};

export async function listManagedUsers(): Promise<ManagedUser[]> {
  const result = await parseResponse<{ users: ManagedUser[] }>(
    await apiFetch(`${API_BASE}/admin/users`, { cache: "no-store" }),
  );
  return result.users;
}

export async function disableManagedUser(userId: string): Promise<ManagedUser> {
  return parseResponse(await apiFetch(
    `${API_BASE}/admin/users/${encodeURIComponent(userId)}/disable`,
    { method: "POST" },
  ));
}

export type InvitationStatus = "ACTIVE" | "EXPIRED" | "REDEEMED" | "REVOKED";

export type InvitationSummary = {
  id: string;
  workspace_id: string;
  masked_hint: string;
  issuer_channel: "WEB_ADMIN" | "OPERATOR_CLI";
  label: string | null;
  status: InvitationStatus;
  created_at: string;
  expires_at: string;
  redeemed_at: string | null;
  redeemed_by_user_id: string | null;
  redeemed_by_username: string | null;
  redeemed_by_display_name: string | null;
  revoked_at: string | null;
};

export type CreatedInvitation = InvitationSummary & {
  registration_url: string;
};

export async function createInvitation(input: {
  expires_in_seconds: number;
  label?: string;
}): Promise<CreatedInvitation> {
  return parseResponse(await apiFetch(`${API_BASE}/admin/invitations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }));
}

export async function listInvitations(includeInactive = true): Promise<InvitationSummary[]> {
  const result = await parseResponse<{ invitations: InvitationSummary[] }>(
    await apiFetch(
      `${API_BASE}/admin/invitations?include_inactive=${includeInactive ? "true" : "false"}`,
      { cache: "no-store" },
    ),
  );
  return result.invitations;
}

export async function getInvitation(invitationId: string): Promise<InvitationSummary> {
  return parseResponse(await apiFetch(
    `${API_BASE}/admin/invitations/${encodeURIComponent(invitationId)}`,
    { cache: "no-store" },
  ));
}

export async function revokeInvitation(invitationId: string): Promise<InvitationSummary> {
  return parseResponse(await apiFetch(
    `${API_BASE}/admin/invitations/${encodeURIComponent(invitationId)}/revoke`,
    { method: "POST" },
  ));
}

export type MeProfile = {
  id: string;
  username: string;
  display_name: string;
  active_workspace_id: string;
};

export async function getMe(): Promise<MeProfile> {
  return parseResponse(await apiFetch(`${API_BASE}/me`, { cache: "no-store" }));
}

export async function updateMe(displayName: string): Promise<MeProfile> {
  return parseResponse(await apiFetch(`${API_BASE}/me`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName }),
  }));
}

export type OnboardingCredentialWrite = {
  provider: string;
  api_key: string;
  api_base?: string | null;
  model?: string | null;
};

export type OnboardingStatus = {
  status: "PENDING" | "COMPLETED";
  onboarding_required: boolean;
  username: string;
  display_name: string;
  automatic_translation_enabled: boolean;
  automatic_ai_repair_enabled: boolean;
  default_credential: LlmCredentialMetadata | null;
  completed_at: string | null;
};

export async function getOnboardingStatus(): Promise<OnboardingStatus> {
  return parseResponse(await apiFetch(`${API_BASE}/onboarding/status`, { cache: "no-store" }));
}

export async function completeOnboarding(input: {
  display_name: string;
  automatic_translation_enabled: boolean;
  automatic_ai_repair_enabled: boolean;
  credential?: OnboardingCredentialWrite | null;
}): Promise<OnboardingStatus> {
  return parseResponse(await apiFetch(`${API_BASE}/onboarding/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  }));
}

export async function importPaper(source: string): Promise<{ created: boolean; paper: Paper }> {
  const response = await apiFetch(`${API_BASE}/papers/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  });
  return parseResponse(response);
}

export async function listPapers(favoriteOnly = false): Promise<Paper[]> {
  const query = favoriteOnly ? "?favorite_only=true" : "";
  return parseResponse(await apiFetch(`${API_BASE}/papers${query}`, { cache: "no-store" }));
}

export async function updatePaperPreferences(
  paperId: string,
  preferences: { is_favorite?: boolean; is_pinned?: boolean },
): Promise<Paper> {
  return parseResponse(await apiFetch(`${API_BASE}/papers/${paperId}/preferences`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(preferences),
  }));
}

export async function markPaperOpened(paperId: string): Promise<Paper> {
  return parseResponse(await apiFetch(`${API_BASE}/papers/${paperId}/opened`, {
    method: "POST",
  }));
}

export async function deletePapers(paperIds: string[]): Promise<{
  deleted_count: number;
  deleted_ids: string[];
  deleted_files: number;
}> {
  const response = await apiFetch(`${API_BASE}/papers`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_ids: paperIds }),
  });
  return parseResponse(response);
}

export async function getPaper(paperId: string): Promise<Paper> {
  return parseResponse(await apiFetch(`${API_BASE}/papers/${paperId}`, { cache: "no-store" }));
}

export function paperSourceDownloadUrl(paperId: string, sourceType: string): string {
  return `${API_BASE}/papers/${encodeURIComponent(paperId)}/sources/${encodeURIComponent(sourceType)}/download`;
}

export async function retryPaper(paperId: string): Promise<{ retried_tasks: number; paper: Paper }> {
  const retry = async (confirmPaid: boolean): Promise<{ retried_tasks: number; paper: Paper }> => (
    parseResponse(await apiFetch(`${API_BASE}/papers/${encodeURIComponent(paperId)}/retry`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_paid: confirmPaid }),
    }))
  );
  try {
    return await retry(false);
  } catch (error) {
    if (!(error instanceof ApiRequestError)
        || error.code !== "PAID_OPERATION_CONFIRMATION_REQUIRED") throw error;
    if (typeof window === "undefined" || !window.confirm(
      "将重试你在这篇论文中失败的模型任务，可能产生模型 API 费用。是否继续？",
    )) throw error;
    return retry(true);
  }
}

export type PaperOutlineItem = {
  id: string;
  parent_id: string | null;
  ordinal_path: string;
  heading_level: number;
  display_number: string | null;
  title: string;
  title_segments: Array<Record<string, unknown>>;
};

export type ContentBlock = {
  id: string;
  parent_id: string | null;
  block_type: string;
  ordinal_path: string;
  heading_level: number | null;
  source_text: string | null;
  source_payload: Record<string, unknown>;
  render_payload: Record<string, unknown>;
  source_locator: Record<string, unknown>;
  translation: BlockTranslation | null;
};

export type BlockBookmark = {
  id: string;
  paper_id: string;
  block_id: string;
  block_type: string;
  ordinal_path: string;
  section_title: string | null;
  section_display_number: string | null;
  preview: string;
  created_at: string;
};

export async function getBlockBookmarks(paperId: string): Promise<BlockBookmark[]> {
  return parseResponse(await apiFetch(`${API_BASE}/papers/${paperId}/bookmarks`, {
    cache: "no-store",
  }));
}

export async function addBlockBookmark(
  paperId: string,
  blockId: string,
): Promise<BlockBookmark> {
  return parseResponse(await apiFetch(`${API_BASE}/papers/${paperId}/bookmarks/${blockId}`, {
    method: "PUT",
  }));
}

export async function removeBlockBookmark(paperId: string, blockId: string): Promise<void> {
  await parseResponse<void>(await apiFetch(`${API_BASE}/papers/${paperId}/bookmarks/${blockId}`, {
    method: "DELETE",
  }));
}

export type BlockTranslation = {
  id: string;
  block_id: string;
  language: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  translated_text: string | null;
  translated_payload: Record<string, unknown>;
  provider: string;
  model: string;
  prompt_version: string;
  system_prompt: string | null;
  prompt_kind: "global" | "custom" | "paper";
  target_type: string;
  cache_hit: boolean;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  estimated_cost_microusd: number | null;
  provider_request_id: string | null;
  error_type: string | null;
  error_code: string | null;
  error_message: string | null;
  updated_at: string;
};

export type TranslationProgress = {
  configured: boolean;
  target_language: string;
  provider: string | null;
  model: string | null;
  prompt_version: string;
  eligible: number;
  missing: number;
  pending: number;
  running: number;
  succeeded: number;
  failed: number;
  cache_hits: number;
  input_tokens: number;
  output_tokens: number;
  average_latency_ms: number | null;
  retry_count: number;
  recent_errors: Array<{
    block_id: string;
    error_type: string | null;
    error_code: string | null;
    error_message: string | null;
    attempt_count: number;
  }>;
};

export type ContentBlockPage = {
  items: ContentBlock[];
  next_cursor: string | null;
};

export type BibliographyPerson = {
  role: string;
  position: number;
  display_name: string;
  given_names: string[];
  family_names: string[];
  prefix_names: string[];
  suffix_names: string[];
  initials: string[];
};

export type BibliographyEntry = {
  id: string;
  citation_key: string;
  entry_type: string;
  source_path: string | null;
  fields: Record<string, unknown>;
  title: string | null;
  container_title: string | null;
  publication_year: string | null;
  doi: string | null;
  url: string | null;
  eprint: string | null;
  archive_prefix: string | null;
  isbn: string | null;
  issn: string | null;
  ordinal: number | null;
  display_label: string | null;
  formatted_text: string | null;
  render_payload: Record<string, unknown>;
  persons: BibliographyPerson[];
};

export type PaperBibliography = {
  status: "EMPTY" | "READY" | "PARTIAL" | "FAILED";
  requested_style: string | null;
  resolved_style: string | null;
  fallback_used: boolean;
  processor_version: string | null;
  source_files: string[];
  warnings: string[];
  entries: BibliographyEntry[];
};

export async function getPaperBibliography(paperId: string): Promise<PaperBibliography> {
  return parseResponse(
    await apiFetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/bibliography`,
      { cache: "no-store" },
    ),
  );
}

export type PaperMapQuestion = {
  id: string;
  question_key: string;
  question_text: string;
  anchor_node_id: string | null;
  is_initial: boolean;
  rationale: string;
  evidence_block_ids: string[];
  ordinal: number;
  status: "NOT_REQUESTED" | "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED";
  answer_generation: number;
  answer_status: string | null;
  answer_text: string | null;
  citations: Array<{ block_id: string; quote: string }>;
  provider: string | null;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  error_type: string | null;
  error_code: string | null;
  error_message: string | null;
  updated_at: string;
};

export type PaperMapNode = {
  id: string;
  question_id: string | null;
  node_key: string;
  node_type: string;
  concept_type: string | null;
  label: string;
  body: string;
  status: string | null;
  evidence: Array<{ block_id: string; quote: string }>;
  ordinal: number;
};

export type PaperMapEdge = {
  id: string;
  source_node_id: string;
  target_node_id: string;
  edge_type: string;
  label: string | null;
  is_tree: boolean;
};

export type PaperVisualMap = {
  id: string;
  paper_id: string;
  generation: number;
  status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "PARTIAL" | "FAILED" | "STALE";
  content_hash: string;
  question_library_version: string;
  primary_type: string | null;
  secondary_types: string[];
  type_confidence: number | null;
  provider: string;
  model: string;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  error_type: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  questions: PaperMapQuestion[];
  nodes: PaperMapNode[];
  edges: PaperMapEdge[];
};

export type PaperVisualMapEnvelope = {
  configured: boolean;
  provider: string | null;
  model: string | null;
  paper_map: PaperVisualMap | null;
};

export async function getPaperVisualMap(paperId: string): Promise<PaperVisualMapEnvelope> {
  return parseResponse(
    await apiFetch(`${API_BASE}/papers/${paperId}/visual-map`, { cache: "no-store" }),
  );
}

export async function generatePaperVisualMap(paperId: string): Promise<{
  map_id: string;
  task_id: string;
  generation: number;
  status: string;
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/papers/${paperId}/visual-map`, { method: "POST" }),
  );
}

export async function generatePaperMapAnswer(questionId: string): Promise<{
  map_id: string;
  question_id: string;
  task_id: string;
  generation: number;
  status: string;
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/paper-map-questions/${questionId}/answer`, { method: "POST" }),
  );
}

export async function createPaperMapFollowup(nodeId: string, question: string): Promise<{
  map_id: string;
  question_id: string;
  task_id: string;
  generation: number;
  status: string;
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/paper-map-nodes/${nodeId}/follow-ups`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    }),
  );
}

export async function createPaperMapConcept(
  nodeId: string,
  concept: { concept_type: string; label: string; body: string },
): Promise<{ node: PaperMapNode; edge: PaperMapEdge }> {
  return parseResponse(
    await apiFetch(`${API_BASE}/paper-map-nodes/${nodeId}/concepts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(concept),
    }),
  );
}

export async function createPaperMapEdge(
  mapId: string,
  sourceNodeId: string,
  targetNodeId: string,
): Promise<{ edge: PaperMapEdge }> {
  return parseResponse(
    await apiFetch(`${API_BASE}/paper-maps/${mapId}/edges`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_node_id: sourceNodeId,
        target_node_id: targetNodeId,
        label: "",
      }),
    }),
  );
}

export async function updatePaperMapEdge(
  edgeId: string,
  label: string,
): Promise<{ edge: PaperMapEdge }> {
  return parseResponse(
    await apiFetch(`${API_BASE}/paper-map-edges/${edgeId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    }),
  );
}

export async function deletePaperMapEdge(edgeId: string): Promise<void> {
  await parseResponse(
    await apiFetch(`${API_BASE}/paper-map-edges/${edgeId}`, { method: "DELETE" }),
  );
}

export async function deletePaperMapNode(nodeId: string): Promise<void> {
  await parseResponse(
    await apiFetch(`${API_BASE}/paper-map-nodes/${nodeId}`, { method: "DELETE" }),
  );
}

export type AnnotationSelector = {
  version: 1;
  block_id: string;
  start_offset: number;
  end_offset: number;
  exact_quote: string;
  prefix: string;
  suffix: string;
  text_hash?: string;
};

export type Annotation = {
  id: string;
  paper_id: string;
  block_id: string | null;
  parent_annotation_id: string | null;
  anchor_annotation_id: string | null;
  annotation_type: "ANNOTATION" | "HIGHLIGHT" | "USER_NOTE" | "AI_QUESTION" | "AI_RESPONSE";
  target: "SOURCE" | "TRANSLATION" | "BLOCK" | "PAPER";
  body: string;
  is_highlighted: boolean;
  creator_type: "USER" | "AI";
  selector: AnnotationSelector | null;
  target_translation_id: string | null;
  target_translation_input_hash: string | null;
  resolution_status: string;
  resolution_reason: string | null;
  resolved_start_offset: number | null;
  resolved_end_offset: number | null;
  revision: number;
  qa_status: "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | null;
  qa_generation: number;
  qa_error_type: string | null;
  qa_error_code: string | null;
  qa_error_message: string | null;
  qa_context: QAContextSnapshot | null;
  citations: Array<{
    block_id: string;
    target: "SOURCE" | "TRANSLATION";
    quote: string;
    translation_id: string | null;
  }>;
  provider: string | null;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  created_at: string;
  updated_at: string;
  comments: AnnotationComment[];
};

export type QAContextItem = {
  kind: string;
  content: string;
  block_id: string | null;
  target: "SOURCE" | "TRANSLATION" | null;
  translation_id: string | null;
  truncated: boolean;
};

export type QAContextSnapshot = {
  version: 1;
  generation: number;
  prompt_version: string;
  prompt_hash: string;
  context_token_budget: number;
  max_output_tokens: number;
  captured_at: string;
  user_payload: {
    question: string;
    question_id: string;
    paper_id: string;
    paper_title: string;
    target: "SOURCE" | "TRANSLATION" | "BLOCK" | "PAPER";
    target_block_id: string | null;
    contexts: QAContextItem[];
  };
};

export type AnnotationComment = {
  id: string;
  annotation_id: string;
  body: string;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type CreateAnnotationInput = {
  paper_id: string;
  block_id?: string;
  annotation_type: "ANNOTATION" | "HIGHLIGHT" | "USER_NOTE" | "AI_QUESTION";
  target: "SOURCE" | "TRANSLATION" | "BLOCK" | "PAPER";
  body?: string;
  is_highlighted?: boolean;
  selector?: AnnotationSelector;
  target_translation_id?: string;
  initial_comment?: string;
  initial_question?: string;
};

export async function getPaperAnnotations(paperId: string): Promise<Annotation[]> {
  return parseResponse(
    await apiFetch(`${API_BASE}/papers/${encodeURIComponent(paperId)}/annotations`, {
      cache: "no-store",
    }),
  );
}

export async function createAnnotation(input: CreateAnnotationInput): Promise<{
  annotation: Annotation;
  task_id: string | null;
  question: Annotation | null;
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/annotations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function createAnnotationComment(
  annotationId: string,
  body: string,
): Promise<AnnotationComment> {
  return parseResponse(
    await apiFetch(`${API_BASE}/annotations/${encodeURIComponent(annotationId)}/comments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body }),
    }),
  );
}

export async function updateAnnotationComment(
  commentId: string,
  input: { body: string; revision: number },
): Promise<AnnotationComment> {
  return parseResponse(
    await apiFetch(`${API_BASE}/annotation-comments/${encodeURIComponent(commentId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function deleteAnnotationComment(commentId: string): Promise<void> {
  const response = await apiFetch(
    `${API_BASE}/annotation-comments/${encodeURIComponent(commentId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) await parseResponse(response);
}

export async function askAnnotation(
  annotationId: string,
  body: string,
): Promise<{ question: Annotation; task_id: string }> {
  return parseResponse(
    await apiFetch(`${API_BASE}/annotations/${encodeURIComponent(annotationId)}/questions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body }),
    }),
  );
}

export async function updateAnnotation(
  annotationId: string,
  input: {
    body?: string;
    is_highlighted?: boolean;
    revision: number;
  },
): Promise<Annotation> {
  return parseResponse(
    await apiFetch(`${API_BASE}/annotations/${encodeURIComponent(annotationId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function deleteAnnotation(annotationId: string): Promise<void> {
  const response = await apiFetch(`${API_BASE}/annotations/${encodeURIComponent(annotationId)}`, {
    method: "DELETE",
  });
  if (!response.ok) await parseResponse(response);
}

export async function retryQuestion(questionId: string): Promise<{
  question: Annotation;
  task_id: string;
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/annotations/${encodeURIComponent(questionId)}/answer`, {
      method: "POST",
    }),
  );
}

export async function getPaperOutline(paperId: string): Promise<PaperOutlineItem[]> {
  return parseResponse(
    await apiFetch(`${API_BASE}/papers/${paperId}/outline`, { cache: "no-store" }),
  );
}

export async function getPaperBlocks(
  paperId: string,
  after: string | null = null,
  limit = 200,
): Promise<ContentBlockPage> {
  const parameters = new URLSearchParams({ limit: String(limit) });
  if (after !== null) parameters.set("after", after);
  return parseResponse(
    await apiFetch(`${API_BASE}/papers/${paperId}/blocks?${parameters}`, { cache: "no-store" }),
  );
}

export async function getPaperTranslations(paperId: string): Promise<BlockTranslation[]> {
  return parseResponse(
    await apiFetch(`${API_BASE}/papers/${encodeURIComponent(paperId)}/translations`, {
      cache: "no-store",
    }),
  );
}

export async function getPaperTranslationProgress(
  paperId: string,
): Promise<TranslationProgress> {
  return parseResponse(
    await apiFetch(`${API_BASE}/papers/${encodeURIComponent(paperId)}/progress`, {
      cache: "no-store",
    }),
  );
}

export async function retryBlockTranslation(
  paperId: string,
  blockId: string,
): Promise<{ task_id: string; status: string }> {
  return parseResponse(
    await apiFetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/blocks/${encodeURIComponent(blockId)}/translation/retry`,
      { method: "POST" },
    ),
  );
}

export async function retranslateBlock(
  paperId: string,
  blockId: string,
  systemPrompt: string,
): Promise<{ task_id: string; status: string }> {
  return parseResponse(
    await apiFetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/blocks/${encodeURIComponent(blockId)}/translation/retranslate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ system_prompt: systemPrompt }),
      },
    ),
  );
}

export async function retranslatePaper(
  paperId: string,
  systemPrompt: string,
): Promise<{
  batch_id: string;
  eligible: number;
  queued: number;
  superseded_tasks: number;
}> {
  return parseResponse(
    await apiFetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/translations/retranslate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ system_prompt: systemPrompt }),
      },
    ),
  );
}

export async function translateRemainingPaper(paperId: string): Promise<{
  eligible: number;
  queued: number;
  created: number;
  retried: number;
  cached: number;
  preserved: number;
  in_flight: number;
}> {
  return parseResponse(
    await apiFetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/translations/remaining`,
      { method: "POST" },
    ),
  );
}

export function paperAssetUrl(paperId: string, assetPath: string): string {
  const encodedPath = assetPath.split("/").map(encodeURIComponent).join("/");
  return `${API_BASE}/papers/${encodeURIComponent(paperId)}/assets/${encodedPath}`;
}

export function paperBlockRenderAssetUrl(
  paperId: string,
  blockId: string,
  assetName: string,
): string {
  return `${API_BASE}/papers/${encodeURIComponent(paperId)}/blocks/${encodeURIComponent(blockId)}/render-assets/${encodeURIComponent(assetName)}`;
}

export const paperTableRenderAssetUrl = paperBlockRenderAssetUrl;

export async function reportRenderFailure(
  paperId: string,
  blockId: string,
  renderer: "katex" | "figure" | "table" | "latex",
  errorMessage: string,
  formulaSelector?: {
    target: "BLOCK" | "TRANSLATION";
    path: Array<string | number>;
    exactLatex: string;
    translationId?: string;
  },
  automatic = false,
): Promise<{ queued: boolean; applied: boolean; task_id: string; status: string }> {
  let textHash: string | undefined;
  if (formulaSelector && globalThis.crypto?.subtle) {
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(formulaSelector.exactLatex),
    );
    textHash = Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }
  const response = await apiFetch(
    `${API_BASE}/papers/${encodeURIComponent(paperId)}/blocks/${encodeURIComponent(blockId)}/render-failure`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        renderer,
        error_message: errorMessage.slice(0, 2000),
        automatic,
        formula_selector: formulaSelector ? {
          target: formulaSelector.target,
          path: formulaSelector.path,
          exact_latex: formulaSelector.exactLatex,
          text_hash: textHash,
          translation_id: formulaSelector.translationId,
        } : undefined,
      }),
    },
  );
  return parseResponse(response);
}

export async function requestLatexFormulaRender(
  paperId: string,
  blockId: string,
  errorMessage: string,
  formulaSelector?: {
    target: "BLOCK" | "TRANSLATION";
    path: Array<string | number>;
    exactLatex: string;
    translationId?: string;
  },
): Promise<{ queued: boolean; applied: boolean; task_id: string; status: string }> {
  let textHash: string | undefined;
  if (formulaSelector && globalThis.crypto?.subtle) {
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(formulaSelector.exactLatex),
    );
    textHash = Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }
  return parseResponse(
    await apiFetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/blocks/${encodeURIComponent(blockId)}/latex-render`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          error_message: errorMessage.slice(0, 2000),
          formula_selector: formulaSelector ? {
            target: formulaSelector.target,
            path: formulaSelector.path,
            exact_latex: formulaSelector.exactLatex,
            text_hash: textHash,
            translation_id: formulaSelector.translationId,
          } : undefined,
        }),
      },
    ),
  );
}

export async function getPaperTaskDetail(
  paperId: string,
  taskId: string,
): Promise<PaperTaskDetail> {
  return parseResponse(
    await apiFetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/tasks/${encodeURIComponent(taskId)}`,
      { cache: "no-store" },
    ),
  );
}

export type SettingsStatus = {
  environment: string;
  storage_backend: string;
  llm_configured: boolean;
  llm_provider: string | null;
  llm_model: string | null;
  repair_render_reasoning_effort: "off" | "low" | "medium" | "high";
  translation_configured: boolean;
  qa_configured: boolean;
  qa_max_attempts: number;
  translation_target_language: string;
  translation_max_attempts: number;
  translation_system_prompt: string;
  automatic_ai_repair_enabled: boolean;
  automatic_translation_enabled: boolean;
};

export async function getSettingsStatus(): Promise<SettingsStatus> {
  return parseResponse(await apiFetch(`${API_BASE}/settings/status`, { cache: "no-store" }));
}

export async function updateAutomaticAiRepair(
  enabled: boolean,
): Promise<{ automatic_ai_repair_enabled: boolean }> {
  return parseResponse(
    await apiFetch(`${API_BASE}/settings/automatic-ai-repair`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    }),
  );
}

export async function updateAutomaticTranslation(
  enabled: boolean,
): Promise<{ automatic_translation_enabled: boolean }> {
  return parseResponse(
    await apiFetch(`${API_BASE}/settings/automatic-translation`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    }),
  );
}

export async function updateTranslationSystemPrompt(
  systemPrompt: string,
): Promise<{ system_prompt: string }> {
  return parseResponse(
    await apiFetch(`${API_BASE}/settings/translation-prompt`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ system_prompt: systemPrompt }),
    }),
  );
}

export async function resetTranslationSystemPrompt(): Promise<{ system_prompt: string }> {
  return parseResponse(
    await apiFetch(`${API_BASE}/settings/translation-prompt/reset`, {
      method: "POST",
    }),
  );
}

export type PaperMapQuestionLibraryItem = {
  key: string;
  text: string;
  dimension: string;
  paper_types: string[];
  user_defined: boolean;
};

export async function getPaperMapQuestionLibrary(): Promise<{
  questions: PaperMapQuestionLibraryItem[];
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/settings/paper-map-questions`, { cache: "no-store" }),
  );
}

export async function addPaperMapQuestion(text: string): Promise<{
  questions: PaperMapQuestionLibraryItem[];
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/settings/paper-map-questions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  );
}

export async function deletePaperMapQuestion(key: string): Promise<{
  questions: PaperMapQuestionLibraryItem[];
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/settings/paper-map-questions/${encodeURIComponent(key)}`, {
      method: "DELETE",
    }),
  );
}

export async function updatePaperMapQuestion(key: string, text: string): Promise<{
  questions: PaperMapQuestionLibraryItem[];
}> {
  return parseResponse(
    await apiFetch(`${API_BASE}/settings/paper-map-questions/${encodeURIComponent(key)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }),
  );
}
