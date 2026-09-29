"""Build eval/label.html: a page for labelling candidates good / ok / bad per room.

    python -m eval.label eval/runs/baseline.json [more runs...]

Shows every candidate any of the given runs retrieved for a case, with existing
labels from eval/labels.json pre-filled. Open the page in a browser, label, then
click "Download labels.json" and save it over eval/labels.json.
"""
import argparse
import json
from pathlib import Path

from eval.metrics import load_labels

EVAL_DIR = Path(__file__).resolve().parent
OUT = EVAL_DIR / "label.html"


def build_cases(runs):
    """Merge runs into [{id, room, furniture_type, candidates: [...]}], unioning candidates."""
    merged = {}
    for run in runs:
        for case in run["cases"]:
            if case.get("error"):
                continue
            entry = merged.setdefault(case["id"], {
                "id": case["id"], "room": case["room"], "furniture_type": case["furniture_type"],
                "analysis": case.get("analysis"), "candidates": {},
            })
            primary = case["repeats"][0]["scores"]
            for c in case["candidates"]:
                cand = entry["candidates"].setdefault(c["id"], {**c, "model": []})
                if c["id"] in primary:
                    cand["model"].append({"run": run["name"], **primary[c["id"]]})
    out = []
    for entry in merged.values():
        entry["candidates"] = list(entry["candidates"].values())
        out.append(entry)
    return out


def render(cases, labels):
    data = json.dumps({"cases": cases, "labels": labels}).replace("</", "<\\/")
    return PAGE.replace("__DATA__", data)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--labels", default=str(EVAL_DIR / "labels.json"))
    args = parser.parse_args()
    runs = [json.loads(Path(p).read_text()) for p in args.runs]
    cases = build_cases(runs)
    OUT.write_text(render(cases, load_labels(args.labels)))
    total = sum(len(c["candidates"]) for c in cases)
    print(f"Wrote {OUT} — {len(cases)} rooms, {total} candidates")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Decora Labelling</title>
