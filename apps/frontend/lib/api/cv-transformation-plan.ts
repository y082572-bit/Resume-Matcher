import { apiFetch } from './client';
import type {
  CVTransformationAction,
  CVTransformationPlan,
  CVTransformationPlanItem,
  CVTransformationSection,
  EvidenceAllowedOperation,
  EvidenceClaimCode,
  EvidencePermission,
  EvidenceStrength,
  ProhibitedClaim,
  ProhibitedClaimCode,
} from '../types/cv-transformation-plan';
import type { PositioningStrategy, RoleLevel } from '../types/career-positioning';

export class CVTransformationPlanApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
    message: string
  ) {
    super(message);
    this.name = 'CVTransformationPlanApiError';
  }
}

const ROOT_KEYS = new Set([
  'plan_version',
  'plan_fingerprint',
  'resume_id',
  'job_id',
  'generated_at',
  'detected_job_level',
  'candidate_profile_level',
  'positioning_strategy',
  'requires_human_review',
  'summary_plan',
  'experience_plan',
  'competencies_plan',
  'achievements_plan',
  'tools_plan',
  'evidence_permissions',
  'prohibited_claims',
  'limitations',
  'source_summary',
]);
const ITEM_KEYS = new Set([
  'action',
  'section',
  'source_reference',
  'display_label',
  'reason_code',
  'reason',
  'evidence_strength',
  'human_review_required',
]);
const CLAIM_KEYS = new Set(['code', 'reason_code', 'reason', 'human_review_required']);
const PERMISSION_KEYS = new Set([
  'claim_code',
  'source_reference',
  'evidence_strength',
  'allowed_operation',
  'human_review_required',
]);
const SOURCE_KEYS = new Set([
  'approved_truth_items_count',
  'resume_items_count',
  'job_requirements_count',
  'career_positioning_schema_version',
]);
const ACTIONS = new Set<CVTransformationAction>([
  'KEEP',
  'EMPHASIZE',
  'DEEMPHASIZE',
  'REPHRASE',
  'OMIT',
  'HUMAN_REVIEW',
]);
const SECTIONS = new Set<CVTransformationSection>([
  'SUMMARY',
  'EXPERIENCE',
  'COMPETENCIES',
  'ACHIEVEMENTS',
  'TOOLS',
]);
const STRENGTHS = new Set<EvidenceStrength>(['NONE', 'WEAK', 'MODERATE', 'STRONG']);
const LEVELS = new Set<RoleLevel>([
  'UNKNOWN',
  'JUNIOR',
  'SPECIALIST',
  'EXPERT',
  'MANAGER',
  'DIRECTOR',
  'EXECUTIVE',
]);
const STRATEGIES = new Set<PositioningStrategy>([
  'REVIEW_REQUIRED',
  'JUNIOR_ENTRY',
  'SPECIALIST_DELIVERY',
  'EXPERT_AUTHORITY',
  'MANAGER_LEADERSHIP',
  'DIRECTOR_SCALE',
  'EXECUTIVE_ENTERPRISE',
  'CONTROLLED_FLATTENING',
  'CONTROLLED_ELEVATION',
  'BALANCED',
]);
const CLAIM_CODE_VALUES: ProhibitedClaimCode[] = [
  'BOARD_WITHOUT_EVIDENCE',
  'BUDGET_WITHOUT_EVIDENCE',
  'CERTIFICATION_WITHOUT_EVIDENCE',
  'LANGUAGE_LEVEL_WITHOUT_EVIDENCE',
  'MANAGING_MANAGERS_WITHOUT_EVIDENCE',
  'PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE',
  'PNL_WITHOUT_EVIDENCE',
  'QUANTIFIED_RESULT_WITHOUT_EVIDENCE',
  'TEAM_SIZE_WITHOUT_EVIDENCE',
  'TECHNICAL_SKILL_WITHOUT_EVIDENCE',
];
const CLAIM_CODES = new Set<ProhibitedClaimCode>(CLAIM_CODE_VALUES);
const EVIDENCE_CLAIM_CODES = new Set<EvidenceClaimCode>([
  ...CLAIM_CODE_VALUES,
  'STRATEGY_RESPONSIBILITY_EVIDENCE',
  'ORGANIZATIONAL_SCALE_EVIDENCE',
  'COMPETENCY_EVIDENCE',
]);
const ALLOWED_OPERATIONS = new Set<EvidenceAllowedOperation>([
  'KEEP_EXISTING',
  'EMPHASIZE_EXISTING',
  'REPHRASE_EXISTING',
]);
const SOURCE_REFERENCE_PATTERN =
  /^(resume|positioning):(summary|experience|competencies|achievements|tools):\d{4}$/;

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

