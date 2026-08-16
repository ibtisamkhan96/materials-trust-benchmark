# Project Brief: Materials Data Trust Benchmark

**Read this whole file before writing any code.** It is the complete specification. It is written
to be handed to a coding agent with no prior context.

---

## 1. What this is

An open-source tool and public benchmark that answers one question about any materials property
value: **can I trust this number?**

Every AI-for-materials system today (DeepMind's GNoME, Microsoft's MatterGen, Materials Project,
and every startup in the space) is racing to *generate* materials and predict properties at scale.
Almost all of it is trained on, and validated against, DFT data. But that data:

- carries systematic errors (PBE underestimates band gaps by roughly 30 to 50 percent),
- depends on the functional and correction scheme used, so it is not comparable across sources,
- and **disagrees between major databases for the same material**.

So the field produces a flood of predictions with no standard way to separate the trustworthy
numbers from the confident-but-wrong ones. As generation scales, validation becomes the bottleneck.
This project builds the validation layer.

**Positioning:** this is not another property-prediction model. It is the quality-assurance layer
that sits on top of everyone else's databases and models.

**Author:** Ibtisam Ahmed Khan, materials engineer working in materials informatics. Publishes at
materialsdecoded.com. This project is the flagship artifact of that publication and the technical
seed of a UK company and an Innovate UK grant application.

---

## 2. The non-negotiable rule: physics correctness

**A physically wrong analysis is worse than no analysis.** This project's entire value is rigour,
so an error here destroys the point of it. Every one of the following is mandatory, not advisory.

### 2.1 Match structures, not formulas
Two entries with the same chemical formula are **not** necessarily the same material. TiO2 is
rutile, anatase, and brookite, with genuinely different properties. Comparing them and reporting
"the databases disagree" would be a fabricated disagreement and would invalidate the benchmark.

Use `pymatgen.analysis.structure_matcher.StructureMatcher` to establish that two entries describe
the same material before comparing any property. Where a structure match is impossible, record the
comparison as **polymorph-ambiguous** and report it separately. Never silently merge on formula.

### 2.2 Use formation energy, never raw total energy
Total energies from different codes, pseudopotentials, and settings are on different absolute
scales and are meaningless to compare. Use **formation energy per atom** (eV/atom). Normalise
units: some sources report per formula unit or kJ/mol.

Note also that databases apply their own corrections to formation energies (Materials Project
applies the MP2020 compatibility corrections including anion and GGA/GGA+U mixing corrections;
OQMD uses its own fitted elemental reference energies). These produce **systematic offsets**, which
is a real finding to quantify and report, not a bug to hide.

### 2.3 Compare like with like on functionals
Only compare values computed with the same exchange-correlation functional. PBE against PBE. Do
not compare a PBE gap to an HSE gap or to experiment as though they measure the same thing. Where a
source mixes GGA and GGA+U entries (Materials Project does this for transition-metal oxides and
fluorides), record which scheme was used and flag mixed comparisons.

### 2.4 Treat DFT band gaps as DFT band gaps
Standard PBE underestimates band gaps substantially. A DFT gap is not a prediction of the
experimental gap. A computed gap of 0.0 eV does not establish that a material is a metal. When
comparing DFT to experiment, this is the headline finding, quantify the gap, do not present the
DFT number as truth.

### 2.5 Magnetic state matters
The same structure computed ferromagnetic, antiferromagnetic, or non-spin-polarised gives different
energies and gaps. Record the magnetic ordering where the source exposes it, and flag comparisons
where it differs or is unknown.

### 2.6 Provenance on everything
Every value carried through the pipeline must retain: source database, source identifier,
functional, correction scheme, magnetic state, and whether the structure is experimentally observed
(ICSD-derived) or hypothetical. A value without provenance cannot be audited and must not be
reported.

---

## 3. What to build (MVP scope)

A Python package plus a public benchmark report. Keep it small and real. Do not build a web app, a
database, or a UI in this phase.

### 3.1 Core capabilities

**A. Cross-source audit.** For a set of materials, retrieve the same property from multiple
sources, match them structurally, and quantify agreement or disagreement.

- Primary sources: **Materials Project** (via `mp-api`) and **OQMD** (REST API).
- Properties: **formation energy per atom** (eV/atom) and **band gap** (eV).
- Output per material: every source value with full provenance, the spread across sources, and
  flags raised.

**B. DFT versus experiment.** Compare computed band gaps to measured ones. Use the curated
experimental band-gap dataset available through `matminer`
(`matminer.datasets.load_dataset("expt_gap")`, the Zhuo et al. compilation) or an equivalent
documented experimental source. Quantify the systematic underestimation: mean signed error, MAE,
and the distribution. This is the most citable result in the project.

**C. Physics-consistency checks.** Automated flags, each of which must be individually reported:

- `POLYMORPH_AMBIGUOUS`, formula matched but structures did not.
- `FUNCTIONAL_MISMATCH`, sources used different functionals or correction schemes.
- `MAGNETIC_UNKNOWN` or `MAGNETIC_MISMATCH`.
- `SINGLE_SOURCE`, no corroborating value exists, so agreement is unmeasurable.
- `HYPOTHETICAL`, structure is not experimentally observed.
- `LARGE_DISAGREEMENT`, spread exceeds a documented threshold.

**D. Trust report.** For each material, emit a structured record: the values, the spread, the flags,
and a confidence band. **Do not emit a single opaque score with no explanation.** A trust output
that cannot be interrogated is exactly the failure mode this project exists to criticise. If a
scalar score is produced, it must be accompanied by the specific flags and numbers that produced it,
and its formula must be documented in the README.

