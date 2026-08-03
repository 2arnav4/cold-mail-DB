import fs from "node:fs";
import path from "node:path";
import { research, printDigest } from "./research.js";
import { checkCompany, hasErrors } from "./check.js";
import { auditCompany, buildCompany, loadCompany, allSlugs } from "./build.js";
import { readLog, upsert } from "./log.js";
import { lookup } from "./db.js";
import { COMPANIES, companyFile, outDir, LOG } from "./paths.js";

const [, , cmd, ...args] = process.argv;

const die = (msg: string): never => {
  console.error(`\n  ${msg}\n`);
  process.exit(1);
};

function printIssues(slug: string, issues: ReturnType<typeof checkCompany>) {
  const errors = issues.filter((i) => i.level === "error");
  const warns = issues.filter((i) => i.level === "warn");
  if (!errors.length && !warns.length) {
    console.log(`  ${slug.padEnd(20)} clean`);
    return;
  }
  console.log(`  ${slug}`);
  for (const e of errors) console.log(`    ERROR  ${e.field}: ${e.message}`);
  for (const w of warns) console.log(`    warn   ${w.field}: ${w.message}`);
}

async function main() {
  switch (cmd) {
    case "new": {
      const [slug, ...rest] = args;
      if (!slug) die('usage: npm run new -- <slug> "Company Name"  |  npm run new -- <slug> --from-db <domain|email>');
      const dest = companyFile(slug!);
      if (fs.existsSync(dest)) die(`companies/${slug}.yaml already exists`);

      let stub = fs.readFileSync(path.join(COMPANIES, "_template.yaml"), "utf8");
      const dbIdx = rest.indexOf("--from-db");

      if (dbIdx !== -1) {
        // Pull what the pipeline already knows rather than retyping it.
        const needle = rest[dbIdx + 1];
        if (!needle) die("usage: npm run new -- <slug> --from-db <domain|email>");
        const hit = lookup(needle!);
        if (!hit) die(`No company matching "${needle}" in turso-full.db. Scrape it first, or pass a name instead.`);
        const { company, contacts } = hit!;
        const best = contacts[0];

        stub = stub.replace(/^name:.*$/m, `name: ${company.name}`);
        if (company.description) {
          stub = stub.replace(/^what_they_build:.*$/m, `what_they_build: ${JSON.stringify(company.description)}`);
        }
        if (best) {
          stub = stub
            .replace(/^  name: TODO$/m, `  name: ${best.name ?? "TODO"}`)
            .replace(/^  role: TODO$/m, `  role: ${best.role ?? "TODO"}`)
            .replace(/^  email: TODO$/m, `  email: ${best.email}`)
            .replace(/^  source: TODO$/m, `  source: turso-full.db contacts${best.source_url ? `, ${best.source_url}` : ""}`);
        }
        fs.writeFileSync(dest, stub);

        console.log(`\n  Created companies/${slug}.yaml from the contact database`);
        console.log(`  ${company.name} (${company.domain})${company.sector ? `, sector: ${company.sector}` : ""}`);
        if (contacts.length) {
          console.log(`\n  Candidates, best first. Swap recipient in the yaml if the top one is wrong:`);
          for (const c of contacts) {
            const flag = c.verified ? "verified" : "UNVERIFIED";
            console.log(`    ${flag.padEnd(11)} ${(c.email ?? "").padEnd(38)} ${c.name ?? "?"}, ${c.role ?? "role unknown"}`);
          }
        } else {
          console.log(`\n  No contacts on file for this company yet. Find one before you write anything.`);
        }
      } else {
        const name = rest.join(" ") || slug!;
        stub = stub.replace(/^name:.*$/m, `name: ${name}`);
        fs.writeFileSync(dest, stub);
        console.log(`\n  Created companies/${slug}.yaml`);
      }

      console.log(`\n  Next: npm run research -- ${slug} <their-github-org>\n`);
      break;
    }

    case "research": {
      const [slug, org] = args;
      if (!slug || !org) die("usage: npm run research -- <slug> <their-github-org>");
      printDigest(await research(slug!, org!));
      break;
    }

    case "check": {
      const slugs = args.length ? args : allSlugs();
      if (!slugs.length) die("no companies yet, start with: npm run new -- <slug> \"Company Name\"");
      let bad = 0;
      console.log("");
      for (const s of slugs) {
        const issues = auditCompany(loadCompany(s));
        printIssues(s, issues);
        if (hasErrors(issues)) bad++;
      }
      console.log(`\n  ${slugs.length - bad}/${slugs.length} ready to build\n`);
      if (bad) process.exit(1);
      break;
    }

    case "build": {
      const slugs = args.length ? args : allSlugs();
      if (!slugs.length) die("no companies yet");
      console.log("");
      let built = 0;
      for (const s of slugs) {
        const { ok, issues } = await buildCompany(s);
        if (!ok) {
          printIssues(s, issues);
          continue;
        }
        for (const w of issues) console.log(`    warn   ${s}: ${w.field}: ${w.message}`);
        console.log(`  built  out/${s}/`);
        built++;
      }
      console.log(`\n  ${built}/${slugs.length} built\n`);
      break;
    }

    case "log": {
      const [slug] = args;
      if (!slug) die("usage: npm run log -- <slug> --sent|--opened|--replied|--outcome \"...\"");
      const flags = args.slice(1);
      const patch: Record<string, string> = {};
      const today = new Date().toISOString().slice(0, 10);
      if (flags.includes("--sent")) {
        const c = loadCompany(slug!);
        Object.assign(patch, {
          company: c.name,
          recipient: c.recipient.name,
          role: c.recipient.role,
          subject: c.subject_candidates[c.subject_chosen ?? 0] ?? "",
          sent: today,
        });
      }
      if (flags.includes("--opened")) patch.opened = today;
      if (flags.includes("--replied")) patch.replied = today;
      const oi = flags.indexOf("--outcome");
      if (oi !== -1 && flags[oi + 1]) patch.outcome = flags[oi + 1]!;
      if (!Object.keys(patch).length) die("nothing to record, pass --sent, --opened, --replied or --outcome");
      upsert(slug!, patch);
      console.log(`\n  logged ${slug}: ${Object.entries(patch).map(([k, v]) => `${k}=${v}`).join(" ")}\n`);
      break;
    }

    case "status": {
      const rows = readLog();
      if (!rows.length) die(`nothing logged yet (${LOG})`);
      const sent = rows.filter((r) => r.sent).length;
      const opened = rows.filter((r) => r.opened).length;
      const replied = rows.filter((r) => r.replied).length;
      console.log("");
      for (const r of rows) {
        const marks = [r.sent ? "sent" : "", r.opened ? "opened" : "", r.replied ? "REPLIED" : ""].filter(Boolean).join(" / ");
        console.log(`  ${r.slug.padEnd(18)} ${marks.padEnd(26)} ${r.subject}`);
      }
      console.log(`\n  ${sent} sent, ${opened} opened, ${replied} replied`);
      console.log(`  Opens diagnose the subject line. Replies are the metric.\n`);
      break;
    }

    default:
      console.log(`
  outreach

    npm run new      -- <slug> "Company Name"      scaffold companies/<slug>.yaml
    npm run research -- <slug> <github-org>        pull what they actually write
    npm run check    -- [slug...]                  refuse generic output
    npm run build    -- [slug...]                  render email.txt + deck.pdf
    npm run log      -- <slug> --sent|--opened|--replied|--outcome "..."
    npm run status                                 the funnel so far
`);
  }
}

main().catch((e) => die(e instanceof Error ? e.message : String(e)));
