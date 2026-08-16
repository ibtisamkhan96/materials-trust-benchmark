const $ = (id) => document.getElementById(id);

const STRATUM = {
  "A correlated oxide or fluoride": "correlated oxide / fluoride",
  "B other chalcogenide or pnictide": "chalcogenide / pnictide",
  "C halide": "halide",
  "D remainder": "remainder",
};

function fmt(n, digits = 3) {
    if (n === null || n === undefined || Number.isNaN(n)) return "-";
  return Number(n).toLocaleString("en-GB", {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  });
}

function bandLabel(band) {
  if (!band) return "no pair";
  return band.replaceAll("_", " ");
}

async function load() {
  const [dash, materials] = await Promise.all([
    fetch("data/dashboard.json").then((r) => r.json()),
    fetch("data/materials.json").then((r) => r.json()),
  ]);
  $("n-comp").textContent = fmt(dash.n_compositions, 0);
  $("fe-mae").textContent = fmt(dash.fe_mae, 3);
  $("gap-err").textContent = fmt(dash.expt_signed, 2);
  renderExplorer(materials);
}

function renderExplorer(materials) {
  const list = $("list");
  const q = $("q");
  let hubbard = "all";
  let selected = null;

  function filtered() {
    const needle = q.value.trim().toLowerCase();
    return materials.filter((m) => {
      if (hubbard === "differ" && !m.hubbard_differs) return false;
      if (hubbard === "agree" && m.hubbard_differs) return false;
      if (!needle) return true;
      return (
        m.formula.toLowerCase().includes(needle) ||
        (m.mpid || "").toLowerCase().includes(needle)
      );
    });
  }

  function paintList() {
    const rows = filtered();
    list.replaceChildren();
    rows.slice(0, 80).forEach((m) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.innerHTML = `${m.formula}<span class="sub">${m.n_structures} structures · ${bandLabel(m.fe && m.fe.band)}</span>`;
      btn.setAttribute("aria-current", selected && selected.formula === m.formula ? "true" : "false");
      btn.addEventListener("click", () => {
        selected = m;
        paintList();
        paintDetail(m);
      });
      li.appendChild(btn);
      list.appendChild(li);
    });
    if (rows.length > 80) {
      const li = document.createElement("li");
      li.innerHTML = `<p class="sub" style="padding:0.6rem 0.8rem">${rows.length - 80} more. Narrow the search.</p>`;
      list.appendChild(li);
    }
    if (!rows.length) {
      const li = document.createElement("li");
      li.innerHTML = `<p class="sub" style="padding:0.6rem 0.8rem">No assayed composition matches that.</p>`;
      list.appendChild(li);
    }
  }

  q.addEventListener("input", paintList);
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      hubbard = chip.dataset.hubbard;
      document.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === chip));
      paintList();
    });
  });
  paintList();
}

function paintDetail(m) {
  const detail = $("detail");
  detail.hidden = false;
  $("d-formula").textContent = m.formula;
  const bits = [
    m.mpid,
    STRATUM[m.stratum] || m.stratum,
    m.in_mp ? "in Materials Project" : "not in Materials Project",
    m.in_oqmd ? "in OQMD" : "not in OQMD",
    `${m.n_structures} distinct structures`,
  ].filter(Boolean);
  $("d-meta").textContent = bits.join(" · ");
  const stamp = $("d-stamp");
  const band = (m.fe && m.fe.band) || "not_assessable";
  stamp.className = `mini-stamp ${band}`;
  stamp.textContent = bandLabel(band);

  const kv = $("d-values");
  kv.replaceChildren();
  const rows = [
    ["measured gap", m.metal ? "measured metallic" : (m.expt_gap == null ? "—" : `${fmt(m.expt_gap, 2)} eV`)],
    ["formation energy MP", m.fe && m.fe.mp != null ? `${fmt(m.fe.mp, 3)} eV/atom` : "no pair"],
    ["formation energy OQMD", m.fe && m.fe.oqmd != null ? `${fmt(m.fe.oqmd, 3)} eV/atom` : "no pair"],
    ["FE spread", m.fe && m.fe.spread != null ? `${fmt(m.fe.spread, 3)} eV/atom` : "—"],
    ["matched structure", (m.fe && m.fe.structure) || "not structure-matched across sources"],
    ["band gap MP", m.gap && m.gap.mp != null ? `${fmt(m.gap.mp, 3)} eV` : "no pair"],
    ["band gap OQMD", m.gap && m.gap.oqmd != null ? `${fmt(m.gap.oqmd, 3)} eV` : "no pair"],
  ];
  for (const [k, v] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = v;
    kv.append(dt, dd);
  }
  $("d-hubbard").textContent = m.hubbard || "";
  $("d-cli").textContent = `mtb audit ${m.formula}`;
  const liveBtn = $("d-live");
  if (liveBtn) {
    liveBtn.onclick = () => {
      $("live-formula").value = m.formula;
      $("live-q").value = `Why do Materials Project and OQMD disagree on ${m.formula} formation energy?`;
      document.getElementById("live").scrollIntoView({ behavior: "smooth" });
    };
  }
}

