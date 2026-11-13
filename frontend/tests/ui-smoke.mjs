/**
 * UI smoke test: boots the built bundle inside jsdom against a live backend.
 *
 * Usage: node tests/ui-smoke.mjs [http://127.0.0.1:8000]
 * The backend must already have a task that scanned /workspace/demo/incoming.
 */
import { readFile } from "node:fs/promises";
import { JSDOM, VirtualConsole } from "jsdom";

const BASE = process.argv[2] ?? "http://127.0.0.1:8000";
const dist = new URL("../dist/", import.meta.url);

const html = await readFile(new URL("index.html", dist), "utf8");
const scriptMatch = html.match(/src="\.?\/?(assets\/[^"]+\.js)"/);
if (!scriptMatch) throw new Error("built bundle not found; run npm run build");
const bundle = await readFile(new URL(scriptMatch[1], dist), "utf8");

const virtualConsole = new VirtualConsole();
const problems = [];
virtualConsole.on("error", (msg) => problems.push(`console.error: ${msg}`));
virtualConsole.on("jsdomError", (err) => problems.push(`jsdom: ${err.message}`));

const dom = new JSDOM(html, {
  url: `${BASE}/`,
  runScripts: "outside-only",
  pretendToBeVisual: true,
  virtualConsole,
});

const { window } = dom;
window.fetch = (input, init) =>
  fetch(new URL(input, BASE).toString(), init);
window.TextDecoder = TextDecoder;
window.TextEncoder = TextEncoder;
window.eval(bundle);

const waitFor = async (predicate, timeoutMs = 8000, label = "") => {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const value = predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`timed out waiting for ${label}`);
};

const text = () => window.document.body.textContent ?? "";

await waitFor(() => text().includes("PKG-2026-001"), 8000, "package rows");

const packageTable = window.document.querySelector("table.grid");
const rows = [...packageTable.querySelectorAll("tbody tr")];
const packageRows = rows.filter((row) =>
  (row.textContent ?? "").includes("PKG-2026-"),
);
if (rows.length !== 7) {
  throw new Error(`expected 7 package rows, got ${rows.length}`);
}

const tableText = rows.map((r) => r.textContent ?? "").join("\n");
for (const state of ["已发布", "已关联", "冲突", "失败"]) {
  if (!tableText.includes(state)) throw new Error(`missing state label: ${state}`);
}
for (const reason of ["压缩包散列与清单不符", "路径不安全", "展开体积超限"]) {
  if (!tableText.includes(reason)) throw new Error(`missing failure text: ${reason}`);
}

// Open the published package detail.
const publishedRow = packageRows.find((row) =>
  (row.textContent ?? "").includes("已发布"),
);
publishedRow.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await waitFor(
  () => [...window.document.querySelectorAll(".section-title")].some(
    (el) => (el.textContent ?? "").includes("清单核对"),
  ),
  8000,
  "package detail panel",
);
if (!text().includes("校验通过")) throw new Error("no per-file results shown");

// Switch to batches and open the published batch.
const tabButtons = [...window.document.querySelectorAll(".segmented button")];
const batchesTab = tabButtons.find((b) => (b.textContent ?? "").includes("批次"));
batchesTab.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await waitFor(
  () => text().includes("批次与下载") && text().includes("ZIP 散列"),
  8000,
  "batch list",
);
const batchCards = [...window.document.querySelectorAll(".batch-card")];
const publishedCard = batchCards.find((card) =>
  (card.textContent ?? "").includes("已发布"),
);
if (!publishedCard) throw new Error("no published batch card");
publishedCard.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await waitFor(() => text().includes("下载 ZIP"), 8000, "batch panel");

// The README is the first verified file; preview it.
const previewButton = [...window.document.querySelectorAll(".link-btn")].find(
  (b) => (b.textContent ?? "").trim() === "预览" && !b.disabled,
);
if (!previewButton) throw new Error("no preview button");
previewButton.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await waitFor(
  () => window.document.querySelector("pre.code-view")?.textContent?.length,
  8000,
  "file preview",
);
const previewText = window.document.querySelector("pre.code-view")?.textContent ?? "";
if (!previewText.includes("订阅数据交付样例")) {
  throw new Error(`unexpected preview text: ${previewText.slice(0, 80)}`);
}

// Conflicts tab shows the conflict card and both resolutions.
const conflictTab = [...window.document.querySelectorAll(".segmented button")].find(
  (b) => (b.textContent ?? "").includes("冲突"),
);
conflictTab.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await waitFor(
  () => text().includes("保留已有批次") && text().includes("接收本次内容"),
  8000,
  "conflict card",
);

if (problems.length) {
  throw new Error(`UI errors:\n${problems.join("\n")}`);
}
console.log("UI smoke test passed: rows, detail, preview, batches, conflicts");

// jsdom keeps polling timers alive; exit explicitly once assertions pass.
process.exit(0);
