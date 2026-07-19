import type { ResumeData } from '@/components/dashboard/resume-component';
import { apiFetch } from './client';
import type {
  CVTransformationGeneration,
  CVTransformationGenerationRequest,
  CVTransformationGenerationStatus,
  CVTransformationProvenance,
} from '../types/cv-transformation-generation';

const KEYS = new Set([
  'generation_id',
  'approval_id',
  'resume_id',
  'job_id',
  'plan_version',
  'plan_fingerprint',
  'generation_version',
  'prompt_version',
  'generation_input_fingerprint',
  'status',
  'provider',
  'model',
  'draft_resume',
  'provenance',
  'failure_code',
  'attempt_count',
  'created_at',
  'updated_at',
  'completed_at',
  'requires_truth_validation',
  'truth_validation_status',
  'applied_to_resume',
]);
const PROVENANCE_KEYS = new Set([
  'source_reference',
  'section',
  'action',
  'decision',
  'mode',
  'priority',
  'source_fingerprint',
  'output_fingerprint',
  'llm_used',
  'prompt_version',
  'validation_status',
  'boundary_validation_status',
]);
const STATUSES = new Set<CVTransformationGenerationStatus>([
  'GENERATING',
  'GENERATED',
  'FAILED',
  'SUPERSEDED',
]);
const SECTIONS = new Set(['SUMMARY', 'EXPERIENCE', 'COMPETENCIES', 'ACHIEVEMENTS', 'TOOLS']);
const ACTIONS = new Set(['KEEP', 'EMPHASIZE', 'DEEMPHASIZE', 'REPHRASE', 'OMIT', 'HUMAN_REVIEW']);
const MODES = new Set([
  'COPIED',
  'COPIED_LOW_PRIORITY',
  'GENERATED',
  'EXACT_INSERT',
  'OMITTED',
  'EXCLUDED',
  'PRESERVED_AFTER_REVIEW',
  'EXCLUDED_UNRESOLVED',
  'COPIED_NO_MUTABLE_CONTENT',
  'COPIED_PROTECTED_CONTENT',
  'EXCLUDED_BY_PARENT',
]);
const FAILURE_CODES = new Set([
  'INVALID_LLM_RESPONSE',
  'SOURCE_REFERENCE_MISMATCH',
  'NUMERIC_BOUNDARY_VIOLATION',
  'PROHIBITED_CLAIM_BOUNDARY_VIOLATION',
  'LLM_PROVIDER_ERROR',
  'DRAFT_ASSEMBLY_FAILED',
  'SOURCE_CHANGED_DURING_GENERATION',
]);
const SOURCE_REFERENCE =
  /^(?:resume|positioning):(?:summary|experience|competencies|achievements|tools):\d{4}$/;

export class CVTransformationGenerationApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status: number,
    message: string
  ) {
    super(message);
    this.name = 'CVTransformationGenerationApiError';
  }
}

function record(value: unknown, name: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error(`${name} must be an object.`);
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, allowed: Set<string>, name: string): void {
  for (const key of Object.keys(value))
    if (!allowed.has(key)) throw new Error(`Unexpected field ${name}.${key}.`);
  for (const key of allowed) if (!(key in value)) throw new Error(`Missing field ${name}.${key}.`);
}

function string(value: unknown, name: string): string {
  if (typeof value !== 'string' || !value.trim())
    throw new Error(`${name} must be a non-empty string.`);
  return value;
}

function nullableString(value: unknown, name: string): string | null {
  if (value === null) return null;
  return string(value, name);
}

function plainString(value: unknown, name: string): string {
  if (typeof value !== 'string') throw new Error(`${name} must be a string.`);
  return value;
}

function nullablePlainString(value: unknown, name: string): string | null {
  return value === null ? null : plainString(value, name);
}

function integer(value: unknown, name: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value))
    throw new Error(`${name} must be an integer.`);
  return value;
}

function stringArray(value: unknown, name: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string'))
    throw new Error(`${name} must contain only strings.`);
  return value;
}

