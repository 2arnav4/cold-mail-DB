# Template rules

Every template in this directory, and every new one, follows this shape. The
live generator (`daily_drafts.py`: `PROJECTS`, `INTRO`, `RESOURCES`,
`compose`, `validate`) enforces the same thing, so change both together.

```
Subject: ...

Hi {{first_name}},

<company paragraph: exactly two sentences>

I'm a third-year CSE student in Delhi, graduating 2028. The three closest things I've built:

• <Project>, <metric>: <what I did>, so <what it buys>. <live link>
• ...
• ...

Would like to be considered for an internship at {{company}}.

Resources:
• Portfolio, all projects and code: https://arnav24.tech
• Resume: https://www.arnav24.tech/resume.pdf

Resume attached.

Arnav
```

Rules:

1. **Company paragraph: two sentences, hard cap.** It names the engineering
   tension under what they build. It never describes their product back at
   them and never flatters.
2. **One line per project, metric first.** Format is
   `name, number: action taken, so benefit`. A founder should get every number
   in one scan and open a project only if a number interests him.
3. **Never a bare feature count.** "Rate limiting across 14 endpoints" says
   nothing on its own. Say what the endpoints cover and what the work buys:
   "14 REST endpoints over one Postgres schema, JWT and per-route rate limits,
   so a retried write never duplicates a task."
4. **Say the mechanism when it is the point.** If the thing polls, say it
   polls and say what polling bought: "one shared poll across three market
   APIs instead of one per widget, which stays inside free-tier limits."
5. **Numbers in every bullet.** 3,000+ tasks, 14 endpoints, 200+ assets,
   three APIs, 150+ students. Never invent one.
6. **Link at the end of each bullet, no trailing punctuation** after the URL,
   or the punctuation gets swallowed into the link.
7. **Three project links**, ordered most relevant to that company's sector
   first. Only the first bullet is read closely.
8. **No GitHub link and no LinkedIn link.** The portfolio already indexes
   every project and its source; a second code link only splits the click.
   Resume link always present, alongside the attachment.
9. **No em-dashes, no phone number.** Links only as bullets under
   `Resources:`.
10. **Never name a role or track.** Not "backend internship", not "frontend".
    The ask is "an internship at {{company}}": the email should not presume
    which role is open.
11. **Keep it short.** No paragraph longer than two sentences anywhere; the
    body should fit one phone screen.
