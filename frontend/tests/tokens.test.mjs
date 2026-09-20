import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/styles/tokens.css", import.meta.url), "utf8");

function block(selector) {
  const start = css.indexOf(selector);
  assert.ok(start >= 0, `selector ${selector} not found`);
  const open = css.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  throw new Error("unbalanced block");
}

function tokens(body) {
  const out = {};
  for (const [, name, value] of body.matchAll(/--([\w-]+):\s*([^;]+);/g)) {
    out[name] = value.trim();
  }
  return out;
}

const light = tokens(block(":root {"));
const dark = { ...light, ...tokens(block(':root[data-theme="dark"]')) };

const resolve = (theme, value) => {
  const ref = value.match(/^var\(--([\w-]+)\)$/);
  return ref ? resolve(theme, theme[ref[1]]) : value;
};

const hex = (value) => {
  const m = value.match(/^#([0-9a-f]{6})$/i);
  assert.ok(m, `expected hex colour, got ${value}`);
  return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255);
};

const luminance = (rgb) => {
  const [r, g, b] = rgb.map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

const contrast = (a, b) => {
  const [l1, l2] = [luminance(hex(a)), luminance(hex(b))].sort((x, y) => y - x);
  return (l1 + 0.05) / (l2 + 0.05);
};

const textTokens = ["text", "text-2", "text-3", "accent-text", "ok", "warn", "danger"];
const surfaces = ["bg", "surface", "surface-2"];

for (const [name, theme] of [
  ["light", light],
  ["dark", dark],
]) {
  test(`${name}: text tokens reach 4.5:1 on all surfaces`, () => {
    for (const fg of textTokens) {
      for (const bg of surfaces) {
        const ratio = contrast(resolve(theme, theme[fg]), resolve(theme, theme[bg]));
        assert.ok(
          ratio >= 4.5,
          `${name} --${fg} on --${bg}: ${ratio.toFixed(2)}:1 < 4.5`,
        );
      }
    }
  });

  test(`${name}: status text on its soft background reaches 4.5:1`, () => {
    for (const status of ["ok", "warn", "danger"]) {
      const ratio = contrast(
        resolve(theme, theme[status]),
        resolve(theme, theme[`${status}-soft`]),
      );
      assert.ok(ratio >= 4.5, `${name} --${status} on soft: ${ratio.toFixed(2)}:1`);
    }
  });

  test(`${name}: accent button label and non-text borders`, () => {
    const label = contrast(resolve(theme, theme["on-accent"]), resolve(theme, theme.accent));
    assert.ok(label >= 4.5, `${name} --on-accent on --accent: ${label.toFixed(2)}:1`);
    const border = contrast(
      resolve(theme, theme["border-strong"]),
      resolve(theme, theme.surface),
    );
    assert.ok(border >= 2, `${name} --border-strong on surface: ${border.toFixed(2)}:1`);
  });
}

test("dark block overrides every colour token the light block defines", () => {
  const darkOnly = tokens(block(':root[data-theme="dark"]'));
  const mediaDark = tokens(block(':root:not([data-theme="light"])'));
  assert.deepEqual(darkOnly, mediaDark, "auto and forced dark palettes must be identical");
  for (const name of Object.keys(darkOnly)) {
    assert.ok(name in light, `--${name} defined for dark only`);
  }
});
