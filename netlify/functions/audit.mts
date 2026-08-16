import type { Config } from "@netlify/functions";
import { runAudit } from "./_shared/audit";
import { json, requireGitHub, redact } from "./_shared/http";

export const config: Config = {
  path: "/api/audit",
  method: "POST",
};

export default async (req: Request) => {
  if (req.method !== "POST") {
    return json({ error: "POST a formula and keys." }, 405);
  }

  let body: {
    formula?: string;
    githubToken?: string;
    mpApiKey?: string;
  };
  try {
    body = await req.json();
  } catch {
    return json({ error: "Send JSON with formula, githubToken, and mpApiKey." }, 400);
  }

  const gh = await requireGitHub(body.githubToken);
  if (gh instanceof Response) return gh;

  const formula = (body.formula || "").trim();
  if (!/^[A-Za-z0-9().]+$/.test(formula) || formula.length > 40) {
    return json({ error: "That does not look like a composition formula." }, 400);
  }
  if (!body.mpApiKey) {
    return json(
      { error: "A Materials Project API key is required for live numbers. GitHub has no materials data." },
      400
    );
  }

  try {
    const result = await runAudit(formula, body.mpApiKey);
    return json({ ...result, github_user: gh.login });
  } catch (err) {
    return json({ error: redact(err instanceof Error ? err.message : "audit failed") }, 502);
  }
};
