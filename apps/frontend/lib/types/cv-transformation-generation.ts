import type { ResumeData } from '@/components/dashboard/resume-component';

export type CVTransformationGenerationStatus = 'GENERATING' | 'GENERATED' | 'FAILED' | 'SUPERSEDED';
export type CVTransformationGenerationAction =
  | 'KEEP'
  | 'EMPHASIZE'
  | 'DEEMPHASIZE'
  | 'REPHRASE'
  | 'OMIT'
  | 'HUMAN_REVIEW';
export type CVTransformationGenerationMode =
  | 'COPIED'
  | 'COPIED_LOW_PRIORITY'
  | 'GENERATED'
  | 'EXACT_INSERT'
  | 'OMITTED'
  | 'EXCLUDED'
  | 'PRESERVED_AFTER_REVIEW'
  | 'EXCLUDED_UNRESOLVED'
  | 'COPIED_NO_MUTABLE_CONTENT'
  | 'COPIED_PROTECTED_CONTENT'
  | 'EXCLUDED_BY_PARENT';
export type CVTransformationGenerationFailureCode =
  | 'INVALID_LLM_RESPONSE'
  | 'SOURCE_REFERENCE_MISMATCH'
  | 'NUMERIC_BOUNDARY_VIOLATION'
  | 'PROHIBITED_CLAIM_BOUNDARY_VIOLATION'
  | 'LLM_PROVIDER_ERROR'
  | 'DRAFT_ASSEMBLY_FAILED'
  | 'SOURCE_CHANGED_DURING_GENERATION';

export interface CVTransformationProvenance {
  source_reference: string;
  section: 'SUMMARY' | 'EXPERIENCE' | 'COMPETENCIES' | 'ACHIEVEMENTS' | 'TOOLS';
  action: CVTransformationGenerationAction;
  decision: 'ACCEPT' | 'REJECT';
  mode: CVTransformationGenerationMode;
  priority: 'HIGH' | 'NORMAL' | 'LOW';
  source_fingerprint: string;
  output_fingerprint: string | null;
  llm_used: boolean;
  prompt_version: string | null;
  validation_status: 'NOT_RUN';
  boundary_validation_status: 'PASSED' | 'NOT_APPLICABLE';
}

export interface CVTransformationGeneration {
  generation_id: string;
  approval_id: string;
  resume_id: string;
  job_id: string;
  plan_version: '1.1';
  plan_fingerprint: string;
  generation_version: '1.3';
  prompt_version: '1.3';
  generation_input_fingerprint: string;
  status: CVTransformationGenerationStatus;
  provider: string;
  model: string;
  draft_resume: ResumeData | null;
  provenance: CVTransformationProvenance[];
  failure_code: CVTransformationGenerationFailureCode | null;
  attempt_count: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  requires_truth_validation: true;
  truth_validation_status: 'NOT_RUN';
  applied_to_resume: false;
}

export interface CVTransformationGenerationRequest {
  approval_id: string;
  plan_fingerprint: string;
  retry_failed?: boolean;
}
