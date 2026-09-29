/**
 * Shared colour-maths + globals.css reading helpers for the token invariant
 * fences (chart-palette-invariant.test.ts, status-contrast-invariant.test.ts).
 * Kept minimal: only what more than one fence needs.
 */
import postcss from "postcss";

export type Vec3 = [number, number, number];

const lin = (c: number) =>
  c < 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);

export function hexToRgb(hex: string): Vec3 {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) throw new Error(`not a #rrggbb colour: ${JSON.stringify(hex)}`);
  return [1, 2, 3].map((i) => parseInt(m[i], 16) / 255) as Vec3;
}

/** #rrggbb or rgba(r, g, b, a) (a defaults to 1 for rgb()). */
export function parseColor(value: string): { rgb: Vec3; alpha: number } {
  const v = value.trim();
  if (v.startsWith("#")) return { rgb: hexToRgb(v), alpha: 1 };
  const m = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(v);
  if (!m) throw new Error(`not a #rrggbb or rgba() colour: ${JSON.stringify(value)}`);
  const rgb: Vec3 = [Number(m[1]) / 255, Number(m[2]) / 255, Number(m[3]) / 255];
  const alpha = m[4] === undefined ? 1 : Number(m[4]);
  return { rgb, alpha };
}

/** Alpha-composite fg (with alpha) over an opaque bg, both linear-free sRGB 0..1 triples. */
export function compositeOver(fg: { rgb: Vec3; alpha: number }, bg: Vec3): Vec3 {
  return fg.rgb.map((c, i) => c * fg.alpha + bg[i] * (1 - fg.alpha)) as Vec3;
}

/** WCAG 2.x relative-luminance contrast ratio between two opaque sRGB colours. */
export function contrast(a: Vec3, b: Vec3): number {
  const Y = (c: Vec3) => {
    const [r, g, bl] = c.map(lin);
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [Y(a), Y(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** All custom-property declarations of one top-level rule (e.g. `:root`) in a parsed sheet. */
export function block(sheet: postcss.Root, selector: string): Map<string, string> {
  const rules = (sheet.nodes ?? []).filter(
    (n): n is postcss.Rule => n.type === "rule" && n.selector === selector,
  );
  if (rules.length !== 1) {
    throw new Error(`expected exactly one top-level ${selector} block, found ${rules.length}`);
  }
  const props = new Map<string, string>();
  rules[0].walkDecls((d) => {
    if (d.prop.startsWith("--")) props.set(d.prop, d.value.trim());
  });
  return props;
}
