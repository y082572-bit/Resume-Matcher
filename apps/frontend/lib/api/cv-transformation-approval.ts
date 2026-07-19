import { apiFetch } from './client';
import type {
  CVTransformationPlanApproval,
  CVTransformationPlanApprovalRequest,
  TransformationPlanApprovalStatus,
  TransformationPlanDecision,
  TransformationPlanDecisionValue,
} from '../types/cv-transformation-approval';

const APPROVAL_KEYS = new Set([
  'approval_id',
  'plan_version',
  'plan_fingerprint',
  'resume_id',
  'job_id',
  'status',
  'decisions',
  'guardrails_acknowledged',
  'created_at',
  'updated_at',
]);
const DECISION_KEYS = new Set(['source_reference', 'decision']);
const STATUSES = new Set<TransformationPlanApprovalStatus>([
  'DRAFT',
  'APPROVED',
  'REQUIRES_REVIEW',
  'SUPERSEDED',
]);
const DECISIONS = new Set<TransformationPlanDecisionValue>(['ACCEPT', 'REJECT', 'REQUEST_REVIEW']);
const REFERENCE_PATTERN =
  /^(resume|positioning):(summary|experience|competencies|achievements|tools):\d{4}$/;

export class CVTransformationPlanApprovalApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
    message: string
  ) {
    super(message);
    this.name = 'CVTransformationPlanApprovalApiError';
  }
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(value: Record<string, unknown>, allowed: Set<string>, name: string): void {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new Error(`Unexpected field ${name}.${key}.`);
  }
}

function requiredString(value: unknown, name: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`${name} must be a non-empty string.`);
  }
  return value;
}

function parseDecision(value: unknown): TransformationPlanDecision {
  const decision = object(value, 'decision');
  exactKeys(decision, DECISION_KEYS, 'decision');
  const reference = requiredString(decision.source_reference, 'source_reference');
  const selected = requiredString(decision.decision, 'decision') as TransformationPlanDecisionValue;
  if (!REFERENCE_PATTERN.test(reference) || !DECISIONS.has(selected)) {
    throw new Error('Invalid transformation-plan decision.');
  }
  return { source_reference: reference, decision: selected };
}

export function parseCVTransformationPlanApproval(value: unknown): CVTransformationPlanApproval {
  const approval = object(value, 'approval');
  exactKeys(approval, APPROVAL_KEYS, 'approval');
  if (approval.plan_version !== '1.1') throw new Error('Unsupported approval plan_version.');
  const fingerprint = requiredString(approval.plan_fingerprint, 'plan_fingerprint');
  if (!/^[0-9a-f]{64}$/.test(fingerprint)) throw new Error('Invalid plan_fingerprint.');
  const status = requiredString(approval.status, 'status') as TransformationPlanApprovalStatus;
  if (!STATUSES.has(status)) throw new Error('Invalid approval status.');
  if (!Array.isArray(approval.decisions)) throw new Error('decisions must be an array.');
  const decisions = approval.decisions.map(parseDecision);
  const references = decisions.map((item) => item.source_reference);
  if (
    references.length !== new Set(references).size ||
    references.some((item, index) => index > 0 && references[index - 1] > item)
  ) {
    throw new Error('Approval decisions must be unique and sorted.');
  }
  if (typeof approval.guardrails_acknowledged !== 'boolean') {
    throw new Error('guardrails_acknowledged must be a boolean.');
  }
  const acknowledged = approval.guardrails_acknowledged;
  const hasReviewRequest = decisions.some((item) => item.decision === 'REQUEST_REVIEW');
  if (status === 'APPROVED' && !acknowledged) {
    throw new Error('APPROVED requires guardrails acknowledgment.');
  }
  if (status === 'APPROVED' && hasReviewRequest) {
    throw new Error('APPROVED cannot contain REQUEST_REVIEW.');
  }
  if (status === 'REQUIRES_REVIEW' && !acknowledged) {
    throw new Error('REQUIRES_REVIEW requires guardrails acknowledgment.');
  }
  if (status === 'REQUIRES_REVIEW' && !hasReviewRequest) {
    throw new Error('REQUIRES_REVIEW requires REQUEST_REVIEW.');
  }
  const createdAt = requiredString(approval.created_at, 'created_at');
  const updatedAt = requiredString(approval.updated_at, 'updated_at');
  if (!Number.isFinite(Date.parse(createdAt)) || !Number.isFinite(Date.parse(updatedAt))) {
    throw new Error('Approval timestamps must be valid.');
  }
  return {
    approval_id: requiredString(approval.approval_id, 'approval_id'),
    plan_version: '1.1',
    plan_fingerprint: fingerprint,
    resume_id: requiredString(approval.resume_id, 'resume_id'),
    job_id: requiredString(approval.job_id, 'job_id'),
    status,
    decisions,
    guardrails_acknowledged: acknowledged,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

async function approvalResponse(response: Response): Promise<CVTransformationPlanApproval> {
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as {
      detail?: { code?: string; message?: string };
    };
    throw new CVTransformationPlanApprovalApiError(
      payload.detail?.code ?? 'TRANSFORMATION_PLAN_APPROVAL_ERROR',
      response.status,
      payload.detail?.message ?? `Transformation plan approval failed (${response.status}).`
    );
  }
  return parseCVTransformationPlanApproval(await response.json());
}

function approvalPath(jobId: string, resumeId: string): string {
  return `/jobs/${encodeURIComponent(jobId)}/resumes/${encodeURIComponent(resumeId)}/transformation-plan/approval`;
}

export async function fetchCVTransformationPlanApproval(
  jobId: string,
  resumeId: string,
  signal?: AbortSignal
): Promise<CVTransformationPlanApproval> {
  try {
    return await approvalResponse(
      await apiFetch(approvalPath(jobId, resumeId), { method: 'GET', signal })
    );
  } catch (error) {
    if (error instanceof CVTransformationPlanApprovalApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') throw error;
    throw new CVTransformationPlanApprovalApiError(
      'NETWORK_OR_CONTRACT_ERROR',
      0,
      error instanceof Error ? error.message : 'Approval request failed.'
    );
  }
}

export async function saveCVTransformationPlanApproval(
  jobId: string,
  resumeId: string,
  request: CVTransformationPlanApprovalRequest,
  signal?: AbortSignal
): Promise<CVTransformationPlanApproval> {
  try {
    return await approvalResponse(
      await apiFetch(approvalPath(jobId, resumeId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
        signal,
      })
    );
  } catch (error) {
    if (error instanceof CVTransformationPlanApprovalApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') throw error;
    throw new CVTransformationPlanApprovalApiError(
      'NETWORK_OR_CONTRACT_ERROR',
      0,
      error instanceof Error ? error.message : 'Approval request failed.'
    );
  }
}
