import { apiFetch } from './client';
import {
  CareerPositioningResponse,
  PositioningDetails,
  Risks,
  RiskItem,
  CompetencyItem,
  Competencies,
  Strategy,
  NarrativeVariant,
  Evidence,
  Limitations,
  RoleLevel,
  RiskLevel,
  PositioningStrategy,
  TitleTreatment,
  FitLevel,
} from '../types/career-positioning';

export type CPErrorKind =
  | 'RESUME_NOT_FOUND'
  | 'RESUME_NOT_TAILORED'
  | 'JOB_CONTEXT_NOT_FOUND'
  | 'JOB_NOT_FOUND'
  | 'NO_TRUTH_LIBRARY'
  | 'INVALID_TRUTH_LIBRARY'
  | 'TRUTH_LIBRARY_READ_ERROR'
  | 'INVALID_CONTRACT'
  | 'NETWORK_ERROR'
  | 'UNKNOWN';

export class CareerPositioningApiError extends Error {
  public code: string;
  public status: number;
  public kind: CPErrorKind;

  constructor(kind: CPErrorKind, message: string, status = 500, code = 'ERR_CP') {
    super(message);
    this.name = 'CareerPositioningApiError';
    this.kind = kind;
    this.status = status;
    this.code = code;
  }
}

const VALID_ROLE_LEVELS = new Set<string>([
  'UNKNOWN',
  'JUNIOR',
  'SPECIALIST',
  'EXPERT',
  'MANAGER',
  'DIRECTOR',
  'EXECUTIVE',
]);

const VALID_RISK_LEVELS = new Set<string>(['NONE', 'LOW', 'MEDIUM', 'HIGH']);

