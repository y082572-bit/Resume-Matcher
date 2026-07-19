export type TransformationPlanDecisionValue = 'ACCEPT' | 'REJECT' | 'REQUEST_REVIEW';

export type TransformationPlanApprovalStatus =
  | 'DRAFT'
  | 'APPROVED'
  | 'REQUIRES_REVIEW'
  | 'SUPERSEDED';

export interface TransformationPlanDecision {
  source_reference: string;
  decision: TransformationPlanDecisionValue;
}

export interface CVTransformationPlanApprovalRequest {
  plan_fingerprint: string;
  decisions: TransformationPlanDecision[];
  guardrails_acknowledged: boolean;
  submit: boolean;
}

export interface CVTransformationPlanApproval {
  approval_id: string;
  plan_version: '1.1';
  plan_fingerprint: string;
  resume_id: string;
  job_id: string;
  status: TransformationPlanApprovalStatus;
  decisions: TransformationPlanDecision[];
  guardrails_acknowledged: boolean;
  created_at: string;
  updated_at: string;
}
