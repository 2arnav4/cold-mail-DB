import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { ROOT } from "./paths.js";

/**
 * Read side of the contact database this repo already maintains.
 *
 * The scraper, the verifier and the sender all key off turso-full.db, so
 * re-typing company and recipient details into YAML would mean two sources of
 * truth for the same fifteen companies. This opens it read only and never
 * writes: contact state belongs to send_emails.py.
 */

export const DB_PATH = path.resolve(ROOT, "..", "turso-full.db");

export interface DbCompany {
  name: string;
  domain: string;
  sector: string | null;
  industry: string | null;
  description: string | null;
}

export interface DbContact {
  name: string | null;
  role: string | null;
  email: string;
  verified: boolean;
  linkedin_url: string | null;
  source_url: string | null;
}

function open(): DatabaseSync {
  if (!fs.existsSync(DB_PATH)) {
    throw new Error(`Contact database not found at ${DB_PATH}. Run this from inside the Cold-Mail-DB repo.`);
  }
  return new DatabaseSync(DB_PATH, { readOnly: true });
}

/**
 * Ranks candidates the way the tier one method wants them: whoever owns the
 * work first, HR last. `contacts.priority` and `email_verified` come from the
 * existing pipeline and are respected on top of that.
 */
const ROLE_RANK = `
  CASE
    WHEN ct.role IS NULL THEN 50
    WHEN lower(ct.role) LIKE '%founder%'      THEN 0
    WHEN lower(ct.role) LIKE '%cto%'          THEN 0
    WHEN lower(ct.role) LIKE '%engineering%'  THEN 1
    WHEN lower(ct.role) LIKE '%engineer%'     THEN 1
    WHEN lower(ct.role) LIKE '%tech lead%'    THEN 1
    WHEN lower(ct.role) LIKE '%developer%'    THEN 2
    WHEN lower(ct.role) LIKE '%product%'      THEN 3
    WHEN lower(ct.role) LIKE '%ceo%'          THEN 4
    WHEN lower(ct.role) LIKE '%recruit%'      THEN 90
    WHEN lower(ct.role) LIKE '%human resource%' THEN 90
    WHEN lower(ct.role) LIKE '%talent%'       THEN 90
    ELSE 50
  END`;

export function lookup(needle: string): { company: DbCompany; contacts: DbContact[] } | null {
  const db = open();
  try {
    const byEmail = needle.includes("@");
    const sql = `
      SELECT c.name, c.domain, c.sector, c.industry, c.description
      FROM companies c
      ${byEmail ? "JOIN contacts ct ON ct.company_id = c.id WHERE lower(ct.email) = lower(?)" : "WHERE lower(c.domain) = lower(?) OR lower(c.name) = lower(?)"}
      LIMIT 1`;
    const args = byEmail ? [needle] : [needle, needle];
    const company = db.prepare(sql).get(...args) as DbCompany | undefined;
    if (!company) return null;

    const contacts = db
      .prepare(
        `SELECT ct.name, ct.role, ct.email, ct.email_verified AS verified,
                ct.linkedin_url, ct.source_url
         FROM contacts ct
         JOIN companies c ON c.id = ct.company_id
         WHERE lower(c.domain) = lower(?) AND ct.is_invalid = 0 AND ct.email IS NOT NULL
         ORDER BY ${ROLE_RANK} ASC, ct.email_verified DESC, ct.priority DESC
         LIMIT 8`
      )
      .all(company.domain) as any[];

    return {
      company,
      contacts: contacts.map((c) => ({ ...c, verified: !!c.verified })),
    };
  } finally {
    db.close();
  }
}

/** Warn when the pipeline has never confirmed this mailbox exists. */
export function isVerified(email: string): boolean {
  const db = open();
  try {
    const row = db
      .prepare("SELECT email_verified AS v, is_invalid AS bad FROM contacts WHERE lower(email) = lower(?)")
      .get(email) as { v: number; bad: number } | undefined;
    return !!row && !!row.v && !row.bad;
  } finally {
    db.close();
  }
}
