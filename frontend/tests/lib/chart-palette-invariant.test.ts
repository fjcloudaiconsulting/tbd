/**
 * Categorical chart palette invariants (TBD-429).
 *
 * Measures the `--theme-cat-1..8` tokens in BOTH theme blocks of
 * `app/globals.css` and asserts the properties the palette exists to have,
 * whatever hexes are chosen:
 *
 *  1. every slot is a WCAG 1.4.11 graphic: >= 3:1 against `--theme-surface`;
 *  2. every pair, including "Other" (`--theme-border-strong`, the fold bucket
 *     in lib/reports/breakdown.ts), stays >= 5 CIEDE2000 apart under normal
 *     vision AND simulated protan/deutan/tritan dichromacy, under BOTH the
 *     Vienot 1999 and the Brettel 1997 models;
 *  3. no slot reads as a STATUS: every slot is >= SEMANTIC_MIN CIEDE2000 from
 *     `--theme-success`, `--theme-danger`, `--theme-warning` and
 *     `--theme-accent` in the same theme (PRODUCT.md, status-is-data);
 *  4. a slot's light and dark variants are the same hue family (OKLCH hue).
 *
 * The maths is guarded by reference vectors, so a typo'd matrix or a broken
 * CIEDE2000 cannot quietly make every palette pass: the simulators against
 * DaltonLens (daltonlens 0.1.x Python, sRGB/Smith-Pokorny LMS model), and
 * CIEDE2000 against the Sharma, Wu & Dalal (2005) test data.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import postcss from "postcss";

const CONTRAST_MIN = 3;
const CVD_DE_MIN = 5;
const SEMANTIC_DE_MIN = 12;
const HUE_FAMILY_MAX_DEG = 20;

const GLOBALS = path.resolve(__dirname, "..", "..", "app", "globals.css");
const sheet = postcss.parse(readFileSync(GLOBALS, "utf8"), { from: GLOBALS });

type Vec3 = [number, number, number];
type Mat3 = [Vec3, Vec3, Vec3];

// ─── colour maths ────────────────────────────────────────────────────────

const lin = (c: number) =>
  c < 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
const unlin = (c: number) => {
  const x = Math.min(1, Math.max(0, c));
  return x < 0.0031308 ? x * 12.92 : 1.055 * Math.pow(x, 1 / 2.4) - 0.055;
};
const mul = (m: Mat3, v: Vec3): Vec3 => [
  m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
  m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
  m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
];

function hexToRgb(hex: string): Vec3 {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) throw new Error(`not a #rrggbb colour: ${JSON.stringify(hex)}`);
  return [1, 2, 3].map((i) => parseInt(m[i], 16) / 255) as Vec3;
}

// Linear-sRGB dichromacy matrices, generated from the daltonlens package
// (Simulator_Vienot1999 / Simulator_Brettel1997, default LMS model).
type Kind = "protan" | "deutan" | "tritan";
const VIENOT: Record<Kind, Mat3> = {
  protan: [
    [0.10888931, 0.89111069, 0],
    [0.10888931, 0.89111069, 0],
    [0.00447131, -0.00447131, 1],
  ],
  deutan: [
    [0.29030532, 0.70969468, 0],
    [0.29030532, 0.70969468, 0],
    [-0.02197354, 0.02197354, 1],
  ],
  tritan: [
    [1, 0.15236201, -0.15236201],
    [0, 0.86717322, 0.13282678],
    [0, 0.86717322, 0.13282678],
  ],
};
const BRETTEL: Record<Kind, { m1: Mat3; m2: Mat3; sep: Vec3 }> = {
  protan: {
    m1: [
      [0.14509619, 1.20165322, -0.34674941],
      [0.10446501, 0.85316393, 0.04237106],
      [0.00428964, -0.00602952, 1.00173988],
    ],
    m2: [
      [0.14115172, 1.16782194, -0.30897366],
      [0.10494700, 0.85729795, 0.03775505],
      [0.00430943, -0.00585976, 1.00155033],
    ],
    sep: [0.07754968, 0.66513533, -0.74268502],
  },
  deutan: {
    m1: [
      [0.36198239, 0.86754666, -0.22952906],
      [0.26098534, 0.64512428, 0.09389038],
      [-0.01975428, 0.02686095, 0.99289333],
    ],
    m2: [
      [0.37009001, 0.88540179, -0.25549180],
      [0.25766886, 0.63782052, 0.10451062],
      [-0.01950325, 0.02741378, 0.99208947],
    ],
    sep: [-0.24918770, -0.54877761, 0.79796531],
  },
  tritan: {
    m1: [
      [1.01354162, 0.14268231, -0.15622393],
      [-0.01180536, 0.87561183, 0.13619353],
      [0.07707253, 0.81208091, 0.11084655],
    ],
    m2: [
      [0.93336976, 0.19999005, -0.13335981],
      [0.05808718, 0.82565186, 0.11626096],
      [-0.37922811, 1.13824973, 0.24097838],
    ],
    sep: [0.79248167, -0.56647479, -0.22600689],
  },
};

function simulate(rgb: Vec3, kind: Kind, model: "vienot" | "brettel"): Vec3 {
  const l = rgb.map(lin) as Vec3;
  let out: Vec3;
  if (model === "vienot") {
    out = mul(VIENOT[kind], l);
  } else {
    const p = BRETTEL[kind];
    const side = l[0] * p.sep[0] + l[1] * p.sep[1] + l[2] * p.sep[2];
    out = mul(side < 0 ? p.m2 : p.m1, l);
  }
  return out.map(unlin) as Vec3;
}

function rgbToLab(rgb: Vec3): Vec3 {
  const [r, g, b] = rgb.map(lin);
  const x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047;
  const y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b;
  const z = (0.0193339 * r + 0.119192 * g + 0.9503041 * b) / 1.08883;
  const f = (t: number) =>
    t > 216 / 24389 ? Math.cbrt(t) : ((24389 / 27) * t + 16) / 116;
  const [fx, fy, fz] = [f(x), f(y), f(z)];
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

const rad = (d: number) => (d * Math.PI) / 180;
const deg = (r: number) => (r * 180) / Math.PI;

function ciede2000(l1: Vec3, l2: Vec3): number {
  const [L1, a1, b1] = l1;
  const [L2, a2, b2] = l2;
  const Cb = (Math.hypot(a1, b1) + Math.hypot(a2, b2)) / 2;
  const G = 0.5 * (1 - Math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)));
  const a1p = (1 + G) * a1;
  const a2p = (1 + G) * a2;
  const C1p = Math.hypot(a1p, b1);
  const C2p = Math.hypot(a2p, b2);
  const h1p = (deg(Math.atan2(b1, a1p)) + 360) % 360;
  const h2p = (deg(Math.atan2(b2, a2p)) + 360) % 360;
  const dLp = L2 - L1;
  const dCp = C2p - C1p;
  let dhp = 0;
  if (C1p * C2p !== 0) {
    dhp = h2p - h1p;
    if (dhp > 180) dhp -= 360;
    else if (dhp < -180) dhp += 360;
  }
  const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin(rad(dhp / 2));
  const Lbp = (L1 + L2) / 2;
  const Cbp = (C1p + C2p) / 2;
  let hbp = h1p + h2p;
  if (C1p * C2p !== 0) {
    if (Math.abs(h1p - h2p) <= 180) hbp = (h1p + h2p) / 2;
    else if (h1p + h2p < 360) hbp = (h1p + h2p + 360) / 2;
    else hbp = (h1p + h2p - 360) / 2;
  }
  const T =
    1 -
    0.17 * Math.cos(rad(hbp - 30)) +
    0.24 * Math.cos(rad(2 * hbp)) +
    0.32 * Math.cos(rad(3 * hbp + 6)) -
    0.2 * Math.cos(rad(4 * hbp - 63));
  const dTheta = 30 * Math.exp(-(((hbp - 275) / 25) ** 2));
  const RC = 2 * Math.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7));
  const SL =
    1 + (0.015 * (Lbp - 50) ** 2) / Math.sqrt(20 + (Lbp - 50) ** 2);
  const SC = 1 + 0.045 * Cbp;
  const SH = 1 + 0.015 * Cbp * T;
  const RT = -Math.sin(rad(2 * dTheta)) * RC;
  return Math.sqrt(
    (dLp / SL) ** 2 +
      (dCp / SC) ** 2 +
      (dHp / SH) ** 2 +
      RT * (dCp / SC) * (dHp / SH),
  );
}

function contrast(a: Vec3, b: Vec3): number {
  const Y = (c: Vec3) => {
    const [r, g, bl] = c.map(lin);
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [Y(a), Y(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function oklchHue(rgb: Vec3): number {
  const [r, g, b] = rgb.map(lin);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  return (deg(Math.atan2(B, A)) + 360) % 360;
}

// ─── reading the tokens ──────────────────────────────────────────────────

const BLOCKS = { dark: ":root", light: '[data-theme="light"]' } as const;
const MEASURED_TOKEN = /^--theme-(cat-\d+|surface|border-strong|success|danger|warning|accent)$/;
type Theme = keyof typeof BLOCKS;

function block(selector: string): Map<string, string> {
  const rules = (sheet.nodes ?? []).filter(
    (n): n is postcss.Rule => n.type === "rule" && n.selector === selector,
  );
  expect(rules, `exactly one top-level ${selector} block`).toHaveLength(1);
  const props = new Map<string, string>();
  rules[0].walkDecls((d) => {
    if (d.prop.startsWith("--")) props.set(d.prop, d.value.trim());
  });
  return props;
}

function tokens(theme: Theme) {
  const props = block(BLOCKS[theme]);
  const catNames = [...props.keys()].filter((k) => k.startsWith("--theme-cat-"));
  const get = (name: string) => {
    const v = props.get(name);
    if (v === undefined) throw new Error(`${name} missing from ${BLOCKS[theme]}`);
    return v;
  };
  return {
    catNames,
    cat: Array.from({ length: 8 }, (_, i) => get(`--theme-cat-${i + 1}`)),
    other: get("--theme-border-strong"),
    surface: get("--theme-surface"),
    semantic: {
      success: get("--theme-success"),
      danger: get("--theme-danger"),
      warning: get("--theme-warning"),
      accent: get("--theme-accent"),
    },
  };
}

const THEMES: Theme[] = ["light", "dark"];
const MODELS = ["vienot", "brettel"] as const;
const KINDS: Kind[] = ["protan", "deutan", "tritan"];
const CVD_LABELS = ["normal", ...MODELS.flatMap((m) => KINDS.map((k) => `${m} ${k}`))];

/**
 * Every pair (slots 1..n-1 named cat-N, the last colour named Other) closer
 * than CVD_DE_MIN, keyed by viewing condition. Every condition key is present,
 * with an empty list when it has no failing pair.
 */
