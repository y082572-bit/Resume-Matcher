/**
 * Career Positioning TypeScript Definitions
 *
 * Must align exactly with apps/backend/app/schemas/career_positioning.py.
 */

export type RoleLevel =
  | 'UNKNOWN'
  | 'JUNIOR'
  | 'SPECIALIST'
  | 'EXPERT'
  | 'MANAGER'
  | 'DIRECTOR'
  | 'EXECUTIVE';

export type RiskLevel = 'NONE' | 'LOW' | 'MEDIUM' | 'HIGH';

export type PositioningStrategy =
  | 'REVIEW_REQUIRED'
  | 'JUNIOR_ENTRY'
  | 'SPECIALIST_DELIVERY'
  | 'EXPERT_AUTHORITY'
  | 'MANAGER_LEADERSHIP'
  | 'DIRECTOR_SCALE'
  | 'EXECUTIVE_ENTERPRISE'
  | 'CONTROLLED_FLATTENING'
  | 'CONTROLLED_ELEVATION'
  | 'BALANCED';

export type TitleTreatment = 'KEEP' | 'CONTEXTUALIZE' | 'DEEMPHASIZE' | 'REVIEW';

export type FitLevel = 'LOW' | 'MODERATE' | 'GOOD' | 'STRONG';

export interface PositioningDetails {
  detected_job_level: RoleLevel;
  candidate_profile_level: RoleLevel;
  level_gap: number;
  positioning_strategy: PositioningStrategy;
  title_treatment: TitleTreatment;
  fit_level: FitLevel;
  fit_score: number;
  confidence: number;
  requires_human_review: boolean;
}

export interface RiskItem {
  availability: 'AVAILABLE' | 'UNSUPPORTED';
  level: RiskLevel | null;
  rationale: string[];
}

export interface Risks {
  overqualification: RiskItem;
  underqualification: RiskItem;
  stability_concern: RiskItem;
  sector_gap: RiskItem;
  functional_gap: RiskItem;
}

export interface CompetencyItem {
  name: string;
  reason: string;
  source: string;
}

export interface Competencies {
  direct_matches: CompetencyItem[];
  transferable_matches: CompetencyItem[];
  missing_critical: CompetencyItem[];
  excessive_for_role: CompetencyItem[];
}

export interface NarrativeVariant {
  type: 'EXPERT' | 'MANAGER' | 'DIRECTOR';
  availability: 'AVAILABLE' | 'UNSUPPORTED';
  recommended: boolean;
  title_positioning: string | null;
  summary_positioning: string | null;
  elements_to_emphasize: string[];
  elements_to_flatten: string[];
  elements_to_remove_or_deprioritize: string[];
  risk_level: RiskLevel;
  limitations: string[];
}

export interface Strategy {
  recommendation: string;
  recommended_narrative: 'EXPERT' | 'MANAGER' | 'DIRECTOR' | null;
  elements_to_emphasize: string[];
  elements_to_flatten: string[];
  elements_to_remove_or_deprioritize: string[];
  title_positioning: string | null;
  summary_positioning: string | null;
  narrative_variants: NarrativeVariant[];
}

export interface Evidence {
  used_truth_items_count: number;
  direct_evidence_count: number;
  transferable_evidence_count: number;
  unresolved_items_count: number;
}

export interface Limitations {
  insufficient_data: boolean;
  messages: string[];
}

export interface CareerPositioningResponse {
  schema_version: '1.0';
  generated_at: string; // ISO datetime
  job_id: string;
  job_title: string;
  positioning: PositioningDetails;
  risks: Risks;
  competencies: Competencies;
  strategy: Strategy;
  evidence: Evidence;
  limitations: Limitations;
}
