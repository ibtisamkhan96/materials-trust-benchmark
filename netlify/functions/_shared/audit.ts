import { compareHubbard } from "./hubbard";

const OQMD_FIELDS =
  "name,entry_id,spacegroup,band_gap,delta_e,icsd_id,natoms,stability";

export type Entry = {
  source: string;
  id: string;
  spacegroup: string;
  formation_energy: number | null;
  band_gap: number | null;
  experimental: boolean | null;
};

export async function runAudit(formula: string, mpApiKey: string) {
  const hubbard = compareHubbard(formula);
  const [mpResult, oqmdResult] = await Promise.allSettled([
    fetchMp(formula, mpApiKey),
    fetchOqmd(formula),
  ]);

  const mp =
    mpResult.status === "fulfilled"
      ? mpResult.value
      : { entries: [] as Entry[], error: String(mpResult.reason) };
  const oqmd =
    oqmdResult.status === "fulfilled"
      ? oqmdResult.value
      : { entries: [] as Entry[], error: String(oqmdResult.reason) };

  return {
    formula,
    caveat:
      "This live path groups entries by space group, not pymatgen StructureMatcher. Distinct crystals can share a space group. The CLI audit remains the authoritative comparison.",
    hubbard,
    materials_project: mp,
    oqmd,
    groups: groupBySpacegroup([...(mp.entries || []), ...(oqmd.entries || [])]),
  };
}

async function fetchOqmd(formula: string) {
  const url = new URL("https://oqmd.org/oqmdapi/formationenergy");
  url.searchParams.set("composition", formula);
  url.searchParams.set("limit", "25");
  url.searchParams.set("fields", OQMD_FIELDS);
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error(`OQMD HTTP ${resp.status}`);
  const payload = (await resp.json()) as { data?: Record<string, unknown>[] };
  const entries: Entry[] = (payload.data || []).slice(0, 20).map((row) => ({
    source: "oqmd",
    id: String(row.entry_id ?? row.formationenergy_id ?? ""),
    spacegroup: String(row.spacegroup || "unknown"),
    formation_energy: num(row.delta_e),
    band_gap: num(row.band_gap),
    experimental: row.icsd_id != null && row.icsd_id !== "",
  }));
  return { entries, n: entries.length };
}

async function fetchMp(formula: string, apiKey: string) {
  const headers = { "X-API-KEY": apiKey, Accept: "application/json" };
  const summaryUrl = new URL("https://api.materialsproject.org/materials/summary/");
  summaryUrl.searchParams.set("formula", formula);
  summaryUrl.searchParams.set("deprecated", "false");
  summaryUrl.searchParams.set("_per_page", "20");
  summaryUrl.searchParams.set(
    "_fields",
    "material_id,formula_pretty,band_gap,symmetry,theoretical,nsites"
  );
  const thermoUrl = new URL("https://api.materialsproject.org/materials/thermo/");
  thermoUrl.searchParams.set("formula", formula);
  thermoUrl.searchParams.set("thermo_types", "GGA_GGA+U");
  thermoUrl.searchParams.set("_per_page", "20");
  thermoUrl.searchParams.set("_fields", "material_id,thermo_type,formation_energy_per_atom");

  const [sumResp, thResp] = await Promise.all([
    fetch(summaryUrl, { headers }),
    fetch(thermoUrl, { headers }),
  ]);
  if (sumResp.status === 401 || sumResp.status === 403) {
    throw new Error("Materials Project rejected the API key");
  }
  if (!sumResp.ok) throw new Error(`Materials Project summary HTTP ${sumResp.status}`);
  const summary = (await sumResp.json()) as { data?: Record<string, unknown>[] };
  const thermo = thResp.ok
    ? ((await thResp.json()) as { data?: Record<string, unknown>[] })
    : { data: [] };
  const feById = new Map<string, number>();
  for (const row of thermo.data || []) {
    const id = String(row.material_id || "");
    const fe = num(row.formation_energy_per_atom);
    if (id && fe != null) feById.set(id, fe);
  }
  const entries: Entry[] = (summary.data || []).slice(0, 20).map((row) => {
    const id = String(row.material_id || "");
    const sym = row.symmetry as { symbol?: string } | undefined;
    return {
      source: "materials_project",
      id,
      spacegroup: String(sym?.symbol || "unknown"),
      formation_energy: feById.get(id) ?? null,
      band_gap: num(row.band_gap),
      experimental: row.theoretical === false,
    };
  });
  return { entries, n: entries.length, thermo_type: "GGA_GGA+U" };
}

function groupBySpacegroup(entries: Entry[]) {
  const map = new Map<string, Entry[]>();
  for (const e of entries) {
    const key = e.spacegroup || "unknown";
    const list = map.get(key) || [];
    list.push(e);
    map.set(key, list);
  }
  return [...map.entries()].map(([spacegroup, rows]) => {
    const mp = rows.filter((r) => r.source === "materials_project");
    const oq = rows.filter((r) => r.source === "oqmd");
    const mpFe = median(mp.map((r) => r.formation_energy));
    const oqFe = median(oq.map((r) => r.formation_energy));
    const spread = mpFe != null && oqFe != null ? Math.abs(mpFe - oqFe) : null;
    return {
      spacegroup,
      n_mp: mp.length,
      n_oqmd: oq.length,
      formation_energy_mp: mpFe,
      formation_energy_oqmd: oqFe,
      spread_ev_per_atom: spread != null ? round(spread, 4) : null,
      large_disagreement: spread != null && spread > 0.05,
      single_source: !(mp.length && oq.length),
    };
  });
}

function num(v: unknown): number | null {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function median(values: (number | null)[]): number | null {
  const xs = values.filter((v): v is number => v != null).sort((a, b) => a - b);
  if (!xs.length) return null;
  const mid = Math.floor(xs.length / 2);
  const v = xs.length % 2 ? xs[mid] : (xs[mid - 1] + xs[mid]) / 2;
  return round(v, 4);
}

function round(n: number, d: number) {
  const p = 10 ** d;
  return Math.round(n * p) / p;
}