function cvdFailures(hexes: string[]): Record<string, Array<[string, number]>> {
  const names = hexes.map((_, i) => (i === hexes.length - 1 ? "Other" : `cat-${i + 1}`));
  const rgb = hexes.map(hexToRgb);
  const conditions: Array<[string, (c: Vec3) => Vec3]> = [
    ["normal", (c) => c],
    ...MODELS.flatMap((model) =>
      KINDS.map(
        (kind) => [`${model} ${kind}`, (c: Vec3) => simulate(c, kind, model)] as [string, (c: Vec3) => Vec3],
      ),
    ),
  ];
  const out: Record<string, Array<[string, number]>> = {};
  let pairs = 0;
  for (const [label, fn] of conditions) {
    const labs = rgb.map((c) => rgbToLab(fn(c)));
    out[label] = [];
    for (let i = 0; i < labs.length; i++) {
      for (let j = i + 1; j < labs.length; j++) {
        pairs++;
        const d = ciede2000(labs[i], labs[j]);
        if (d < CVD_DE_MIN) out[label].push([`${names[i]} vs ${names[j]}`, d]);
      }
    }
  }
  const n = hexes.length;
  expect(pairs, "every pair under every condition").toBe((CVD_LABELS.length * n * (n - 1)) / 2);
  return out;
}