<style>
  :root {
    --bg: #f7f6f3; --panel: #ffffff; --text: #1d1d1b; --muted: #6b6a66; --line: #e3e1dc;
    --good: #2f7d4f; --ok: #b07d12; --bad: #b23b3b; --accent: #1d1d1b;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #161615; --panel: #1f1f1e; --text: #ecebe7; --muted: #a09f9a; --line: #33332f;
            --good: #5fbf85; --ok: #e0b24a; --bad: #e57373; --accent: #ecebe7; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  header { position: sticky; top: 0; z-index: 2; background: var(--bg); border-bottom: 1px solid var(--line);
           padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
  header h1 { font-size: 16px; margin: 0 auto 0 0; }
  .progress { color: var(--muted); font-variant-numeric: tabular-nums; }
  button, select { font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--line);
                   border-radius: 6px; padding: 6px 12px; cursor: pointer; }
  button.primary { background: var(--accent); color: var(--bg); border-color: var(--accent); }
  label.toggle { color: var(--muted); display: flex; gap: 6px; align-items: center; cursor: pointer; }
  main { max-width: 1200px; margin: 0 auto; padding: 16px; }
  .room { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr); gap: 20px; margin-bottom: 20px; }
  .room img { width: 100%; border-radius: 8px; display: block; }
  .room h2 { margin: 0 0 4px; font-size: 20px; }
  .room .meta { color: var(--muted); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 14px; }
  .card { background: var(--panel); border: 2px solid var(--line); border-radius: 8px; padding: 10px;
          display: flex; flex-direction: column; gap: 6px; }
  .card[data-label=good] { border-color: var(--good); }
  .card[data-label=ok] { border-color: var(--ok); }
  .card[data-label=bad] { border-color: var(--bad); }
  .card img { width: 100%; aspect-ratio: 1; object-fit: contain; background: #fff; border-radius: 4px; }
  .card .name { font-weight: 600; }
  .card .type, .card .model { color: var(--muted); font-size: 13px; }
  .model { display: none; }
  body.show-model .model { display: block; }
  .buttons { display: flex; gap: 6px; margin-top: auto; }
  .buttons button { flex: 1; padding: 6px 0; }
  .buttons button.on[data-v=good] { background: var(--good); border-color: var(--good); color: #fff; }
  .buttons button.on[data-v=ok] { background: var(--ok); border-color: var(--ok); color: #fff; }
  .buttons button.on[data-v=bad] { background: var(--bad); border-color: var(--bad); color: #fff; }
  .nav { display: flex; justify-content: space-between; margin-top: 20px; }
  .help { color: var(--muted); font-size: 13px; margin: 0 0 16px; }
  @media (max-width: 700px) { .room { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>Decora labelling</h1>
  <select id="pick" aria-label="Room"></select>
  <span class="progress" id="progress"></span>
  <label class="toggle"><input type="checkbox" id="showModel"> show model scores</label>
  <button class="primary" id="download">Download labels.json</button>
</header>
<main>
  <p class="help">Would you put this product in this room? <b>Good</b> = you'd happily recommend it.
  <b>OK</b> = acceptable, not inspired. <b>Bad</b> = clashes, or isn't the requested furniture.
  Model scores are hidden by default so they don't anchor you. Labels autosave in this browser;
  download them and save over <code>eval/labels.json</code> when done.</p>
  <section class="room" id="room"></section>
  <section class="grid" id="grid"></section>
  <div class="nav"><button id="prev">&larr; Previous room</button><button id="next">Next room &rarr;</button></div>
</main>
<script>
const DATA = __DATA__;
const STORE = "decora-labels";
let labels = DATA.labels || {};
try {
  const saved = JSON.parse(localStorage.getItem(STORE) || "null");
  if (saved) for (const [c, v] of Object.entries(saved)) labels[c] = Object.assign({}, labels[c], v);
} catch (e) {}
const save = () => { try { localStorage.setItem(STORE, JSON.stringify(labels)); } catch (e) {} };
let idx = 0;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function progress() {
  let done = 0, total = 0;
  for (const c of DATA.cases) for (const p of c.candidates) { total++; if (labels[c.id]?.[p.id]) done++; }
  document.getElementById("progress").textContent = `${done}/${total} labelled`;
  const pick = document.getElementById("pick");
  DATA.cases.forEach((c, i) => {
    const n = c.candidates.filter(p => labels[c.id]?.[p.id]).length;
    pick.options[i].textContent = `${c.id} — ${c.furniture_type} (${n}/${c.candidates.length})`;
  });
}

function show(i) {
  idx = (i + DATA.cases.length) % DATA.cases.length;
  const c = DATA.cases[idx];
  document.getElementById("pick").value = idx;
  const a = c.analysis || {};
  document.getElementById("room").innerHTML = `
    <img src="${esc(c.room)}" alt="Room photo">
    <div><h2>${esc(c.furniture_type)} for ${esc(c.id)}</h2>
    <p class="meta">Gemini read it as: ${esc(a.style)} · ${esc((a.colors || []).join(", "))} · ${esc(a.mood)}<br>
    Search keywords: ${esc(a.search_keywords)}</p></div>`;
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  for (const p of c.candidates) {
    const v = labels[c.id]?.[p.id] || "";
    const model = p.model.map(m => `${esc(m.run)}: ${m.score}/10 — ${esc(m.reason)}`).join("<br>");
    const card = document.createElement("div");
    card.className = "card";
    card.dataset.label = v;
    card.innerHTML = `
      <a href="${esc(p.url)}" target="_blank" rel="noopener"><img src="${esc(p.image)}" alt="${esc(p.name)}" loading="lazy"></a>
      <div class="name">${esc(p.name)}</div>
      <div class="type">${esc(p.type_name)}${p.price != null ? " · $" + esc(p.price) : ""}</div>
      <div class="model">${model || "not scored"}</div>
      <div class="buttons">
        <button data-v="good">Good</button><button data-v="ok">OK</button><button data-v="bad">Bad</button>
      </div>`;
    card.querySelectorAll(".buttons button").forEach(b => {
      b.classList.toggle("on", b.dataset.v === v);
      b.onclick = () => {
        labels[c.id] = labels[c.id] || {};
        if (labels[c.id][p.id] === b.dataset.v) delete labels[c.id][p.id];
        else labels[c.id][p.id] = b.dataset.v;
        const cur = labels[c.id][p.id] || "";
        card.dataset.label = cur;
        card.querySelectorAll(".buttons button").forEach(x => x.classList.toggle("on", x.dataset.v === cur));
        save(); progress();
      };
    });
    grid.appendChild(card);
  }
  window.scrollTo(0, 0);
}

const pick = document.getElementById("pick");
DATA.cases.forEach((c, i) => pick.add(new Option(c.id, i)));
pick.onchange = () => show(+pick.value);
document.getElementById("prev").onclick = () => show(idx - 1);
document.getElementById("next").onclick = () => show(idx + 1);
document.getElementById("showModel").onchange = e => document.body.classList.toggle("show-model", e.target.checked);
document.getElementById("download").onclick = () => {
  const clean = {};
  for (const [c, v] of Object.entries(labels)) if (Object.keys(v).length) clean[c] = v;
  const blob = new Blob([JSON.stringify(clean, null, 1) + "\n"], {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "labels.json";
  a.click();
};
if (DATA.cases.length) { show(0); progress(); }
else document.getElementById("room").textContent = "No cases in the given runs.";
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