function string(value: unknown, name: string): string {
  if (typeof value !== 'string' || !value.trim())
    throw new Error(`${name} must be a non-empty string.`);
  return value;
}

function boolean(value: unknown, name: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`${name} must be a boolean.`);
  return value;
}

function count(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0)
    throw new Error(`${name} must be a non-negative integer.`);
  return value as number;
}

function parseItem(
  value: unknown,
  expectedSection: CVTransformationSection
): CVTransformationPlanItem {
  const item = object(value, 'plan item');
  exactKeys(item, ITEM_KEYS, 'plan item');
  const action = string(item.action, 'action') as CVTransformationAction;
  const section = string(item.section, 'section') as CVTransformationSection;
  const strength = string(item.evidence_strength, 'evidence_strength') as EvidenceStrength;
  if (
    !ACTIONS.has(action) ||
    !SECTIONS.has(section) ||
    section !== expectedSection ||
    !STRENGTHS.has(strength)
  ) {
    throw new Error('Invalid plan item enum value.');
  }
  const reference = string(item.source_reference, 'source_reference');
  if (!SOURCE_REFERENCE_PATTERN.test(reference)) {
    throw new Error('Invalid safe source_reference.');
  }
  if (reference.split(':')[1] !== section.toLowerCase()) {
    throw new Error('source_reference section does not match item section.');
  }
  const humanReviewRequired = boolean(item.human_review_required, 'human_review_required');
  if (action === 'HUMAN_REVIEW' && !humanReviewRequired) {
    throw new Error('HUMAN_REVIEW action requires human_review_required=true.');
  }
  return {
    action,
    section,
    source_reference: reference,
    display_label: (() => {
      const label = string(item.display_label, 'display_label');
      if (label.length > 120) throw new Error('display_label exceeds 120 characters.');
      return label;
    })(),
    reason_code: string(item.reason_code, 'reason_code'),
    reason: string(item.reason, 'reason'),
    evidence_strength: strength,
    human_review_required: humanReviewRequired,
  };
}

function parseItems(value: unknown, section: CVTransformationSection): CVTransformationPlanItem[] {
  if (!Array.isArray(value)) throw new Error(`${section} plan must be an array.`);
  return value.map((item) => parseItem(item, section));
}

function parseClaim(value: unknown): ProhibitedClaim {
  const claim = object(value, 'prohibited claim');
  exactKeys(claim, CLAIM_KEYS, 'prohibited claim');
  const code = string(claim.code, 'claim code') as ProhibitedClaimCode;
  if (
    !CLAIM_CODES.has(code) ||
    claim.reason_code !== 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM' ||
    claim.human_review_required !== true
  ) {
    throw new Error('Invalid prohibited claim.');
  }
  return {
    code,
    reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
    reason: string(claim.reason, 'claim reason'),
    human_review_required: true,
  };
}

