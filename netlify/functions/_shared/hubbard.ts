/** Documented +U tables, transcribed from src/materials_trust/hubbard.py. */

export const MP_U: Record<string, number> = {
  Co: 3.32, Cr: 3.7, Fe: 5.3, Mn: 3.9, Mo: 4.38, Ni: 6.2, V: 3.25, W: 6.2,
};

export const OQMD_U: Record<string, number> = {
  V: 3.1, Cr: 3.5, Mn: 3.8, Fe: 4.0, Co: 3.3, Ni: 6.4, Cu: 4.0,
  Th: 4.0, U: 4.0, Np: 4.0, Pu: 4.0,
};

const MP_ANIONS = new Set(["O", "F"]);
const OQMD_ANIONS = new Set(["O"]);
const SPIN_3D = new Set(["Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu"]);
const ACTINIDES = new Set([
  "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf", "Es", "Fm", "Md", "No", "Lr",
]);

export function elementsIn(formula: string): string[] {
  const found = formula.match(/[A-Z][a-z]?/g) || [];
  return [...new Set(found)];
}

export function compareHubbard(formula: string) {
  const els = elementsIn(formula);
  const hasMpAnion = els.some((e) => MP_ANIONS.has(e));
  const hasO = els.some((e) => OQMD_ANIONS.has(e));
  const mp: Record<string, number> = {};
  const oqmd: Record<string, number> = {};
  if (hasMpAnion) {
    for (const el of els) if (el in MP_U) mp[el] = MP_U[el];
  }
  if (hasO) {
    for (const el of els) if (el in OQMD_U) oqmd[el] = OQMD_U[el];
  }
  const onlyMp: Record<string, number> = {};
  const onlyOqmd: Record<string, number> = {};
  const differing: Record<string, [number, number]> = {};
  for (const el of new Set([...Object.keys(mp), ...Object.keys(oqmd)])) {
    if (el in mp && !(el in oqmd)) onlyMp[el] = mp[el];
    else if (el in oqmd && !(el in mp)) onlyOqmd[el] = oqmd[el];
    else if (mp[el] !== oqmd[el]) differing[el] = [mp[el], oqmd[el]];
  }
  const agrees = !Object.keys(onlyMp).length && !Object.keys(onlyOqmd).length && !Object.keys(differing).length;
  let explanation: string;
  if (!Object.keys(mp).length && !Object.keys(oqmd).length) {
    explanation = "Neither database applies a Hubbard U correction to this composition, so +U is not an available explanation for any disagreement between them.";
  } else {
    const parts: string[] = [];
    if (Object.keys(onlyMp).length) {
      const listed = Object.entries(onlyMp).map(([el, u]) => `${el} (U = ${u} eV)`).join(", ");
      parts.push(`Materials Project applies +U to ${listed} and OQMD does not. Materials Project applies +U to both oxides and fluorides, while OQMD applies it only to compounds containing oxygen.`);
    }
    if (Object.keys(onlyOqmd).length) {
      const listed = Object.entries(onlyOqmd).map(([el, u]) => `${el} (U minus J = ${u} eV)`).join(", ");
      parts.push(`OQMD applies +U to ${listed} and Materials Project does not.`);
    }
    if (Object.keys(differing).length) {
      const listed = Object.entries(differing).map(([el, [a, b]]) => `${el} (Materials Project ${a} eV, OQMD ${b} eV)`).join(", ");
      parts.push(`Both databases apply +U but with different parameters for ${listed}.`);
    }
    explanation = parts.join(" ");
  }
  return {
    formula,
    mp_u: mp,
    oqmd_u_minus_j: oqmd,
    agrees,
    explanation,
    oqmd_spin_polarised: els.some((e) => SPIN_3D.has(e) || ACTINIDES.has(e)),
  };
}