const VALID_STRATEGIES = new Set<string>([
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

const VALID_TREATMENTS = new Set<string>(['KEEP', 'CONTEXTUALIZE', 'DEEMPHASIZE', 'REVIEW']);

const VALID_FIT_LEVELS = new Set<string>(['LOW', 'MODERATE', 'GOOD', 'STRONG']);

const VALID_NARRATIVES = new Set<string>(['EXPERT', 'MANAGER', 'DIRECTOR']);

const VALID_AVAILABILITY = new Set<string>(['AVAILABLE', 'UNSUPPORTED']);

const ALLOWED_ROOT_KEYS = new Set([
  'schema_version',
  'generated_at',
  'job_id',
  'job_title',
  'positioning',
  'risks',
  'competencies',
  'strategy',
  'evidence',
  'limitations',
]);

const ALLOWED_POS_KEYS = new Set([
  'detected_job_level',
  'candidate_profile_level',
  'level_gap',
  'positioning_strategy',
  'title_treatment',
  'fit_level',
  'fit_score',
  'confidence',
  'requires_human_review',
]);

const ALLOWED_RISKS_KEYS = new Set([
  'overqualification',
  'underqualification',
  'stability_concern',
  'sector_gap',
  'functional_gap',
]);

const ALLOWED_RISK_ITEM_KEYS = new Set(['availability', 'level', 'rationale']);

const ALLOWED_COMP_KEYS = new Set([
  'direct_matches',
  'transferable_matches',
  'missing_critical',
  'excessive_for_role',
]);

const ALLOWED_COMP_ITEM_KEYS = new Set(['name', 'reason', 'source']);

const ALLOWED_STRAT_KEYS = new Set([
  'recommendation',
  'recommended_narrative',
  'elements_to_emphasize',
  'elements_to_flatten',
  'elements_to_remove_or_deprioritize',
  'title_positioning',
  'summary_positioning',
  'narrative_variants',
]);

const ALLOWED_VAR_KEYS = new Set([
  'type',
  'availability',
  'recommended',
  'title_positioning',
  'summary_positioning',
  'elements_to_emphasize',
  'elements_to_flatten',
  'elements_to_remove_or_deprioritize',
  'risk_level',
  'limitations',
]);

const ALLOWED_EV_KEYS = new Set([
  'used_truth_items_count',
  'direct_evidence_count',
  'transferable_evidence_count',
  'unresolved_items_count',
]);

const ALLOWED_LIM_KEYS = new Set(['insufficient_data', 'messages']);

function isObject(val: unknown): val is Record<string, unknown> {
  return typeof val === 'object' && val !== null;
}

function assertExactKeys(
  obj: Record<string, unknown>,
  allowedKeys: Set<string>,
  path: string
): void {
  for (const key of Object.keys(obj)) {
    if (!allowedKeys.has(key)) {
      throw new Error(`Extra field "${key}" found at ${path}`);
    }
  }
}

function isRoleLevel(val: string): val is RoleLevel {
  return VALID_ROLE_LEVELS.has(val);
}

function isRiskLevel(val: string): val is RiskLevel {
  return VALID_RISK_LEVELS.has(val);
}

function isPositioningStrategy(val: string): val is PositioningStrategy {
  return VALID_STRATEGIES.has(val);
}

function isTitleTreatment(val: string): val is TitleTreatment {
  return VALID_TREATMENTS.has(val);
}

function isFitLevel(val: string): val is FitLevel {
  return VALID_FIT_LEVELS.has(val);
}

function checkFinite(val: unknown, name: string): number {
  if (typeof val !== 'number' || !Number.isFinite(val) || Number.isNaN(val)) {
    throw new Error(`Field ${name} is not a finite number.`);
  }
  return val;
}

function checkSafeInteger(val: unknown, name: string): number {
  if (typeof val !== 'number' || !Number.isSafeInteger(val) || val < 0) {
    throw new Error(`Field ${name} is not a non-negative safe integer.`);
  }
  return val;
}

function checkString(val: unknown, name: string): string {
  if (typeof val !== 'string' || !val.trim()) {
    throw new Error(`Field ${name} must be a non-empty string.`);
  }
  return val;
}

function checkStringList(list: unknown, name: string): string[] {
  if (!Array.isArray(list)) {
    throw new Error(`Field ${name} is not an array.`);
  }
  const validated: string[] = [];
  for (const item of list) {
    if (typeof item !== 'string' || !item.trim()) {
      throw new Error(`Array ${name} contains empty or invalid string.`);
    }
    validated.push(item);
  }
  return validated;
}

function checkStringListNoDuplicates(list: unknown, name: string): string[] {
  const arr = checkStringList(list, name);
  const seen = new Set<string>();
  for (const item of arr) {
    const trimmed = item.trim();
    if (seen.has(trimmed)) {
      throw new Error(`Duplicate element found in ${name}: ${item}`);
    }
    seen.add(trimmed);
  }
  return arr;
}

export function parseCareerPositioningResponse(data: unknown): CareerPositioningResponse {
  if (!isObject(data)) {
    throw new Error('Career positioning response is not an object.');
  }
  assertExactKeys(data, ALLOWED_ROOT_KEYS, 'response');

  if (data.schema_version !== '1.0') {
    throw new Error(`Invalid schema version: expected "1.0", got ${String(data.schema_version)}`);
  }

  const generatedAt = checkString(data.generated_at, 'generated_at');
  const timezoneRegex = /(Z|([+-]\d{2}:\d{2}))$/;
  if (!timezoneRegex.test(generatedAt)) {
    throw new Error('generated_at timestamp must have a timezone (Z or offset).');
  }
  const d = new Date(generatedAt);
  if (Number.isNaN(d.getTime())) {
    throw new Error('Invalid generated_at timestamp.');
  }

  const jobId = checkString(data.job_id, 'job_id');
  const jobTitle = checkString(data.job_title, 'job_title');

  const pos = data.positioning;
  if (!isObject(pos)) {
    throw new Error('Positioning details missing or invalid.');
  }
  assertExactKeys(pos, ALLOWED_POS_KEYS, 'positioning');

  const detectedJobLevelStr = checkString(pos.detected_job_level, 'detected_job_level');
  if (!isRoleLevel(detectedJobLevelStr)) {
    throw new Error(`Invalid detected_job_level: ${detectedJobLevelStr}`);
  }
  const detectedJobLevel = detectedJobLevelStr;

  const candidateProfileLevelStr = checkString(
    pos.candidate_profile_level,
    'candidate_profile_level'
  );
  if (!isRoleLevel(candidateProfileLevelStr)) {
    throw new Error(`Invalid candidate_profile_level: ${candidateProfileLevelStr}`);
  }
  const candidateProfileLevel = candidateProfileLevelStr;

  const levelGap = checkFinite(pos.level_gap, 'level_gap');
  if (!Number.isSafeInteger(levelGap)) {
    throw new Error('level_gap must be an integer.');
  }

  const positioningStrategyStr = checkString(pos.positioning_strategy, 'positioning_strategy');
  if (!isPositioningStrategy(positioningStrategyStr)) {
    throw new Error(`Invalid positioning_strategy: ${positioningStrategyStr}`);
  }
  const positioningStrategy = positioningStrategyStr;

  const titleTreatmentStr = checkString(pos.title_treatment, 'title_treatment');
  if (!isTitleTreatment(titleTreatmentStr)) {
    throw new Error(`Invalid title_treatment: ${titleTreatmentStr}`);
  }
  const titleTreatment = titleTreatmentStr;

  const fitLevelStr = checkString(pos.fit_level, 'fit_level');
  if (!isFitLevel(fitLevelStr)) {
    throw new Error(`Invalid fit_level: ${fitLevelStr}`);
  }
  const fitLevel = fitLevelStr;

  const fitScore = checkFinite(pos.fit_score, 'fit_score');
  if (fitScore < 0 || fitScore > 100) {
    throw new Error(`fit_score out of range [0, 100]: ${fitScore}`);
  }

  const confidence = checkFinite(pos.confidence, 'confidence');
  if (confidence < 0 || confidence > 1) {
    throw new Error(`confidence out of range [0, 1]: ${confidence}`);
  }

  if (typeof pos.requires_human_review !== 'boolean') {
    throw new Error('requires_human_review must be a boolean.');
  }
  const requiresHumanReview = pos.requires_human_review;

  const positioningDetails: PositioningDetails = {
    detected_job_level: detectedJobLevel,
    candidate_profile_level: candidateProfileLevel,
    level_gap: levelGap,
    positioning_strategy: positioningStrategy,
    title_treatment: titleTreatment,
    fit_level: fitLevel,
    fit_score: fitScore,
    confidence: confidence,
    requires_human_review: requiresHumanReview,
  };

  const r = data.risks;
  if (!isObject(r)) {
    throw new Error('Risks missing or invalid.');
  }
  assertExactKeys(r, ALLOWED_RISKS_KEYS, 'risks');

  const parseRiskItem = (item: unknown, name: string): RiskItem => {
    if (!isObject(item)) {
      throw new Error(`Risk item ${name} must be an object.`);
    }
    assertExactKeys(item, ALLOWED_RISK_ITEM_KEYS, `risks.${name}`);

    const availability = checkString(item.availability, `${name}.availability`);
    if (!VALID_AVAILABILITY.has(availability)) {
      throw new Error(`Invalid availability in ${name}: ${availability}`);
    }

    let level: RiskLevel | null = null;
    if (availability === 'AVAILABLE') {
      const lvlStr = checkString(item.level, `${name}.level`);
      if (!isRiskLevel(lvlStr)) {
        throw new Error(`Invalid risk level in ${name}: ${lvlStr}`);
      }
      level = lvlStr;
    } else {
      if (item.level !== null) {
        throw new Error(`Risk level in ${name} must be null when unavailable.`);
      }
    }

    const rationale = checkStringListNoDuplicates(item.rationale, `${name}.rationale`);
    return {
      availability: availability as 'AVAILABLE' | 'UNSUPPORTED',
      level,
      rationale,
    };
  };

  const risks: Risks = {
    overqualification: parseRiskItem(r.overqualification, 'overqualification'),
    underqualification: parseRiskItem(r.underqualification, 'underqualification'),
    stability_concern: parseRiskItem(r.stability_concern, 'stability_concern'),
    sector_gap: parseRiskItem(r.sector_gap, 'sector_gap'),
    functional_gap: parseRiskItem(r.functional_gap, 'functional_gap'),
  };

  const comp = data.competencies;
  if (!isObject(comp)) {
    throw new Error('Competencies missing or invalid.');
  }
  assertExactKeys(comp, ALLOWED_COMP_KEYS, 'competencies');

  const seenCompetencies = new Set<string>();
  const parseCompetencyList = (list: unknown, name: string): CompetencyItem[] => {
    if (!Array.isArray(list)) {
      throw new Error(`Competencies: ${name} is not an array.`);
    }
    const validated: CompetencyItem[] = [];
    for (const item of list) {
      if (!isObject(item)) {
        throw new Error(`Competency item in ${name} is not an object.`);
      }
      assertExactKeys(item, ALLOWED_COMP_ITEM_KEYS, `competencies.${name}.item`);
      const cName = checkString(item.name, `${name}.name`);
      const reason = checkString(item.reason, `${name}.reason`);
      const source = checkString(item.source, `${name}.source`);

      const normName = cName.trim().toLowerCase();
      if (seenCompetencies.has(normName)) {
        throw new Error(`Duplicate competency name found across lists: ${cName}`);
      }
      seenCompetencies.add(normName);

      validated.push({
        name: cName,
        reason,
        source,
      });
    }
    return validated;
  };

  const competencies: Competencies = {
    direct_matches: parseCompetencyList(comp.direct_matches, 'direct_matches'),
    transferable_matches: parseCompetencyList(comp.transferable_matches, 'transferable_matches'),
    missing_critical: parseCompetencyList(comp.missing_critical, 'missing_critical'),
    excessive_for_role: parseCompetencyList(comp.excessive_for_role, 'excessive_for_role'),
  };

  const strat = data.strategy;
  if (!isObject(strat)) {
    throw new Error('Strategy missing or invalid.');
  }
  assertExactKeys(strat, ALLOWED_STRAT_KEYS, 'strategy');

  const recommendation = checkString(strat.recommendation, 'strategy.recommendation');

  let recommendedNarrative: 'EXPERT' | 'MANAGER' | 'DIRECTOR' | null = null;
  if (strat.recommended_narrative !== null && strat.recommended_narrative !== undefined) {
    const recNarrStr = checkString(strat.recommended_narrative, 'strategy.recommended_narrative');
    if (!VALID_NARRATIVES.has(recNarrStr)) {
      throw new Error(`Invalid recommended_narrative: ${recNarrStr}`);
    }
    recommendedNarrative = recNarrStr as 'EXPERT' | 'MANAGER' | 'DIRECTOR';
  }

  const em = checkStringListNoDuplicates(
    strat.elements_to_emphasize,
    'strategy.elements_to_emphasize'
  );
  const fl = checkStringListNoDuplicates(strat.elements_to_flatten, 'strategy.elements_to_flatten');
  const rm = checkStringListNoDuplicates(
    strat.elements_to_remove_or_deprioritize,
    'strategy.elements_to_remove_or_deprioritize'
  );

  let titlePos: string | null = null;
  if (strat.title_positioning !== null && strat.title_positioning !== undefined) {
    titlePos = checkString(strat.title_positioning, 'strategy.title_positioning');
  }

  let summaryPos: string | null = null;
  if (strat.summary_positioning !== null && strat.summary_positioning !== undefined) {
    summaryPos = checkString(strat.summary_positioning, 'strategy.summary_positioning');
  }

  const rawVariants = strat.narrative_variants;
  if (!Array.isArray(rawVariants) || rawVariants.length !== 3) {
    throw new Error('narrative_variants must be an array of exactly 3 variants.');
  }

  const narrativeVariants: NarrativeVariant[] = [];
  const seenVariantTypes = new Set<string>();

  for (const item of rawVariants) {
    if (!isObject(item)) {
      throw new Error('Narrative variant is not an object.');
    }
    assertExactKeys(item, ALLOWED_VAR_KEYS, 'strategy.narrative_variants.item');

    const type = checkString(item.type, 'variant.type');
    if (!VALID_NARRATIVES.has(type)) {
      throw new Error(`Invalid variant type: ${type}`);
    }
    if (seenVariantTypes.has(type)) {
      throw new Error(`Duplicate narrative variant type: ${type}`);
    }
    seenVariantTypes.add(type);

    const availability = checkString(item.availability, 'variant.availability');
    if (!VALID_AVAILABILITY.has(availability)) {
      throw new Error(`Invalid variant availability: ${availability}`);
    }

    if (typeof item.recommended !== 'boolean') {
      throw new Error('variant.recommended must be a boolean.');
    }

    let tPos: string | null = null;
    let sPos: string | null = null;
    if (availability === 'UNSUPPORTED') {
      if (item.title_positioning !== null || item.summary_positioning !== null) {
        throw new Error(
          'UNSUPPORTED narrative variant must have null title_positioning and summary_positioning'
        );
      }
    } else {
      tPos = checkString(item.title_positioning, 'variant.title_positioning');
      sPos = checkString(item.summary_positioning, 'variant.summary_positioning');
    }

    const vEm = checkStringListNoDuplicates(
      item.elements_to_emphasize,
      'variant.elements_to_emphasize'
    );
    const vFl = checkStringListNoDuplicates(
      item.elements_to_flatten,
      'variant.elements_to_flatten'
    );
    const vRm = checkStringListNoDuplicates(
      item.elements_to_remove_or_deprioritize,
      'variant.elements_to_remove_or_deprioritize'
    );

    const riskLvlStr = checkString(item.risk_level, 'variant.risk_level');
    if (!isRiskLevel(riskLvlStr)) {
      throw new Error(`Invalid variant risk level: ${riskLvlStr}`);
    }
    const riskLvl = riskLvlStr;

    const limitations = checkStringListNoDuplicates(item.limitations, 'variant.limitations');

    narrativeVariants.push({
      type: type as 'EXPERT' | 'MANAGER' | 'DIRECTOR',
      availability: availability as 'AVAILABLE' | 'UNSUPPORTED',
      recommended: item.recommended,
      title_positioning: tPos,
      summary_positioning: sPos,
      elements_to_emphasize: vEm,
      elements_to_flatten: vFl,
      elements_to_remove_or_deprioritize: vRm,
      risk_level: riskLvl,
      limitations: limitations,
    });
  }

  // Exact set of three types checks
  const expectedTypes = new Set(['EXPERT', 'MANAGER', 'DIRECTOR']);
  for (const v of narrativeVariants) {
    expectedTypes.delete(v.type);
  }
  if (expectedTypes.size > 0) {
    throw new Error('narrative_variants must contain exactly EXPERT, MANAGER, and DIRECTOR.');
  }

  // Exactly one recommended = true
  const recommended = narrativeVariants.filter((v) => v.recommended);
  if (recommendedNarrative === null) {
    if (recommended.length !== 0) {
      throw new Error(
        'Zero narrative variants must be marked as recommended when recommended_narrative is null.'
      );
    }
  } else {
    if (recommended.length !== 1) {
      throw new Error('Exactly one narrative variant must be marked as recommended.');
    }
    // Type matches recommended_narrative
    if (recommended[0].type !== recommendedNarrative) {
      throw new Error('Recommended variant type must match recommended_narrative.');
    }
    // Recommended variant cannot be UNSUPPORTED
    if (recommended[0].availability === 'UNSUPPORTED') {
      throw new Error('Recommended narrative variant cannot be UNSUPPORTED.');
    }
  }

  const strategy: Strategy = {
    recommendation,
    recommended_narrative: recommendedNarrative,
    elements_to_emphasize: em,
    elements_to_flatten: fl,
    elements_to_remove_or_deprioritize: rm,
    title_positioning: titlePos,
    summary_positioning: summaryPos,
    narrative_variants: narrativeVariants,
  };

  const ev = data.evidence;
  if (!isObject(ev)) {
    throw new Error('Evidence missing or invalid.');
  }
  assertExactKeys(ev, ALLOWED_EV_KEYS, 'evidence');

  const evidence: Evidence = {
    used_truth_items_count: checkSafeInteger(ev.used_truth_items_count, 'used_truth_items_count'),
    direct_evidence_count: checkSafeInteger(ev.direct_evidence_count, 'direct_evidence_count'),
    transferable_evidence_count: checkSafeInteger(
      ev.transferable_evidence_count,
      'transferable_evidence_count'
    ),
    unresolved_items_count: checkSafeInteger(ev.unresolved_items_count, 'unresolved_items_count'),
  };

  const lim = data.limitations;
  if (!isObject(lim)) {
    throw new Error('Limitations missing or invalid.');
  }
  assertExactKeys(lim, ALLOWED_LIM_KEYS, 'limitations');

  if (typeof lim.insufficient_data !== 'boolean') {
    throw new Error('insufficient_data must be a boolean.');
  }
  const limitations: Limitations = {
    insufficient_data: lim.insufficient_data,
    messages: checkStringListNoDuplicates(lim.messages, 'limitations.messages'),
  };

  // Rule A & Rule B cross-field validation
  const cLevel = candidateProfileLevel;
  const insufficient = limitations.insufficient_data;
  const requiresReview = positioningDetails.requires_human_review;

  // A. Gdy candidate_profile_level != UNKNOWN i limitations.insufficient_data = false
  if (cLevel !== 'UNKNOWN' && !insufficient) {
    if (strategy.recommended_narrative === null) {
      throw new Error('recommended_narrative cannot be null when data is sufficient');
    }
    const recommendedVariants = strategy.narrative_variants.filter((v) => v.recommended);
    if (recommendedVariants.length !== 1) {
      throw new Error('Exactly one narrative variant must be recommended when data is sufficient');
    }
    if (recommendedVariants[0].type !== strategy.recommended_narrative) {
      throw new Error('Recommended variant type does not match recommended_narrative');
    }
    if (strategy.title_positioning === null || !strategy.title_positioning.trim()) {
      throw new Error('title_positioning must be provided when data is sufficient');
    }
    if (strategy.summary_positioning === null || !strategy.summary_positioning.trim()) {
      throw new Error('summary_positioning must be provided when data is sufficient');
    }
  }

  // B. Gdy candidate_profile_level = UNKNOWN lub limitations.insufficient_data = true
  if (cLevel === 'UNKNOWN' || insufficient) {
    if (!requiresReview) {
      throw new Error(
        'requires_human_review must be true when level is UNKNOWN or data is insufficient'
      );
    }
    if (!insufficient) {
      throw new Error('insufficient_data must be true when level is UNKNOWN');
    }
    if (strategy.recommended_narrative !== null) {
      throw new Error('recommended_narrative must be null when data is insufficient');
    }
    const recommendedVariants = strategy.narrative_variants.filter((v) => v.recommended);
    if (recommendedVariants.length !== 0) {
      throw new Error('Zero narrative variants must be recommended when data is insufficient');
    }
    for (const v of strategy.narrative_variants) {
      if (v.availability !== 'UNSUPPORTED') {
        throw new Error(`Variant ${v.type} must be UNSUPPORTED when data is insufficient`);
      }
    }
    if (strategy.title_positioning !== null) {
      throw new Error('title_positioning must be null when data is insufficient');
    }
    if (strategy.summary_positioning !== null) {
      throw new Error('summary_positioning must be null when data is insufficient');
    }
    if (strategy.elements_to_emphasize.length > 0) {
      throw new Error('elements_to_emphasize must be empty when data is insufficient');
    }
    if (strategy.elements_to_flatten.length > 0) {
      throw new Error('elements_to_flatten must be empty when data is insufficient');
    }
    if (strategy.elements_to_remove_or_deprioritize.length > 0) {
      throw new Error('elements_to_remove_or_deprioritize must be empty when data is insufficient');
    }
  }

  const validatedResponse: CareerPositioningResponse = {
    schema_version: '1.0',
    generated_at: generatedAt,
    job_id: jobId,
    job_title: jobTitle,
    positioning: positioningDetails,
    risks,
    competencies,
    strategy,
    evidence,
    limitations,
  };

  return validatedResponse;
}

export async function fetchCareerPositioning(
  jobId: string,
  options?: { signal?: AbortSignal }
): Promise<CareerPositioningResponse> {
  let resp: Response;
  try {
    resp = await apiFetch(`/jobs/${jobId}/career-positioning`, { signal: options?.signal });
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw err;
    }
    throw new CareerPositioningApiError('NETWORK_ERROR', 'Network communication failed.');
  }

  if (!resp.ok) {
    let errMsg = 'API request failed';
    let errCode = 'UNKNOWN_ERROR';
    try {
      const errData = await resp.json();
      if (isObject(errData) && isObject(errData.detail)) {
        if (typeof errData.detail.code === 'string') {
          errCode = errData.detail.code;
        }
        if (typeof errData.detail.message === 'string') {
          errMsg = errData.detail.message;
        }
      } else if (isObject(errData) && typeof errData.detail === 'string') {
        errMsg = errData.detail;
      }
    } catch {
      // ignore
    }

    if (errCode === 'NO_TRUTH_LIBRARY') {
      throw new CareerPositioningApiError('NO_TRUTH_LIBRARY', errMsg, resp.status, errCode);
    }
    if (errCode === 'JOB_NOT_FOUND' || resp.status === 404) {
      throw new CareerPositioningApiError('JOB_NOT_FOUND', errMsg, resp.status, errCode);
    }
    if (errCode === 'INVALID_TRUTH_LIBRARY') {
      throw new CareerPositioningApiError('INVALID_TRUTH_LIBRARY', errMsg, resp.status, errCode);
    }
    if (errCode === 'TRUTH_LIBRARY_READ_ERROR') {
      throw new CareerPositioningApiError('TRUTH_LIBRARY_READ_ERROR', errMsg, resp.status, errCode);
    }
    if (resp.status === 400 || resp.status === 422) {
      throw new CareerPositioningApiError('INVALID_CONTRACT', errMsg, resp.status, errCode);
    }
    throw new CareerPositioningApiError('UNKNOWN', errMsg, resp.status, errCode);
  }

  let data: unknown;
  try {
    data = await resp.json();
  } catch {
    throw new CareerPositioningApiError('INVALID_CONTRACT', 'Response is not valid JSON');
  }

  try {
    return parseCareerPositioningResponse(data);
  } catch (validationError: unknown) {
    const msg = validationError instanceof Error ? validationError.message : 'Validation failed';
    throw new CareerPositioningApiError('INVALID_CONTRACT', `Contract validation failed: ${msg}`);
  }
}

export async function fetchResumeJobContext(
  resumeId: string,
  options?: { signal?: AbortSignal }
): Promise<{ job_id: string; job_title?: string }> {
  let resp: Response;
  try {
    resp = await apiFetch(`/resumes/${resumeId}/job-description`, { signal: options?.signal });
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw err;
    }
    throw new CareerPositioningApiError('NETWORK_ERROR', 'Network communication failed.');
  }

  if (!resp.ok) {
    let errMsg = 'API request failed';
    try {
      const errData = await resp.json();
      if (isObject(errData) && typeof errData.detail === 'string') {
        errMsg = errData.detail;
      }
    } catch {
      // ignore
    }
    if (resp.status === 404) {
      throw new CareerPositioningApiError('JOB_CONTEXT_NOT_FOUND', errMsg, 404);
    }
    throw new CareerPositioningApiError('UNKNOWN', errMsg, resp.status);
  }

  let data: unknown;
  try {
    data = await resp.json();
  } catch {
    throw new CareerPositioningApiError('INVALID_CONTRACT', 'Response is not valid JSON');
  }

  if (!isObject(data) || typeof data.job_id !== 'string' || !data.job_id) {
    throw new CareerPositioningApiError(
      'INVALID_CONTRACT',
      'Invalid job description context format.'
    );
  }

  const jobTitle =
    typeof data.content === 'string'
      ? data.content
          .split(/\r?\n/)
          .map((line) => line.trim())
          .find(Boolean)
      : undefined;
  return { job_id: data.job_id, ...(jobTitle ? { job_title: jobTitle } : {}) };
}