function parsePermission(value: unknown): EvidencePermission {
  const permission = object(value, 'evidence permission');
  exactKeys(permission, PERMISSION_KEYS, 'evidence permission');
  const claimCode = string(permission.claim_code, 'permission claim_code') as EvidenceClaimCode;
  const sourceReference = string(permission.source_reference, 'permission source_reference');
  const evidenceStrength = string(permission.evidence_strength, 'permission evidence_strength') as
    | 'MODERATE'
    | 'STRONG';
  const allowedOperation = string(
    permission.allowed_operation,
    'permission allowed_operation'
  ) as EvidenceAllowedOperation;
  if (
    !EVIDENCE_CLAIM_CODES.has(claimCode) ||
    !SOURCE_REFERENCE_PATTERN.test(sourceReference) ||
    !new Set(['MODERATE', 'STRONG']).has(evidenceStrength) ||
    !ALLOWED_OPERATIONS.has(allowedOperation)
  ) {
    throw new Error('Invalid evidence permission.');
  }
  return {
    claim_code: claimCode,
    source_reference: sourceReference,
    evidence_strength: evidenceStrength,
    allowed_operation: allowedOperation,
    human_review_required: boolean(
      permission.human_review_required,
      'permission human_review_required'
    ),
  };
}

export function parseCVTransformationPlan(value: unknown): CVTransformationPlan {
  const root = object(value, 'transformation plan');
  exactKeys(root, ROOT_KEYS, 'transformation plan');
  if (root.plan_version !== '1.1') throw new Error('Unsupported plan_version.');
  const generatedAt = string(root.generated_at, 'generated_at');
  if (!Number.isFinite(Date.parse(generatedAt)) || !/(?:Z|[+-]\d{2}:\d{2})$/.test(generatedAt)) {
    throw new Error('generated_at must be a valid timezone-aware timestamp.');
  }
  const detected = string(root.detected_job_level, 'detected_job_level') as RoleLevel;
  const candidate = string(root.candidate_profile_level, 'candidate_profile_level') as RoleLevel;
  const strategy = string(root.positioning_strategy, 'positioning_strategy') as PositioningStrategy;
  if (!LEVELS.has(detected) || !LEVELS.has(candidate) || !STRATEGIES.has(strategy)) {
    throw new Error('Invalid positioning enum value.');
  }
  const claims = root.prohibited_claims;
  if (!Array.isArray(claims)) throw new Error('prohibited_claims must be an array.');
  const parsedClaims = claims.map(parseClaim);
  const claimCodes = parsedClaims.map((claim) => claim.code);
  if (JSON.stringify(claimCodes) !== JSON.stringify(CLAIM_CODE_VALUES)) {
    throw new Error('prohibited_claims must contain all unique sorted guardrails.');
  }
  const rawPermissions = root.evidence_permissions;
  if (!Array.isArray(rawPermissions)) {
    throw new Error('evidence_permissions must be an array.');
  }
  const permissions = rawPermissions.map(parsePermission);
  const limitations = root.limitations;
  if (
    !Array.isArray(limitations) ||
    limitations.some((item) => typeof item !== 'string' || !item.trim())
  ) {
    throw new Error('limitations must contain non-empty strings.');
  }
  const source = object(root.source_summary, 'source_summary');
  exactKeys(source, SOURCE_KEYS, 'source_summary');
  if (source.career_positioning_schema_version !== '1.0')
    throw new Error('Invalid career positioning schema version.');
  const requiresHumanReview = boolean(root.requires_human_review, 'requires_human_review');
  const summaryPlan = parseItems(root.summary_plan, 'SUMMARY');
  const experiencePlan = parseItems(root.experience_plan, 'EXPERIENCE');
  const competenciesPlan = parseItems(root.competencies_plan, 'COMPETENCIES');
  const achievementsPlan = parseItems(root.achievements_plan, 'ACHIEVEMENTS');
  const toolsPlan = parseItems(root.tools_plan, 'TOOLS');
  const planItems = [
    ...summaryPlan,
    ...experiencePlan,
    ...competenciesPlan,
    ...achievementsPlan,
    ...toolsPlan,
  ];
  const references = planItems.map((item) => item.source_reference);
  if (references.length !== new Set(references).size) {
    throw new Error('source_reference values must be unique.');
  }
  if (
    planItems.some((item) => item.action === 'HUMAN_REVIEW' || item.human_review_required) &&
    !requiresHumanReview
  ) {
    throw new Error('HUMAN_REVIEW must propagate to requires_human_review.');
  }
  const referenceSet = new Set(references);
  if (permissions.some((permission) => !referenceSet.has(permission.source_reference))) {
    throw new Error('Evidence permission references an unknown plan item.');
  }
  const permissionKeys = permissions.map(
    (permission) =>
      `${permission.claim_code}\u0000${permission.source_reference}\u0000${permission.allowed_operation}`
  );
  if (
    permissionKeys.length !== new Set(permissionKeys).size ||
    permissionKeys.some((key, index) => index > 0 && permissionKeys[index - 1] > key)
  ) {
    throw new Error('evidence_permissions must be unique and sorted.');
  }
  if (permissions.some((permission) => permission.human_review_required) && !requiresHumanReview) {
    throw new Error('Permission review must propagate to requires_human_review.');
  }
  const parsedLimitations = limitations as string[];
  if (
    parsedLimitations.length !== new Set(parsedLimitations).size ||
    parsedLimitations.some((item, index) => index > 0 && parsedLimitations[index - 1] > item)
  ) {
    throw new Error('limitations must be unique and sorted.');
  }
  return {
    plan_version: '1.1',
    plan_fingerprint: (() => {
      const fingerprint = string(root.plan_fingerprint, 'plan_fingerprint');
      if (!/^[0-9a-f]{64}$/.test(fingerprint)) {
        throw new Error('plan_fingerprint must be a backend SHA-256 value.');
      }
      return fingerprint;
    })(),
    resume_id: string(root.resume_id, 'resume_id'),
    job_id: string(root.job_id, 'job_id'),
    generated_at: generatedAt,
    detected_job_level: detected,
    candidate_profile_level: candidate,
    positioning_strategy: strategy,
    requires_human_review: requiresHumanReview,
    summary_plan: summaryPlan,
    experience_plan: experiencePlan,
    competencies_plan: competenciesPlan,
    achievements_plan: achievementsPlan,
    tools_plan: toolsPlan,
    evidence_permissions: permissions,
    prohibited_claims: parsedClaims,
    limitations: parsedLimitations,
    source_summary: {
      approved_truth_items_count: count(
        source.approved_truth_items_count,
        'approved_truth_items_count'
      ),
      resume_items_count: count(source.resume_items_count, 'resume_items_count'),
      job_requirements_count: count(source.job_requirements_count, 'job_requirements_count'),
      career_positioning_schema_version: '1.0',
    },
  };
}

export async function fetchCVTransformationPlan(
  jobId: string,
  resumeId: string,
  signal?: AbortSignal
): Promise<CVTransformationPlan> {
  try {
    const response = await apiFetch(
      `/jobs/${encodeURIComponent(jobId)}/resumes/${encodeURIComponent(resumeId)}/transformation-plan`,
      { method: 'GET', signal }
    );
    if (!response.ok) {
      const payload = (await response.json().catch(() => ({}))) as {
        detail?: { code?: string; message?: string };
      };
      throw new CVTransformationPlanApiError(
        payload.detail?.code ?? 'TRANSFORMATION_PLAN_ERROR',
        response.status,
        payload.detail?.message ?? `Transformation plan request failed (${response.status}).`
      );
    }
    return parseCVTransformationPlan(await response.json());
  } catch (error) {
    if (error instanceof CVTransformationPlanApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') throw error;
    throw new CVTransformationPlanApiError(
      'NETWORK_OR_CONTRACT_ERROR',
      0,
      error instanceof Error ? error.message : 'Transformation plan request failed.'
    );
  }
}