const KEY_STORE = "mtb-live-keys";

function keys() {
  return {
    githubToken: $("k-github").value.trim(),
    anthropicKey: $("k-anthropic").value.trim(),
    mpApiKey: $("k-mp").value.trim(),
  };
}

function restoreKeys() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY_STORE) || "null");
    if (!saved) return;
    $("k-github").value = saved.githubToken || "";
    $("k-anthropic").value = saved.anthropicKey || "";
    $("k-mp").value = saved.mpApiKey || "";
    $("key-status").textContent = "Keys restored for this tab only.";
  } catch {
    sessionStorage.removeItem(KEY_STORE);
  }
}

function showLive(text, isError) {
  const out = $("live-out");
  out.hidden = false;
  out.className = isError ? "live-out error" : "live-out";
  out.textContent = text;
}

function formatAudit(data) {
  const lines = [];
  if (data.github_user) lines.push(`GitHub: ${data.github_user}`);
  lines.push(data.caveat || "");
  if (data.hubbard && data.hubbard.explanation) {
    lines.push("", data.hubbard.explanation);
  }
  const mpN = data.materials_project && data.materials_project.n;
  const oqN = data.oqmd && data.oqmd.n;
  lines.push("", `Materials Project entries: ${mpN ?? 0}`);
  lines.push(`OQMD entries: ${oqN ?? 0}`);
  if (data.materials_project && data.materials_project.error) {
    lines.push(`Materials Project: ${data.materials_project.error}`);
  }
  if (data.oqmd && data.oqmd.error) {
    lines.push(`OQMD: ${data.oqmd.error}`);
  }
  for (const g of data.groups || []) {
    const spread =
      g.spread_ev_per_atom == null ? "no pair" : `${g.spread_ev_per_atom} eV/atom`;
    lines.push(
      "",
      `${g.spacegroup}: MP ${g.formation_energy_mp ?? "-"} eV/atom, OQMD ${g.formation_energy_oqmd ?? "-"} eV/atom, spread ${spread}`
    );
    if (g.large_disagreement) lines.push("  larger than 50 meV/atom");
    if (g.single_source) lines.push("  only one source in this space group");
  }
  return lines.join("\n").trim();
}

async function postLive(path, payload) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const text = await resp.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error(
      resp.status === 404
        ? "Live functions are not on this host. Deploy the repo to Netlify with publish directory site."
        : text.slice(0, 300) || `HTTP ${resp.status}`
    );
  }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function bindLive() {
  restoreKeys();
  $("keys-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    sessionStorage.setItem(KEY_STORE, JSON.stringify(keys()));
    $("key-status").textContent = "Keys kept in this tab. They are not written to disk.";
  });
  $("clear-keys").addEventListener("click", () => {
    sessionStorage.removeItem(KEY_STORE);
    $("k-github").value = "";
    $("k-anthropic").value = "";
    $("k-mp").value = "";
    $("key-status").textContent = "Keys cleared.";
  });
  $("run-audit").addEventListener("click", async () => {
    const k = keys();
    const formula = $("live-formula").value.trim();
    if (!formula) return showLive("Enter a composition.", true);
    if (!k.githubToken || !k.mpApiKey) {
      return showLive("GitHub token and Materials Project key are required to audit.", true);
    }
    $("run-audit").disabled = true;
    showLive("Fetching Materials Project and OQMD…");
    try {
      const data = await postLive("/api/audit", {
        formula,
        githubToken: k.githubToken,
        mpApiKey: k.mpApiKey,
      });
      $("key-status").textContent = data.github_user
        ? `Signed in as GitHub user ${data.github_user}.`
        : $("key-status").textContent;
      showLive(formatAudit(data));
    } catch (err) {
      showLive(err.message || String(err), true);
    } finally {
      $("run-audit").disabled = false;
    }
  });
  $("run-explain").addEventListener("click", async () => {
    const k = keys();
    const question = $("live-q").value.trim();
    if (!question) return showLive("Ask a short question.", true);
    if (!k.githubToken || !k.anthropicKey || !k.mpApiKey) {
      return showLive("GitHub, Anthropic, and Materials Project keys are all required to explain.", true);
    }
    $("run-explain").disabled = true;
    showLive("Calling the explainer. This can take a few seconds…");
    try {
      const data = await postLive("/api/explain", {
        question,
        githubToken: k.githubToken,
        anthropicKey: k.anthropicKey,
        mpApiKey: k.mpApiKey,
      });
      const guard = data.guard && data.guard.passed ? "numeric guard passed" : "numeric guard failed";
      showLive(`${data.answer || ""}\n\n${guard}`);
    } catch (err) {
      showLive(err.message || String(err), true);
    } finally {
      $("run-explain").disabled = false;
    }
  });
}

bindLive();

load().catch((err) => {
  $("list").innerHTML = `<li><p class="sub" style="padding:0.8rem">Could not load assay data. ${err}</p></li>`;
});
