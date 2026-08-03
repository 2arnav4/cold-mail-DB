import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));

export const ROOT = path.resolve(here, "..");
export const COMPANIES = path.join(ROOT, "companies");
export const RESEARCH = path.join(ROOT, "research");
export const TEMPLATES = path.join(ROOT, "templates");
export const ASSETS = path.join(ROOT, "assets");
export const OUT = path.join(ROOT, "out");
export const LOG = path.join(ROOT, "log.csv");
export const SENDER = path.join(ROOT, "sender.yaml");

export const companyFile = (slug: string) => path.join(COMPANIES, `${slug}.yaml`);
export const researchFile = (slug: string) => path.join(RESEARCH, `${slug}.json`);
export const outDir = (slug: string) => path.join(OUT, slug);
