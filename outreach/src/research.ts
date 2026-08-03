import fs from "node:fs";
import { researchFile, RESEARCH } from "./paths.js";

/**
 * Machine-gathered facts only.
 *
 * A company homepage is marketing copy and tells you nothing you can pitch on.
 * What carries signal is the GitHub org: what they actually write, what is
 * active, and what is open. This fetches that and stops. Reading their
 * engineering blog, docs, and job posting is still your job, and it is where
 * the angle comes from.
 */

interface Repo {
  name: string;
  description: string | null;
  language: string | null;
  stargazers_count: number;
  open_issues_count: number;
  pushed_at: string;
  html_url: string;
  fork: boolean;
  archived: boolean;
}

interface Digest {
  gathered_at: string;
  org: string;
  profile: { name?: string; description?: string; blog?: string; public_repos?: number; type: string } | null;
  languages: { language: string; repos: number }[];
  active_repos: { name: string; language: string | null; stars: number; open_issues: number; pushed: string; url: string; description: string | null }[];
  good_first_issues: { title: string; repo: string; url: string; updated: string }[];
  notes: string[];
}

const GH = "https://api.github.com";

function headers(): Record<string, string> {
  const h: Record<string, string> = {
    Accept: "application/vnd.github+json",
    "User-Agent": "outreach-research",
  };
  const token = process.env.GITHUB_TOKEN;
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

async function gh<T>(path: string): Promise<T | null> {
  const res = await fetch(`${GH}${path}`, { headers: headers() });
  if (res.status === 404) return null;
  if (res.status === 403 || res.status === 429) {
    throw new Error("GitHub rate limit. Set GITHUB_TOKEN in your env and retry.");
  }
  if (!res.ok) throw new Error(`GitHub ${res.status} on ${path}`);
  return (await res.json()) as T;
}

export async function research(slug: string, org: string): Promise<Digest> {
  const notes: string[] = [];

  let profile = await gh<any>(`/orgs/${org}`);
  let repoPath = `/orgs/${org}/repos`;
  if (!profile) {
    profile = await gh<any>(`/users/${org}`);
    repoPath = `/users/${org}/repos`;
    if (profile) notes.push(`"${org}" is a user account, not an org.`);
  }
  if (!profile) notes.push(`No GitHub account found for "${org}". Check the handle, or they may not publish code.`);

  const repos = (await gh<Repo[]>(`${repoPath}?per_page=100&sort=pushed`)) ?? [];
  const own = repos.filter((r) => !r.fork && !r.archived);

  const langCount = new Map<string, number>();
  for (const r of own) {
    if (r.language) langCount.set(r.language, (langCount.get(r.language) ?? 0) + 1);
  }

  const active = [...own]
    .sort((a, b) => Date.parse(b.pushed_at) - Date.parse(a.pushed_at))
    .slice(0, 12)
    .map((r) => ({
      name: r.name,
      language: r.language,
      stars: r.stargazers_count,
      open_issues: r.open_issues_count,
      pushed: r.pushed_at.slice(0, 10),
      url: r.html_url,
      description: r.description,
    }));

  let gfi: Digest["good_first_issues"] = [];
  if (profile) {
    const q = encodeURIComponent(`org:${org} is:issue is:open label:"good first issue"`);
    const search = await gh<{ items: any[] }>(`/search/issues?q=${q}&per_page=10&sort=updated`);
    gfi = (search?.items ?? []).map((i) => ({
      title: i.title,
      repo: String(i.repository_url ?? "").split("/").slice(-1)[0] ?? "",
      url: i.html_url,
      updated: String(i.updated_at ?? "").slice(0, 10),
    }));
  }

  if (own.length === 0 && profile) {
    notes.push("Org exists but publishes no non-fork repos. Your finding will have to come from the product itself, their docs, or their public API.");
  }
  if (gfi.length > 0) {
    notes.push(`${gfi.length} open "good first issue" items. A merged PR is the strongest artifact you can attach.`);
  }

  const digest: Digest = {
    gathered_at: new Date().toISOString(),
    org,
    profile: profile
      ? {
          name: profile.name,
          description: profile.description,
          blog: profile.blog,
          public_repos: profile.public_repos,
          type: profile.type,
        }
      : null,
    languages: [...langCount.entries()]
      .map(([language, repos]) => ({ language, repos }))
      .sort((a, b) => b.repos - a.repos),
    active_repos: active,
    good_first_issues: gfi,
    notes,
  };

  fs.mkdirSync(RESEARCH, { recursive: true });
  fs.writeFileSync(researchFile(slug), JSON.stringify(digest, null, 2));
  return digest;
}

export function printDigest(d: Digest) {
  console.log(`\n  ${d.profile?.name ?? d.org}`);
  if (d.profile?.description) console.log(`  ${d.profile.description}`);
  if (d.profile?.blog) console.log(`  ${d.profile.blog}`);

  if (d.languages.length) {
    console.log(`\n  Languages they actually write`);
    for (const l of d.languages.slice(0, 6)) console.log(`    ${l.language.padEnd(14)} ${l.repos} repos`);
  }

  if (d.active_repos.length) {
    console.log(`\n  Most recently pushed`);
    for (const r of d.active_repos.slice(0, 8)) {
      const meta = `${r.language ?? "?"}, ${r.stars}*, ${r.open_issues} open`;
      console.log(`    ${r.pushed}  ${r.name.padEnd(24)} ${meta}`);
      if (r.description) console.log(`                ${r.description.slice(0, 88)}`);
    }
  }

  if (d.good_first_issues.length) {
    console.log(`\n  Open "good first issue"`);
    for (const i of d.good_first_issues) console.log(`    ${i.repo}: ${i.title}\n      ${i.url}`);
  }

  for (const n of d.notes) console.log(`\n  Note: ${n}`);
  console.log(`\n  Saved to research/${d.org}.json`);
  console.log(`\n  Still yours to do: read their engineering blog, their docs, and the job posting.`);
  console.log(`  The stack is listed in the posting. The angle is not in any of this JSON.\n`);
}