// ─── guards on the maths ─────────────────────────────────────────────────

describe("palette maths reference vectors", () => {
  // daltonlens Simulator_*.simulate_cvd, severity 1, float sRGB output.
  const REFS: Array<{
    model: "vienot" | "brettel";
    kind: Kind;
    out: Vec3[];
  }> = [{"model": "vienot", "kind": "protan", "out": [[0.3638, 0.3638, 0.0557], [0.9505, 0.9505, 0.0], [0.0, 0.0, 1.0], [0.6737, 0.6737, 0.2928]]}, {"model": "vienot", "kind": "deutan", "out": [[0.5751, 0.5751, 0.0], [0.8595, 0.8595, 0.16], [0.0, 0.0, 1.0], [0.7096, 0.7096, 0.2771]]}, {"model": "vienot", "kind": "tritan", "out": [[1.0, 0.0, 0.0], [0.4267, 0.9392, 0.9392], [0.0, 0.3999, 0.3999], [0.8576, 0.6178, 0.6178]]}, {"model": "brettel", "kind": "protan", "out": [[0.417, 0.3566, 0.0538], [1.0, 0.9325, 0.0], [0.0, 0.2144, 1.0], [0.7548, 0.6629, 0.2915]]}, {"model": "brettel", "kind": "deutan", "out": [[0.6422, 0.5446, 0.0], [0.9478, 0.8197, 0.1807], [0.0, 0.3387, 0.9969], [0.7759, 0.68, 0.284]]}, {"model": "brettel", "kind": "tritan", "out": [[1.0, 0.0, 0.3076], [0.4845, 0.9191, 1.0], [0.0, 0.3754, 0.5281], [0.8602, 0.6143, 0.6406]]}];
  const INPUTS: Vec3[] = [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
    hexToRgb("#d4a64a"),
  ];

  it.each(REFS.map((r) => [r.model, r.kind, r] as const))(
    "%s %s matches DaltonLens within 0.01",
    (_m, _k, ref) => {
      INPUTS.forEach((input, i) => {
        const got = simulate(input, ref.kind, ref.model);
        got.forEach((c, ch) =>
          expect(Math.abs(c - ref.out[i][ch]), `input ${i} channel ${ch}`).toBeLessThanOrEqual(0.01),
        );
      });
    },
  );

  it("CIEDE2000 matches Sharma, Wu & Dalal (2005)", () => {
    const SHARMA: Array<[Vec3, Vec3, number]> = [
      [[50, 2.6772, -79.7751], [50, 0, -82.7485], 2.0425],
      [[50, 3.1571, -77.2803], [50, 0, -82.7485], 2.8615],
      [[50, 2.8361, -74.02], [50, 0, -82.7485], 3.4412],
      [[50, 0, 0], [50, -1, 2], 2.3669],
      [[50, 2.49, -0.001], [50, -2.49, 0.0009], 7.1792],
      [[50, 2.5, 0], [73, 25, -18], 27.1492],
      [[50, 2.5, 0], [50, 3.1736, 0.5854], 1.0],
      [[60.2574, -34.0099, 36.2677], [60.4626, -34.1751, 39.4387], 1.2644],
      [[2.0776, 0.0795, -1.135], [0.9033, -0.0636, -0.5514], 0.9082],
    ];
    for (const [a, b, want] of SHARMA) {
      expect(ciede2000(a, b)).toBeCloseTo(want, 4);
      expect(ciede2000(b, a)).toBeCloseTo(want, 4);
    }
  });

  it("sRGB to CIELAB, WCAG contrast and OKLCH hue match known values", () => {
    rgbToLab([1, 0, 0]).forEach((c, i) =>
      expect(c).toBeCloseTo([53.2408, 80.0925, 67.2032][i], 2),
    );
    expect(contrast(hexToRgb("#777777"), hexToRgb("#ffffff"))).toBeCloseTo(4.478, 3);
    expect(oklchHue([1, 0, 0])).toBeCloseTo(29.23, 1);
    expect(oklchHue(hexToRgb("#8a6a1f"))).toBeCloseTo(84.43, 1);
  });
});