export async function fetchResumePositioningContext(
  resumeId: string,
  options?: { signal?: AbortSignal }
): Promise<{ resume_id: string; parent_id: string }> {
  let resp: Response;
  try {
    resp = await apiFetch(`/resumes?resume_id=${encodeURIComponent(resumeId)}`, {
      signal: options?.signal,
    });
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw err;
    }
    throw new CareerPositioningApiError('NETWORK_ERROR', 'Network communication failed.');
  }

  if (!resp.ok) {
    let errMsg = 'API request failed';
    try {
      const errData = await resp.json();
      if (isObject(errData) && typeof errData.detail === 'string') {
        errMsg = errData.detail;
      }
    } catch {
      // ignore
    }
    if (resp.status === 404) {
      throw new CareerPositioningApiError('RESUME_NOT_FOUND', errMsg, 404);
    }
    throw new CareerPositioningApiError('UNKNOWN', errMsg, resp.status);
  }

  let payload: unknown;
  try {
    payload = await resp.json();
  } catch {
    throw new CareerPositioningApiError('INVALID_CONTRACT', 'Response is not valid JSON');
  }

  if (!isObject(payload) || !isObject(payload.data)) {
    throw new CareerPositioningApiError('INVALID_CONTRACT', 'Resume response is not an object.');
  }

  const resume_id = payload.data.resume_id;
  const parent_id = payload.data.parent_id;

  if (typeof resume_id !== 'string' || !resume_id.trim()) {
    throw new CareerPositioningApiError('INVALID_CONTRACT', 'Invalid or missing resume id.');
  }

  if (
    parent_id === null ||
    parent_id === undefined ||
    (typeof parent_id === 'string' && !parent_id.trim())
  ) {
    throw new CareerPositioningApiError(
      'RESUME_NOT_TAILORED',
      'Resume must be tailored to a job description.'
    );
  }

  if (typeof parent_id !== 'string') {
    throw new CareerPositioningApiError('INVALID_CONTRACT', 'parent_id must be a string.');
  }

  return {
    resume_id,
    parent_id,
  };
}
