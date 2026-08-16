export function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

export function redact(text: string) {
  return text
    .replace(/sk-ant-[a-zA-Z0-9_-]+/g, "[redacted]")
    .replace(/ghp_[a-zA-Z0-9]+/g, "[redacted]")
    .replace(/github_pat_[a-zA-Z0-9_]+/g, "[redacted]")
    .replace(/Bearer\s+\S+/gi, "Bearer [redacted]");
}

export async function requireGitHub(token: string | undefined) {
  if (!token || token.length < 8) {
    return json(
      { error: "A GitHub token is required so live calls are tied to a person, not run anonymously." },
      401
    );
  }
  const resp = await fetch("https://api.github.com/user", {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "materials-trust-benchmark",
    },
  });
  if (!resp.ok) {
    return json(
      {
        error:
          "GitHub token was rejected. Create a classic token with no extra scopes, or a fine-grained token that can read your profile.",
      },
      401
    );
  }
  const user = (await resp.json()) as { login?: string };
  return { login: user.login || "github-user" };
}