// ─── the palette ─────────────────────────────────────────────────────────

describe("categorical chart palette (globals.css)", () => {
  it("@theme exposes chart-1..8 and Other straight from the theme tokens", () => {
    const aliases = new Map<string, string>();
    sheet.walkAtRules("theme", (at) =>
      at.walkDecls((d) => {
        aliases.set(d.prop, d.value.trim());
      }),
    );
    for (let i = 1; i <= 8; i++) {
      expect(aliases.get(`--color-chart-${i}`)).toBe(`var(--theme-cat-${i})`);
    }
    expect(aliases.get("--color-border-strong")).toBe("var(--theme-border-strong)");
  });

  it.each(THEMES)("%s: parses exactly --theme-cat-1..8", (theme) => {
    const { catNames, cat } = tokens(theme);
    expect([...catNames].sort()).toEqual(
      Array.from({ length: 8 }, (_, i) => `--theme-cat-${i + 1}`).sort(),
    );
    cat.forEach((hex) => expect(() => hexToRgb(hex)).not.toThrow());
  });

  it("the measured tokens are declared ONLY in the two owning theme rules", () => {
    // A redeclaration anywhere else (a prefers-color-scheme block, a scoped
    // class, html[data-theme], an at-rule nested in :root) would repaint the
    // palette without this file ever reading it.
    const owners = new Set<postcss.Node>(
      Object.values(BLOCKS).map(
        (sel) =>
          (sheet.nodes ?? []).find((n) => n.type === "rule" && (n as postcss.Rule).selector === sel)!,
      ),
    );
    expect(owners.size).toBe(2);
    const stray: string[] = [];
    let seen = 0;
    sheet.walkDecls(MEASURED_TOKEN, (d) => {
      seen++;
      if (!owners.has(d.parent!)) {
        const where = d.parent?.type === "rule" ? (d.parent as postcss.Rule).selector : d.parent?.toString().split("{")[0].trim();
        stray.push(`${d.prop} in ${where} (line ${d.source?.start?.line})`);
      }
    });
    expect(seen, "8 cat + 6 measured tokens in each owning block").toBeGreaterThanOrEqual(28);
    expect(stray).toEqual([]);
  });

  it.each(THEMES)(`%s: every slot is >= ${CONTRAST_MIN}:1 on the surface`, (theme) => {
    const t = tokens(theme);
    const surface = hexToRgb(t.surface);
    const failures = t.cat
      .map((hex, i) => [i + 1, hex, contrast(hexToRgb(hex), surface)] as const)
      .filter(([, , cr]) => cr < CONTRAST_MIN)
      .map(([slot, hex, cr]) => `cat-${slot} ${hex} ${cr.toFixed(2)}:1`);
    expect(failures).toEqual([]);
  });

  it.each(THEMES)(
    `%s: every pair incl. Other is >= ${CVD_DE_MIN} dE2000 under normal and dichromat vision (Vienot + Brettel)`,
    (theme) => {
      const t = tokens(theme);
      const failures = cvdFailures([...t.cat, t.other]);
      expect(Object.keys(failures)).toEqual(CVD_LABELS);
      expect(
        Object.entries(failures).flatMap(([label, pairs]) =>
          pairs.map(([pair, d]) => `${label}: ${pair} dE ${d.toFixed(2)}`),
        ),
      ).toEqual([]);
    },
  );

  it("canary: the pre-TBD-429 light palette fails under every simulation, and only there", () => {
    // Proves the simulation is APPLIED per condition: a loop that skipped
    // simulate() would report nothing below, and a loop that wired every
    // deficiency to one simulator would report the same pairs for all three.
    // Measured with daltonlens (Python) on the palette this ticket replaced.
    const OLD_LIGHT = ["#B88A2E", "#2f7fb0", "#16a34a", "#7c3aed", "#0d9488", "#db2777", "#d97706", "#dc2626"];
    const failures = cvdFailures([...OLD_LIGHT, "#818ea3"]);
    const pairsOf = (label: string) => failures[label].map(([pair]) => pair);
    expect(pairsOf("normal")).toEqual([]);
    for (const model of MODELS) {
      expect(pairsOf(`${model} protan`)).toEqual(["cat-1 vs cat-3", "cat-1 vs cat-7"]);
      expect(pairsOf(`${model} deutan`)).toEqual(["cat-1 vs cat-7"]);
      expect(pairsOf(`${model} tritan`)).toEqual([
        "cat-2 vs cat-5",
        "cat-3 vs cat-5",
        "cat-6 vs cat-8",
      ]);
    }
  });

  it.each(THEMES)(
    `%s: no slot is within ${SEMANTIC_DE_MIN} dE2000 of a status or accent token`,
    (theme) => {
      const t = tokens(theme);
      const failures: string[] = [];
      t.cat.forEach((hex, i) => {
        const lab = rgbToLab(hexToRgb(hex));
        for (const [name, value] of Object.entries(t.semantic)) {
          const d = ciede2000(lab, rgbToLab(hexToRgb(value)));
          if (d < SEMANTIC_DE_MIN) {
            failures.push(`cat-${i + 1} ${hex} vs ${name} ${value}: dE ${d.toFixed(2)}`);
          }
        }
      });
      expect(failures).toEqual([]);
    },
  );

  it(`a slot's light and dark variants share a hue family (OKLCH hue within ${HUE_FAMILY_MAX_DEG} deg)`, () => {
    const light = tokens("light").cat;
    const dark = tokens("dark").cat;
    const failures = light
      .map((hex, i) => {
        const d = Math.abs(
          ((oklchHue(hexToRgb(hex)) - oklchHue(hexToRgb(dark[i])) + 540) % 360) - 180,
        );
        return [i + 1, d] as const;
      })
      .filter(([, d]) => d > HUE_FAMILY_MAX_DEG)
      .map(([slot, d]) => `cat-${slot}: ${d.toFixed(1)} deg`);
    expect(failures).toEqual([]);
  });
});