function isoDate(value: unknown, name: string): string {
  const result = string(value, name);
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(result) ||
    Number.isNaN(Date.parse(result))
  )
    throw new Error(`${name} must be timezone-aware ISO-8601.`);
  return result;
}

function parseProvenance(value: unknown): CVTransformationProvenance {
  const item = record(value, 'provenance');
  exact(item, PROVENANCE_KEYS, 'provenance');
  const section = string(item.section, 'section');
  if (!SECTIONS.has(section)) throw new Error('Invalid provenance section.');
  const priority = string(item.priority, 'priority');
  if (!['HIGH', 'NORMAL', 'LOW'].includes(priority))
    throw new Error('Invalid provenance priority.');
  const decision = string(item.decision, 'decision');
  if (!['ACCEPT', 'REJECT'].includes(decision)) throw new Error('Invalid provenance decision.');
  const validation = string(item.validation_status, 'validation_status');
  if (validation !== 'NOT_RUN') throw new Error('Invalid truth validation status.');
  const boundary = string(item.boundary_validation_status, 'boundary_validation_status');
  if (!['PASSED', 'NOT_APPLICABLE'].includes(boundary))
    throw new Error('Invalid boundary validation status.');
  const action = string(item.action, 'action');
  const mode = string(item.mode, 'mode');
  if (!ACTIONS.has(action)) throw new Error('Invalid provenance action.');
  if (!MODES.has(mode)) throw new Error('Invalid provenance mode.');
  if (typeof item.llm_used !== 'boolean') throw new Error('llm_used must be boolean.');
  const sourceFingerprint = string(item.source_fingerprint, 'source_fingerprint');
  const outputFingerprint = nullableString(item.output_fingerprint, 'output_fingerprint');
  if (
    !/^[0-9a-f]{64}$/.test(sourceFingerprint) ||
    (outputFingerprint && !/^[0-9a-f]{64}$/.test(outputFingerprint))
  ) {
    throw new Error('Invalid provenance fingerprint.');
  }
  const sourceReference = string(item.source_reference, 'source_reference');
  if (!SOURCE_REFERENCE.test(sourceReference)) throw new Error('Invalid source_reference.');
  const promptVersion = nullableString(item.prompt_version, 'prompt_version');
  const excluded = ['OMITTED', 'EXCLUDED', 'EXCLUDED_UNRESOLVED', 'EXCLUDED_BY_PARENT'].includes(
    mode
  );
  const copied = [
    'COPIED',
    'COPIED_LOW_PRIORITY',
    'EXACT_INSERT',
    'PRESERVED_AFTER_REVIEW',
    'COPIED_NO_MUTABLE_CONTENT',
    'COPIED_PROTECTED_CONTENT',
  ].includes(mode);
  if (decision === 'REJECT' && mode !== 'EXCLUDED') throw new Error('REJECT must be EXCLUDED.');
  if (decision === 'ACCEPT') {
    if (mode === 'EXCLUDED_BY_PARENT') {
      if (section !== 'ACHIEVEMENTS') throw new Error('Invalid parent exclusion mode.');
    } else {
      let expected: string[] | undefined = (
        {
          KEEP: ['COPIED'],
          DEEMPHASIZE: ['COPIED_LOW_PRIORITY'],
          OMIT: ['OMITTED'],
          HUMAN_REVIEW: ['PRESERVED_AFTER_REVIEW', 'EXCLUDED_UNRESOLVED'],
        } as Record<string, string[]>
      )[action];
      if (action === 'EMPHASIZE' || action === 'REPHRASE') {
        expected = ['COMPETENCIES', 'TOOLS'].includes(section)
          ? ['EXACT_INSERT']
          : section === 'EXPERIENCE'
            ? ['GENERATED', 'COPIED_NO_MUTABLE_CONTENT', 'COPIED_PROTECTED_CONTENT']
            : ['GENERATED', 'COPIED_PROTECTED_CONTENT'];
      }
      if (!expected?.includes(mode)) throw new Error('Inconsistent action/section/mode matrix.');
    }
  }
  if (
    (mode === 'GENERATED' &&
      (item.llm_used !== true ||
        promptVersion !== '1.3' ||
        !outputFingerprint ||
        boundary !== 'PASSED')) ||
    (copied &&
      (item.llm_used !== false ||
        promptVersion !== null ||
        !outputFingerprint ||
        boundary !== 'NOT_APPLICABLE')) ||
    (excluded &&
      (item.llm_used !== false ||
        promptVersion !== null ||
        outputFingerprint !== null ||
        boundary !== 'NOT_APPLICABLE'))
  )
    throw new Error('Inconsistent provenance mode contract.');
  return {
    source_reference: sourceReference,
    section: section as CVTransformationProvenance['section'],
    action: action as CVTransformationProvenance['action'],
    decision: decision as CVTransformationProvenance['decision'],
    mode: mode as CVTransformationProvenance['mode'],
    priority: priority as CVTransformationProvenance['priority'],
    source_fingerprint: sourceFingerprint,
    output_fingerprint: outputFingerprint,
    llm_used: item.llm_used,
    prompt_version: promptVersion,
    validation_status: 'NOT_RUN',
    boundary_validation_status:
      boundary as CVTransformationProvenance['boundary_validation_status'],
  };
}

