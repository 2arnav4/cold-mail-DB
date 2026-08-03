/**
 * The shape of a companies/<slug>.yaml file.
 *
 * Everything here is hand-authored. The research command writes to
 * research/<slug>.json instead, so machine-gathered facts never get
 * silently promoted into the pitch. The angle is always a human call.
 */

export type FindingType = "pr" | "bug" | "tool" | "teardown";

export interface Recipient {
  name: string;
  role: string;
  email: string;
  source: string;
}

export interface Hook {
  what: string;
  url: string;
}

export interface Finding {
  type: FindingType;
  title: string;
  summary: string;
  evidence: string;
  evidence_image?: string;
  link?: string;
}

export interface RelevantWork {
  project: string;
  what: string;
  why_relevant: string;
  link?: string;
}

export interface EmailCopy {
  hook_line: string;
  finding_line: string;
  ask_line: string;
}

export interface Company {
  slug: string;
  name: string;
  what_they_build: string;
  stack: string[];
  brand: { primary: string; accent?: string };
  recipient: Recipient;
  hook: Hook;
  finding: Finding;
  my_relevant_work: RelevantWork[];
  first_30_days: string[];
  positioning: string;
  subject_candidates: string[];
  subject_chosen?: number;
  email: EmailCopy;
  resources: { label: string; url: string }[];
}

export interface Sender {
  name: string;
  headline: string;
  email: string;
  signature_lines: string[];
}

export interface CheckIssue {
  level: "error" | "warn";
  field: string;
  message: string;
}
