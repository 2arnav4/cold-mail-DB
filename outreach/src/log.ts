import fs from "node:fs";
import { LOG } from "./paths.js";

/**
 * Fifteen sends is a small enough sample that the only way to learn anything
 * is to write down what you did. Opens are a subject-line diagnostic. Replies
 * are the metric.
 */

const COLUMNS = ["slug", "company", "recipient", "role", "subject", "sent", "opened", "replied", "outcome"] as const;
type Column = (typeof COLUMNS)[number];
type Row = Record<Column, string>;

const esc = (v: string) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);

function parse(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]!;
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cur += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { out.push(cur); cur = ""; }
    else cur += ch;
  }
  out.push(cur);
  return out;
}

export function readLog(): Row[] {
  if (!fs.existsSync(LOG)) return [];
  const lines = fs.readFileSync(LOG, "utf8").trim().split("\n");
  if (lines.length < 2) return [];
  return lines.slice(1).map((l) => {
    const cells = parse(l);
    return Object.fromEntries(COLUMNS.map((c, i) => [c, cells[i] ?? ""])) as Row;
  });
}

export function writeLog(rows: Row[]) {
  const body = rows.map((r) => COLUMNS.map((c) => esc(r[c] ?? "")).join(",")).join("\n");
  fs.writeFileSync(LOG, `${COLUMNS.join(",")}\n${body}\n`);
}

export function upsert(slug: string, patch: Partial<Row>) {
  const rows = readLog();
  const existing = rows.find((r) => r.slug === slug);
  if (existing) Object.assign(existing, patch);
  else rows.push({ ...(Object.fromEntries(COLUMNS.map((c) => [c, ""])) as Row), slug, ...patch });
  writeLog(rows);
}