function parseDraft(value: unknown): ResumeData | null {
  if (value === null) return null;
  const draft = record(value, 'draft_resume');
  exact(
    draft,
    new Set([
      'personalInfo',
      'summary',
      'workExperience',
      'education',
      'personalProjects',
      'additional',
      'sectionMeta',
      'customSections',
    ]),
    'draft_resume'
  );
  const personalInfo = record(draft.personalInfo, 'draft_resume.personalInfo');
  exact(
    personalInfo,
    new Set(['name', 'title', 'email', 'phone', 'location', 'website', 'linkedin', 'github']),
    'draft_resume.personalInfo'
  );
  for (const key of ['name', 'title', 'email', 'phone', 'location'])
    plainString(personalInfo[key], `draft_resume.personalInfo.${key}`);
  for (const key of ['website', 'linkedin', 'github'])
    nullablePlainString(personalInfo[key], `draft_resume.personalInfo.${key}`);
  plainString(draft.summary, 'draft_resume.summary');
  const additional = record(draft.additional, 'draft_resume.additional');
  exact(
    additional,
    new Set(['technicalSkills', 'languages', 'certificationsTraining', 'awards']),
    'draft_resume.additional'
  );
  for (const key of ['technicalSkills', 'languages', 'certificationsTraining', 'awards'])
    stringArray(additional[key], `draft_resume.additional.${key}`);
  const nested: Array<[string, Set<string>]> = [
    ['workExperience', new Set(['id', 'title', 'company', 'location', 'years', 'description'])],
    ['education', new Set(['id', 'institution', 'degree', 'years', 'description'])],
    [
      'personalProjects',
      new Set(['id', 'name', 'role', 'years', 'github', 'website', 'description']),
    ],
    [
      'sectionMeta',
      new Set(['id', 'key', 'displayName', 'sectionType', 'isDefault', 'isVisible', 'order']),
    ],
  ];
  for (const [arrayKey, keys] of nested) {
    if (!Array.isArray(draft[arrayKey]))
      throw new Error(`draft_resume.${arrayKey} must be an array.`);
    (draft[arrayKey] as unknown[]).forEach((entry, index) => {
      const path = `draft_resume.${arrayKey}[${index}]`;
      const item = record(entry, path);
      exact(item, keys, path);
      if (arrayKey === 'workExperience') {
        integer(item.id, `${path}.id`);
        for (const key of ['title', 'company', 'years']) plainString(item[key], `${path}.${key}`);
        nullablePlainString(item.location, `${path}.location`);
        stringArray(item.description, `${path}.description`);
      } else if (arrayKey === 'education') {
        integer(item.id, `${path}.id`);
        for (const key of ['institution', 'degree', 'years'])
          plainString(item[key], `${path}.${key}`);
        nullablePlainString(item.description, `${path}.description`);
      } else if (arrayKey === 'personalProjects') {
        integer(item.id, `${path}.id`);
        for (const key of ['name', 'role', 'years']) plainString(item[key], `${path}.${key}`);
        nullablePlainString(item.github, `${path}.github`);
        nullablePlainString(item.website, `${path}.website`);
        stringArray(item.description, `${path}.description`);
      } else {
        for (const key of ['id', 'key', 'displayName']) plainString(item[key], `${path}.${key}`);
        if (!['personalInfo', 'text', 'itemList', 'stringList'].includes(String(item.sectionType)))
          throw new Error(`${path}.sectionType is invalid.`);
        if (typeof item.isDefault !== 'boolean' || typeof item.isVisible !== 'boolean')
          throw new Error(`${path} visibility flags must be booleans.`);
        integer(item.order, `${path}.order`);
      }
    });
  }
  const customSections = record(draft.customSections, 'draft_resume.customSections');
  for (const [key, rawSection] of Object.entries(customSections)) {
    const section = record(rawSection, `draft_resume.customSections.${key}`);
    exact(
      section,
      new Set(['sectionType', 'items', 'strings', 'text']),
      `draft_resume.customSections.${key}`
    );
    const sectionType = plainString(section.sectionType, `custom section ${key}.sectionType`);
    if (!['text', 'itemList', 'stringList'].includes(sectionType))
      throw new Error('Invalid custom section type.');
    if (sectionType === 'itemList') {
      if (!Array.isArray(section.items) || section.strings !== null || section.text !== null)
        throw new Error('Invalid itemList custom section shape.');
      section.items.forEach((entry, index) => {
        const path = `draft_resume.customSections.${key}.items[${index}]`;
        const item = record(entry, path);
        exact(item, new Set(['id', 'title', 'subtitle', 'location', 'years', 'description']), path);
        integer(item.id, `${path}.id`);
        plainString(item.title, `${path}.title`);
        nullablePlainString(item.subtitle, `${path}.subtitle`);
        nullablePlainString(item.location, `${path}.location`);
        plainString(item.years, `${path}.years`);
        stringArray(item.description, `${path}.description`);
      });
    } else if (sectionType === 'stringList') {
      if (section.items !== null || section.text !== null)
        throw new Error('Invalid stringList custom section shape.');
      stringArray(section.strings, `draft_resume.customSections.${key}.strings`);
    } else if (
      section.items !== null ||
      section.strings !== null ||
      typeof section.text !== 'string'
    ) {
      throw new Error('Invalid text custom section shape.');
    }
  }
  return draft as ResumeData;
}

