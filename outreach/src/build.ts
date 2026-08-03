import fs from "node:fs";
import path from "node:path";
import ejs from "ejs";
import yaml from "js-yaml";
import puppeteer from "puppeteer";
import type { Company, Sender, CheckIssue } from "./types.js";
import { checkCompany, hasErrors } from "./check.js";
import { isVerified } from "./db.js";
import { COMPANIES, TEMPLATES, SENDER, companyFile, outDir } from "./paths.js";

export function loadSender(): Sender {
  return yaml.load(fs.readFileSync(SENDER, "utf8")) as Sender;
}

export function loadCompany(slug: string): Company {
  const c = yaml.load(fs.readFileSync(companyFile(slug), "utf8")) as Company;
  c.slug = slug;
  return c;
}

export function allSlugs(): string[] {
  if (!fs.existsSync(COMPANIES)) return [];
  return fs
    .readdirSync(COMPANIES)
    .filter((f) => f.endsWith(".yaml") && !f.startsWith("_"))
    .map((f) => f.replace(/\.yaml$/, ""))
    .sort();
}

function chosenSubject(c: Company): string {
  const i = c.subject_chosen ?? 0;
  return c.subject_candidates[i] ?? c.subject_candidates[0]!;
}

/**
 * Renders the deck through a real browser rather than a PDF library, because
 * the slides are laid out with CSS grid and flexbox at a fixed 1280x720 and
 * nothing else reproduces that faithfully.
 */
async function renderPdf(html: string, htmlPath: string, pdfPath: string) {
  fs.writeFileSync(htmlPath, html);
  const browser = await puppeteer.launch({ headless: true });
  try {
    const page = await browser.newPage();
    // file:// so relative paths to assets/ and screenshots resolve.
    await page.goto(`file://${htmlPath}`, { waitUntil: "networkidle0" });
    await page.pdf({
      path: pdfPath,
      width: "1280px",
      height: "720px",
      printBackground: true,
      pageRanges: "1-5",
    });
  } finally {
    await browser.close();
  }
}

/**
 * check.ts is pure logic on the yaml. Deliverability is a fact about the
 * contact database, so it is asked here, where the DB is reachable.
 */
export function auditCompany(c: Company): CheckIssue[] {
  const issues = checkCompany(c);
  if (c.recipient?.email && /@/.test(c.recipient.email) && !isVerified(c.recipient.email)) {
    issues.push({
      level: "warn",
      field: "recipient.email",
      message: "not confirmed by email_verify.py. Verify before sending, a bounce here costs the domain.",
    });
  }
  return issues;
}

/** Strip the To/Subject preamble so send_emails.py gets a body, not a file. */
function bodyOf(email: string): string {
  const lines = email.split("\n");
  const i = lines.findIndex((l) => l.startsWith("Subject:"));
  return (i === -1 ? email : lines.slice(i + 1).join("\n")).replace(/^\n+/, "").trimEnd();
}

export async function buildCompany(slug: string): Promise<{ ok: boolean; issues: CheckIssue[] }> {
  const c = loadCompany(slug);
  const sender = loadSender();
  const issues = auditCompany(c);
  if (hasErrors(issues)) return { ok: false, issues };

  const dir = outDir(slug);
  fs.mkdirSync(dir, { recursive: true });

  const subject = chosenSubject(c);

  const emailTpl = fs.readFileSync(path.join(TEMPLATES, "email.ejs"), "utf8");
  const email = ejs.render(emailTpl, { c, sender, subject }, { rmWhitespace: false });
  fs.writeFileSync(path.join(dir, "email.txt"), email.replace(/\n{3,}/g, "\n\n"));

  const deckTpl = fs.readFileSync(path.join(TEMPLATES, "deck.ejs"), "utf8");
  const deckHtml = ejs.render(deckTpl, { c, sender, subject });
  const deckName = `${sender.name.replace(/\s+/g, "-")}-x-${c.name.replace(/[^\w]+/g, "-")}`;
  const deckPath = path.join(dir, `${deckName}.pdf`);
  await renderPdf(deckHtml, path.join(dir, "deck.html"), deckPath);

  // The handoff to send_emails.py. It merges these into PERSONALIZED_EMAILS at
  // startup, so tier one rides the existing sender, cap, tracker and bounce
  // handling instead of a second delivery path.
  fs.writeFileSync(
    path.join(dir, "send.json"),
    JSON.stringify(
      {
        slug,
        company: c.name,
        recipient_email: c.recipient.email,
        recipient_name: c.recipient.name,
        subject,
        body: bodyOf(email),
        deck: deckPath,
        generated_at: new Date().toISOString(),
      },
      null,
      2
    )
  );

  // Subject lines are the highest-leverage line in the email, so keep the
  // rejected candidates next to the sent one for later comparison.
  fs.writeFileSync(
    path.join(dir, "subjects.txt"),
    c.subject_candidates.map((s, i) => `${i === (c.subject_chosen ?? 0) ? "->" : "  "} [${i}] ${s}`).join("\n") + "\n"
  );

  return { ok: true, issues };
}
