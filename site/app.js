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
}

load().catch((err) => {
  $("list").innerHTML = `<li><p class="sub" style="padding:0.8rem">Could not load assay data. ${err}</p></li>`;
});