export function parseCVTransformationGeneration(value: unknown): CVTransformationGeneration {
  const generation = record(value, 'generation');
  exact(generation, KEYS, 'generation');
  const status = string(generation.status, 'status') as CVTransformationGenerationStatus;
  if (!STATUSES.has(status)) throw new Error('Invalid generation status.');
  const planFingerprint = string(generation.plan_fingerprint, 'plan_fingerprint');
  const inputFingerprint = string(
    generation.generation_input_fingerprint,
    'generation_input_fingerprint'
  );
  if (!/^[0-9a-f]{64}$/.test(planFingerprint) || !/^[0-9a-f]{64}$/.test(inputFingerprint))
    throw new Error('Invalid generation fingerprint.');
  if (!Array.isArray(generation.provenance)) throw new Error('provenance must be an array.');
  const provenance = generation.provenance.map(parseProvenance);
  const references = provenance.map((item) => item.source_reference);
  if (
    new Set(references).size !== references.length ||
    references.some((reference, index) => index > 0 && reference < references[index - 1])
  )
    throw new Error('Provenance must be unique and sorted.');
  const draft = parseDraft(generation.draft_resume);
  const failure = nullableString(generation.failure_code, 'failure_code');
  if (failure !== null && !FAILURE_CODES.has(failure)) throw new Error('Invalid failure_code.');
  const completedRaw = nullableString(generation.completed_at, 'completed_at');
  const completed = completedRaw === null ? null : isoDate(completedRaw, 'completed_at');
  if (
    typeof generation.attempt_count !== 'number' ||
    !Number.isInteger(generation.attempt_count) ||
    generation.attempt_count < 1
  )
    throw new Error('Invalid attempt_count.');
  if (
    generation.requires_truth_validation !== true ||
    generation.truth_validation_status !== 'NOT_RUN' ||
    generation.applied_to_resume !== false
  )
    throw new Error('Unsafe generation flags.');
  if (status === 'GENERATING' && (draft || provenance.length || completed || failure))
    throw new Error('Inconsistent GENERATING payload.');
  if (status === 'GENERATED' && (!draft || !provenance.length || !completed || failure))
    throw new Error('Inconsistent GENERATED payload.');
  if (status === 'FAILED' && (draft || provenance.length || !completed || !failure))
    throw new Error('Inconsistent FAILED payload.');
  if (status === 'SUPERSEDED' && (draft || provenance.length))
    throw new Error('Inconsistent SUPERSEDED payload.');
  if (string(generation.plan_version, 'plan_version') !== '1.1')
    throw new Error('Unsupported plan_version.');
  if (string(generation.generation_version, 'generation_version') !== '1.3')
    throw new Error('Unsupported generation_version.');
  if (string(generation.prompt_version, 'prompt_version') !== '1.3')
    throw new Error('Unsupported prompt_version.');
  return {
    generation_id: string(generation.generation_id, 'generation_id'),
    approval_id: string(generation.approval_id, 'approval_id'),
    resume_id: string(generation.resume_id, 'resume_id'),
    job_id: string(generation.job_id, 'job_id'),
    plan_version: '1.1',
    plan_fingerprint: planFingerprint,
    generation_version: '1.3',
    prompt_version: '1.3',
    generation_input_fingerprint: inputFingerprint,
    status,
    provider: string(generation.provider, 'provider'),
    model: string(generation.model, 'model'),
    draft_resume: draft,
    provenance,
    failure_code: failure as CVTransformationGeneration['failure_code'],
    attempt_count: generation.attempt_count,
    created_at: isoDate(generation.created_at, 'created_at'),
    updated_at: isoDate(generation.updated_at, 'updated_at'),
    completed_at: completed,
    requires_truth_validation: true,
    truth_validation_status: 'NOT_RUN',
    applied_to_resume: false,
  };
}

