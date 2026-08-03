import type { Company, CheckIssue } from "./types.js";

/**
 * The guard rail.
 *
 * A tool that makes it easy to send generic mail is worse than no tool: it
 * turns fifteen careful sends into fifteen lazy ones. So the build refuses to
 * render a company whose specific fields are still placeholders, and warns on
 * anything that reads like a mail merge.
 */

const PLACEHOLDER = /^(|TODO|TBD|FIXME|\?+|xxx+|<[^>]*>|\.\.\.)$/i;
const PLACEHOLDER_SUBSTR = /\b(TODO|TBD|FIXME|lorem ipsum|your company|the company|COMPANY_NAME)\b/i;

/** Phrases that signal the sentence could have been sent to anyone. */
const GENERIC_PHRASES = [
  "i am writing to",
  "i would like to express my interest",
  "please consider my application",
  "i am a hardworking",
  "passionate about technology",
  "i hope this email finds you well",
  "please find attached",
  "kindly consider",
  "i am confident that i",
  "great fit for this role",
  "your esteemed organisation",
  "your esteemed organization",
];

/** Roles that mean you did not reach the person who owns the work. */
const NON_ENGINEERING_ROLE = /\b(hr|human resources|talent acquisition|recruit(er|ment)|people ops|careers?)\b/i;
const GENERIC_INBOX = /^(careers?|jobs?|hr|hiring|info|contact|hello|apply|recruit(ing|ment)?)@/i;

const isBlank = (v: unknown): boolean =>
  v === undefined || v === null || (typeof v === "string" && PLACEHOLDER.test(v.trim()));

function requireField(c: Company, path: string, value: unknown, issues: CheckIssue[]) {
  if (isBlank(value)) {
    issues.push({ level: "error", field: path, message: "required, still empty or a placeholder" });
    return;
  }
  if (typeof value === "string" && PLACEHOLDER_SUBSTR.test(value)) {
    issues.push({ level: "error", field: path, message: `contains placeholder text: "${value.trim()}"` });
  }
}

function requireList(c: Company, path: string, value: unknown[] | undefined, min: number, issues: CheckIssue[]) {
  const list = (value ?? []).filter((v) => !isBlank(v));
  if (list.length < min) {
    issues.push({ level: "error", field: path, message: `needs at least ${min} real entries, found ${list.length}` });
  }
}

/** His writing rules from CLAUDE.md, enforced instead of remembered. */
function lintStyle(text: string, field: string, issues: CheckIssue[]) {
  if (text.includes("—")) {
    issues.push({ level: "error", field, message: "contains an em-dash, house style forbids them" });
  }
  if (/\+?\d[\d\s-]{8,}\d/.test(text)) {
    issues.push({ level: "error", field, message: "looks like it contains a phone number, house style keeps it out" });
  }
  for (const phrase of GENERIC_PHRASES) {
    if (text.toLowerCase().includes(phrase)) {
      issues.push({ level: "warn", field, message: `generic phrase: "${phrase}"` });
    }
  }
}

export function checkCompany(c: Company): CheckIssue[] {
  const issues: CheckIssue[] = [];

  requireField(c, "name", c.name, issues);
  requireField(c, "what_they_build", c.what_they_build, issues);
  requireField(c, "positioning", c.positioning, issues);
  requireList(c, "stack", c.stack, 1, issues);

  // Recipient: a person who owns the work, not an inbox.
  requireField(c, "recipient.name", c.recipient?.name, issues);
  requireField(c, "recipient.role", c.recipient?.role, issues);
  requireField(c, "recipient.email", c.recipient?.email, issues);
  requireField(c, "recipient.source", c.recipient?.source, issues);
  if (c.recipient?.role && NON_ENGINEERING_ROLE.test(c.recipient.role)) {
    issues.push({
      level: "warn",
      field: "recipient.role",
      message: "this is an HR or recruiting role, the deck lands better with whoever owns the code",
    });
  }
  if (c.recipient?.email && GENERIC_INBOX.test(c.recipient.email)) {
    issues.push({
      level: "error",
      field: "recipient.email",
      message: "generic inbox, the whole method is aimed at a named person",
    });
  }

  // Hook: the first line has to be about them, and verifiable.
  requireField(c, "hook.what", c.hook?.what, issues);
  requireField(c, "hook.url", c.hook?.url, issues);
  if (c.hook?.url && !/^https?:\/\//i.test(c.hook.url)) {
    issues.push({ level: "error", field: "hook.url", message: "needs a real URL, no URL means you did not verify it" });
  }

  // Finding: the artifact. Without it there is no reason to send the email.
  requireField(c, "finding.type", c.finding?.type, issues);
  requireField(c, "finding.title", c.finding?.title, issues);
  requireField(c, "finding.summary", c.finding?.summary, issues);
  requireField(c, "finding.evidence", c.finding?.evidence, issues);
  if (c.finding?.evidence && c.finding.evidence.trim().length < 40) {
    issues.push({
      level: "warn",
      field: "finding.evidence",
      message: "very short, slide 2 is the slide that earns the interview",
    });
  }
  // Slide 2's evidence block is a fixed box. Past this it clips silently,
  // which is the one failure mode you would not notice before sending.
  const evLines = (c.finding?.evidence ?? "").split("\n").length;
  if (evLines > 26) {
    issues.push({
      level: "warn",
      field: "finding.evidence",
      message: `${evLines} lines, the slide fits about 26 before it clips. Trim it or use evidence_image.`,
    });
  }

  requireList(c, "my_relevant_work", c.my_relevant_work, 2, issues);
  (c.my_relevant_work ?? []).forEach((w, i) => {
    requireField(c, `my_relevant_work[${i}].why_relevant`, w?.why_relevant, issues);
  });

  requireList(c, "first_30_days", c.first_30_days, 3, issues);
  requireList(c, "subject_candidates", c.subject_candidates, 3, issues);

  // Email copy.
  requireField(c, "email.hook_line", c.email?.hook_line, issues);
  requireField(c, "email.finding_line", c.email?.finding_line, issues);
  requireField(c, "email.ask_line", c.email?.ask_line, issues);

  const body = [c.email?.hook_line, c.email?.finding_line, c.email?.ask_line].filter(Boolean).join("\n");
  if (body) lintStyle(body, "email", issues);
  for (const s of c.subject_candidates ?? []) {
    if (typeof s === "string" && s.includes("—")) {
      issues.push({ level: "error", field: "subject_candidates", message: `em-dash in subject: "${s}"` });
    }
  }

  // The mail-merge tell: the company never actually gets named in the copy.
  if (c.name && body && !body.toLowerCase().includes(c.name.toLowerCase().split(" ")[0]!.toLowerCase())) {
    issues.push({
      level: "warn",
      field: "email",
      message: `"${c.name}" is never named in the body, this could have been sent to anyone`,
    });
  }

  const words = body.split(/\s+/).filter(Boolean).length;
  if (words > 130) {
    issues.push({ level: "warn", field: "email", message: `body is ${words} words, target is under 130` });
  }

  return issues;
}

export const hasErrors = (issues: CheckIssue[]) => issues.some((i) => i.level === "error");
