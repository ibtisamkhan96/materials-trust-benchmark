import Anthropic from "@anthropic-ai/sdk";
import type { Config } from "@netlify/functions";
import { runAudit } from "./_shared/audit";
import { json, redact, requireGitHub } from "./_shared/http";

export const config: Config = {
  path: "/api/explain",
  method: "POST",
};

const SYSTEM = `You explain materials data quality from tool output only.
Never compute, estimate, convert units, or invent a number.
If a value is missing from the tool result, say so and stop.
Do not quote conversion factors or remembered melting points.
Identify structures by space group. This live path is space-group grouped, not StructureMatcher.
End with "Values referenced" listing each number and its source.`;

const NUMBER = /(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?/g;

export default async (req: Request) => {
  if (req.method !== "POST") {
    return json({ error: "POST a question and keys." }, 405);
  }

  let body: {
    question?: string;
    githubToken?: string;
    anthropicKey?: string;
    mpApiKey?: string;
  };
  try {
    body = await req.json();
  } catch {
    return json({ error: "Send JSON with question and keys." }, 400);
  }

  const gh = await requireGitHub(body.githubToken);
  if (gh instanceof Response) return gh;
  if (!body.anthropicKey) {
    return json({ error: "An Anthropic API key is required for explanations." }, 400);
  }
  if (!body.mpApiKey) {
    return json(
      { error: "A Materials Project API key is required for live numbers. GitHub has no materials data." },
      400
    );
  }
  const question = (body.question || "").trim();
  if (!question || question.length > 500) {
    return json({ error: "Ask a short question about a composition or a disagreement." }, 400);
  }

  try {
    const client = new Anthropic({ apiKey: body.anthropicKey });
    const tools: Anthropic.Tool[] = [
      {
        name: "audit_material",
        description:
          "Fetch live Materials Project and OQMD values for a formula, grouped by space group, plus documented Hubbard U policy.",
        input_schema: {
          type: "object",
          properties: { formula: { type: "string" } },
          required: ["formula"],
        },
      },
    ];

    let messages: Anthropic.MessageParam[] = [{ role: "user", content: question }];
    const toolOutputs: string[] = [];

    for (let step = 0; step < 3; step++) {
      const msg = await client.messages.create({
        model: "claude-sonnet-4-5-20250929",
        max_tokens: 1200,
        system: SYSTEM,
        tools,
        messages,
      });

      const toolUses = msg.content.filter(
        (b): b is Anthropic.ToolUseBlock => b.type === "tool_use"
      );
      if (!toolUses.length) {
        const answer = msg.content
          .filter((b): b is Anthropic.TextBlock => b.type === "text")
          .map((b) => b.text)
          .join("\n");
        const guard = verify(answer, toolOutputs, question);
        return json({
          github_user: gh.login,
          answer: guard.passed
            ? answer
            : `${answer}\n\nNumeric guard: FAILED. Unsupported: ${guard.unverified.join(", ")}`,
          guard,
        });
      }

      const toolResults: Anthropic.ToolResultBlockParam[] = [];
      for (const call of toolUses) {
        const formula = String((call.input as { formula?: string }).formula || "");
        if (!/^[A-Za-z0-9().]+$/.test(formula) || formula.length > 40) {
          const fail = JSON.stringify({ error: "That formula is not allowed." });
          toolOutputs.push(fail);
          toolResults.push({ type: "tool_result", tool_use_id: call.id, content: fail });
          continue;
        }
        const payload = await runAudit(formula, body.mpApiKey);
        const text = JSON.stringify(payload).slice(0, 60000);
        toolOutputs.push(text);
        toolResults.push({
          type: "tool_result",
          tool_use_id: call.id,
          content: text,
        });
      }
      messages = [
        ...messages,
        { role: "assistant", content: msg.content },
        { role: "user", content: toolResults },
      ];
    }

    return json({ error: "The agent used too many tool steps." }, 504);
  } catch (err) {
    return json({ error: redact(err instanceof Error ? err.message : "explain failed") }, 502);
  }
};

function verify(answer: string, tools: string[], question: string) {
  const permitted = new Set(
    [...`${tools.join("\n")}\n${question}`.matchAll(NUMBER)].map((m) => m[0])
  );
  const claims = [...answer.replace(/\u2212/g, "-").matchAll(NUMBER)].map((m) => m[0]);
  const unverified = [
    ...new Set(claims.filter((c) => !permitted.has(c) && !roundingOk(c, permitted))),
  ];
  return {
    passed: unverified.length === 0,
    n_numeric_claims: claims.length,
    unverified,
  };
}

function roundingOk(claim: string, permitted: Set<string>) {
  const n = Number(claim);
  if (!Number.isFinite(n)) return false;
  for (const p of permitted) {
    const s = Number(p);
    if (!Number.isFinite(s)) continue;
    for (let d = 1; d <= 6; d++) {
      if (Number(s.toFixed(d)) === n) return true;
    }
  }
  return false;
}