function path(jobId: string, resumeId: string): string {
  return `/jobs/${encodeURIComponent(jobId)}/resumes/${encodeURIComponent(resumeId)}/transformation-plan/generation`;
}

async function response(result: Response): Promise<CVTransformationGeneration> {
  if (!result.ok) {
    const payload = (await result.json().catch(() => ({}))) as {
      detail?: { code?: string; message?: string };
    };
    throw new CVTransformationGenerationApiError(
      payload.detail?.code ?? 'GENERATION_ERROR',
      result.status,
      payload.detail?.message ?? `Generation failed (${result.status}).`
    );
  }
  return parseCVTransformationGeneration(await result.json());
}

async function invoke(endpoint: string, init: RequestInit): Promise<CVTransformationGeneration> {
  try {
    return await response(await apiFetch(endpoint, init));
  } catch (error) {
    if (error instanceof CVTransformationGenerationApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') throw error;
    throw new CVTransformationGenerationApiError(
      'NETWORK_OR_CONTRACT_ERROR',
      0,
      error instanceof Error ? error.message : 'Generation request failed.'
    );
  }
}

export function fetchCurrentCVTransformationGeneration(
  jobId: string,
  resumeId: string,
  signal?: AbortSignal
) {
  return invoke(path(jobId, resumeId), { method: 'GET', signal });
}

export function fetchCVTransformationGeneration(
  jobId: string,
  resumeId: string,
  generationId: string,
  signal?: AbortSignal
) {
  return invoke(`${path(jobId, resumeId)}/${encodeURIComponent(generationId)}`, {
    method: 'GET',
    signal,
  });
}

export function generateCVFromApprovedPlan(
  jobId: string,
  resumeId: string,
  request: CVTransformationGenerationRequest,
  signal?: AbortSignal
) {
  return invoke(path(jobId, resumeId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
    signal,
  });
}