### 3.2 Deliverables

1. Installable package with a small CLI: audit a single material, or run a full benchmark.
2. A benchmark run over a documented material set (start with a few hundred well-known compounds
   with good coverage across sources; document exactly how the set was chosen).
3. A results report with real numbers and plots: agreement statistics, disagreement distribution,
   DFT-versus-experiment error, flag frequencies.
4. README stating method, physics rules applied, findings, and **limitations**.

---

## 3.3 Architecture: three layers, one hard boundary

The project is built in three layers. The boundary between layer 1 and the layers above it is the
most important design decision in the entire project.

**Layer 1, the deterministic core.** Data retrieval, structure matching, physics checks, statistics.
Pure Python. **No language model is involved at any point.** Given the same inputs it produces the
same outputs, every time, and every number is traceable to a source and a computation.

**Layer 2, the MCP server.** Exposes the core as tools over the Model Context Protocol, so any
MCP-capable client (Claude Desktop, an IDE, another agent, a customer's pipeline) can call them.
This is the product-shaped layer: it lets an AI system check a materials number before acting on it.

Suggested tools: `audit_material(identifier)`, `compare_sources(formula, property)`,
`check_physics_consistency(record)`, `get_provenance(source, id)`. Each returns the core's
structured output unchanged.

**Layer 3, the agent.** A LangGraph agent that orchestrates multi-step audits and, critically,
**explains** results in plain language: "these two sources differ by 0.31 eV/atom because Materials
Project applied a GGA+U correction to this transition-metal oxide and OQMD did not." Attribution is
the thing users actually want, and it is a genuinely good use of a language model. Use LangSmith to
trace and evaluate this layer.

### The hard boundary

> **A language model may never compute, estimate, adjust, or invent a numerical value.**

It may only: decide which deterministic tools to call, in what order, and translate the core's
structured output into readable explanation. Every quantitative claim it makes must correspond to a
value the core actually emitted, and the output should carry that value alongside the prose so a
reader can check it.

If a language model cannot answer from the core's output, the correct behaviour is to say so. A
trust product that fabricates is worthless, and the failure would be fatal to the project's entire
premise.

---

## 4. Suggested structure

```
materials-trust-benchmark/
  README.md
  requirements.txt
  .env.example            # MP_API_KEY=...      never commit real keys
  src/
    sources/              # one module per data source, each returns a common record type
      materials_project.py
      oqmd.py
      experimental.py
    matching.py           # StructureMatcher logic, the heart of correctness
    checks.py             # the physics-consistency flags
    audit.py              # orchestration: fetch, match, compare, flag
    report.py             # statistics and plots
    mcp_server.py         # layer 2: exposes the core as MCP tools
    agent.py              # layer 3: LangGraph agent, explanation only
  cli.py
  notebooks/
    benchmark.ipynb       # the runnable analysis behind the report
  data/                   # cached raw pulls (gitignored if large)
  results/                # generated outputs, committed
```

Build layer 1 first and prove it produces correct, real results before adding layers 2 and 3. The
MCP server and the agent are wrappers around a working core, not a substitute for one.

A shared record type across sources (source, id, formula, structure, property, value, units,
functional, correction scheme, magnetic state, is_experimental) keeps the comparison logic honest.

---

## 5. Rules for the build

1. **Real data only.** No synthetic or mocked values in results. If an API fails, the pipeline
   reports the failure. It never fabricates a number to fill a gap.
2. **Cache aggressively.** API pulls are slow and rate-limited. Cache to disk so reruns are cheap.
3. **Handle API reality.** OQMD's public API can be slow or intermittent. Handle timeouts and
   partial results explicitly, and record coverage (how many materials were found in each source).
4. **Report honestly.** If the databases agree more than expected, that is the finding. Do not tune
   the analysis toward a dramatic result. Publishing "they mostly agree, except in these specific
   physically-explainable cases" is a genuine and useful contribution.
5. **Explain every disagreement you can.** The value is not "they differ by 0.2 eV", it is "they
   differ by 0.2 eV **because** one used GGA+U and the other did not". Attribution is the product.
6. **No em dashes** anywhere in generated documentation or prose.
7. **Verify before claiming.** Run the code, look at the actual output, and report the real numbers.
   Do not describe results that have not been produced.

---

## 6. Explicitly out of scope for now

Web interface, hosted SaaS API, user accounts, databases beyond Materials Project and OQMD, and any
trained machine-learning model of material properties. Those come later. This phase produces one
credible, physics-correct benchmark with real findings, wrapped in an MCP server and an explanation
agent.

---

## 7. Setup notes

- Python 3.10+ with `pymatgen`, `mp-api`, `matminer`, `pandas`, `numpy`, `matplotlib`, `requests`.
- Layers 2 and 3 additionally need `mcp`, `langchain`, `langgraph`, and `langsmith`.
- No GPU is required anywhere in this project. It is API calls, structure matching, and statistics,
  and the language-model layer runs against a hosted API.
- A free Materials Project API key is required, read from the environment as `MP_API_KEY`. Never
  hardcode or commit it. The same applies to any model provider key and the LangSmith key.
- OQMD has a public REST API (`oqmd.org/oqmdapi`) needing no key, and `qmpy_rester` is an available
  client. Verify current endpoint behaviour before relying on it.

---

## 8. What success looks like

A public repository where a materials scientist can look at any number in the benchmark, see every
source that reported it, see whether those sources agree, and see a physically grounded explanation
of why they do not. That artifact is the proof of the thesis, the reputation piece, and the
technical core of the grant application.
