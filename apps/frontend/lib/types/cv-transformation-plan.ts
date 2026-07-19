import type { PositioningStrategy, RoleLevel } from './career-positioning';

export type CVTransformationAction =
  | 'KEEP'
  | 'EMPHASIZE'
  | 'DEEMPHASIZE'
  | 'REPHRASE'
  | 'OMIT'
  | 'HUMAN_REVIEW';

export type CVTransformationSection =
  | 'SUMMARY'
  | 'EXPERIENCE'
  | 'COMPETENCIES'
  | 'ACHIEVEMENTS'
  | 'TOOLS';

export type EvidenceStrength = 'NONE' | 'WEAK' | 'MODERATE' | 'STRONG';

export type ProhibitedClaimCode =
  | 'PNL_WITHOUT_EVIDENCE'
  | 'BUDGET_WITHOUT_EVIDENCE'
  | 'BOARD_WITHOUT_EVIDENCE'
  | 'PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE'
  | 'MANAGING_MANAGERS_WITHOUT_EVIDENCE'
  | 'TEAM_SIZE_WITHOUT_EVIDENCE'
  | 'TECHNICAL_SKILL_WITHOUT_EVIDENCE'
  | 'LANGUAGE_LEVEL_WITHOUT_EVIDENCE'
  | 'CERTIFICATION_WITHOUT_EVIDENCE'
  | 'QUANTIFIED_RESULT_WITHOUT_EVIDENCE';

export type EvidenceClaimCode =
  | ProhibitedClaimCode
  | 'STRATEGY_RESPONSIBILITY_EVIDENCE'
  | 'ORGANIZATIONAL_SCALE_EVIDENCE'
  | 'COMPETENCY_EVIDENCE';

export type EvidenceAllowedOperation = 'KEEP_EXISTING' | 'EMPHASIZE_EXISTING' | 'REPHRASE_EXISTING';

export interface CVTransformationPlanItem {
  action: CVTransformationAction;
  section: CVTransformationSection;
  source_reference: string;
  display_label: string;
  reason_code: string;
  reason: string;
  evidence_strength: EvidenceStrength;
  human_review_required: boolean;
}

export interface EvidencePermission {
  claim_code: EvidenceClaimCode;
  source_reference: string;
  evidence_strength: 'MODERATE' | 'STRONG';
  allowed_operation: EvidenceAllowedOperation;
  human_review_required: boolean;
}

export interface ProhibitedClaim {
  code: ProhibitedClaimCode;
  reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM';
  reason: string;
  human_review_required: true;
}

export interface TransformationSourceSummary {
  approved_truth_items_count: number;
  resume_items_count: number;
  job_requirements_count: number;
  career_positioning_schema_version: '1.0';
}

export interface CVTransformationPlan {
  plan_version: '1.1';
  plan_fingerprint: string;
  resume_id: string;
  job_id: string;
  generated_at: string;
  detected_job_level: RoleLevel;
  candidate_profile_level: RoleLevel;
  positioning_strategy: PositioningStrategy;
  requires_human_review: boolean;
  summary_plan: CVTransformationPlanItem[];
  experience_plan: CVTransformationPlanItem[];
  competencies_plan: CVTransformationPlanItem[];
  achievements_plan: CVTransformationPlanItem[];
  tools_plan: CVTransformationPlanItem[];
  evidence_permissions: EvidencePermission[];
  prohibited_claims: ProhibitedClaim[];
  limitations: string[];
  source_summary: TransformationSourceSummary;
}
