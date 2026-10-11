const PYODIDE = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
const SAMPLE_FILES = [
  "meta.json",
  "work_items.csv",
  "previous_work_items.csv",
  "notes.csv",
  "milestones.csv",
  "dependencies.csv",
];

const RUN = `
import json
from pathlib import Path
import pipeline
try:
    brief = pipeline.build(Path("/portfolio"))
    payload = {
        "ok": True,
        "result_id": brief["result_id"],
        "portfolio": brief["portfolio"],
        "html": pipeline.render_html(brief),
        "filename": pipeline.share_filename(brief),
    }
except Exception as exc:
    payload = {"ok": False, "error": str(exc)}
json.dumps(payload)
`;

const statusNode = document.querySelector("#status");
const briefFrame = document.querySelector("#brief");
const shareButton = document.querySelector("#share");
const downloadLink = document.querySelector("#download");
const installButton = document.querySelector("#install");
const installHint = document.querySelector("#install-hint");
const exportInput = document.querySelector("#export");

let engine = null;
let latest = null;
let installEvent = null;
let chosenExport = null;

function setStatus(state, text) {
  statusNode.dataset.state = state;
  statusNode.textContent = text;
}

function yieldFrame() {
  return new Promise((resolve) => {
    requestAnimationFrame(() => resolve());
  });
}

async function ensurePyodide() {
  if (typeof loadPyodide === "function") {
    return;
  }
  const response = await fetch(PYODIDE + "pyodide.js");
  if (!response.ok) {
    throw new Error("The checker could not be downloaded.");
  }
  // The repo preview inserts this file without running a script tag, so start
  // the loader from the downloaded source.
  (0, eval)(await response.text());
  if (typeof loadPyodide !== "function") {
    throw new Error("The checker did not start.");
  }
}

async function ready() {
  if (engine) {
    return engine;
  }
  engine = (async () => {
    await ensurePyodide();
    const [pyodide, pipelineSrc, extractSrc] = await Promise.all([
      loadPyodide({ indexURL: PYODIDE }),
      fetchText("engine/pipeline.py"),
      fetchText("engine/extract.py"),
    ]);
    pyodide.FS.mkdirTree("/engine");
    pyodide.FS.writeFile("/engine/pipeline.py", pipelineSrc);
    pyodide.FS.writeFile("/engine/extract.py", extractSrc);
    await pyodide.runPythonAsync(`
import sys
sys.path.insert(0, "/engine")
import pipeline
"ok"
`);
    return pyodide;
  })();
  try {
    await engine;
    if (statusNode.dataset.state === "loading") {
      setStatus("ready", "Ready. Choose a CSV, or open the sample.");
    }
  } catch (error) {
    engine = null;
    setStatus("error", "The checker did not load. " + (error && error.message ? error.message : String(error)));
    throw error;
  }
  return engine;
}

async function fetchText(path) {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(path + " returned " + response.status);
  }
  return response.text();
}

function resetPortfolio(pyodide) {
  pyodide.FS.mkdirTree("/portfolio");
  for (const name of pyodide.FS.readdir("/portfolio")) {
    if (name !== "." && name !== "..") {
      pyodide.FS.unlink("/portfolio/" + name);
    }
  }
}

async function buildPortfolio(files) {
  const pyodide = await ready();
  resetPortfolio(pyodide);
  for (const [name, text] of Object.entries(files)) {
    pyodide.FS.writeFile("/portfolio/" + name, text);
  }
  const raw = await pyodide.runPythonAsync(RUN);
  const payload = JSON.parse(raw);
  if (!payload.ok) {
    throw new Error(payload.error || "The brief could not be built.");
  }
  return payload;
}

async function loadDirectory(prefix) {
  const files = {};
  for (const name of SAMPLE_FILES) {
    files[name] = await fetchText(prefix + name);
  }
  return buildPortfolio(files);
}

function showBrief(payload) {
  latest = payload;
  setStatus("built", payload.portfolio + " · result " + payload.result_id);
  briefFrame.hidden = false;
  briefFrame.srcdoc = payload.html;
  const blob = new Blob([payload.html], { type: "text/html" });
  if (downloadLink.href.startsWith("blob:")) {
    URL.revokeObjectURL(downloadLink.href);
  }
  downloadLink.href = URL.createObjectURL(blob);
  downloadLink.download = payload.filename;
  downloadLink.hidden = false;
  shareButton.hidden = typeof navigator.share !== "function";
  briefFrame.scrollIntoView({ block: "start" });
}

function useFile(file) {
  chosenExport = file;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  exportInput.files = transfer.files;
  const nameInput = document.querySelector("#name");
  if (!nameInput.value) {
    nameInput.value = file.name.replace(/\.csv$/i, "").replace(/[-_]+/g, " ");
  }
  setStatus("ready", "Using " + file.name + ". Add the portfolio name and build the brief.");
}

async function readOptional(id, filename, files) {
  const input = document.querySelector("#" + id);
  const file = input.files && input.files[0];
  if (!file) {
    return;
  }
  files[filename] = await file.text();
}

document.querySelector("#as_of").value = localDate();

document.querySelector("#sample").addEventListener("click", async () => {
  setStatus("building", "Building the Northline sample…");
  try {
    showBrief(await loadDirectory("sample/"));
  } catch (error) {
    setStatus("error", error.message || String(error));
  }
});

document.querySelector("#brief-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const exportFile = (exportInput.files && exportInput.files[0]) || chosenExport;
  if (!exportFile) {
    setStatus("error", "Choose this week's CSV.");
    return;
  }
  const name = document.querySelector("#name").value.trim();
  const asOf = document.querySelector("#as_of").value;
  if (!name || !asOf) {
    setStatus("error", "Add a portfolio name and a report date.");
    return;
  }
  setStatus("building", "Building the brief…");
  await yieldFrame();
  try {
    const files = { "work_items.csv": await exportFile.text() };
    await readOptional("previous", "previous_work_items.csv", files);
    await readOptional("columns", "columns.json", files);
    await readOptional("notes", "notes.csv", files);
    await readOptional("milestones", "milestones.csv", files);
    await readOptional("dependencies", "dependencies.csv", files);
    const meta = {
      portfolio: name,
      as_of: asOf,
      audience: "Delivery director",
      description: "Brief built from a single export file.",
      synthetic: false,
    };
    const profile = document.querySelector("#profile").value;
    if (profile) {
      meta.profile = profile;
    }
    files["meta.json"] = JSON.stringify(meta);
    showBrief(await buildPortfolio(files));
  } catch (error) {
    setStatus("error", error.message || String(error));
  }
});

shareButton.addEventListener("click", async () => {
  if (!latest || typeof navigator.share !== "function") {
    return;
  }
  const file = new File([latest.html], latest.filename, { type: "text/html" });
  try {
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], title: latest.portfolio });
      return;
    }
    await navigator.share({ title: latest.portfolio, text: latest.portfolio + " delivery brief" });
  } catch (error) {
    if (error && error.name === "AbortError") {
      return;
    }
    setStatus("built", latest.portfolio + " · result " + latest.result_id);
  }
});

const installBlurbs = {
  apps: "The page is the app. The export stays in this browser.",
  oneliner: "Works on macOS, Linux, and Windows. The one-liner puts the command on this machine and uses Python when Python is already there. Run the same command again to update.",
  npm: "For a machine that already has Node and Python. Then run delivery-signal.",
  hackable: "The checkout is the product. ./run on macOS and Linux, and run on Windows, opens the configuration page.",
};

function syncInstall() {
  const platformButton = document.querySelector(".platforms button[aria-selected='true']");
  const methodButton = document.querySelector(".methods button[aria-selected='true']");
  const platform = platformButton ? platformButton.dataset.platform : "unix";
  const method = methodButton ? methodButton.dataset.method : "oneliner";
  document.querySelectorAll(".install-panel").forEach((panel) => {
    const methodOk = panel.dataset.methodPanel === method;
    const platformOk = !panel.dataset.platformPanel || panel.dataset.platformPanel === platform;
    panel.hidden = !(methodOk && platformOk);
  });
  const blurb = document.querySelector("#install-blurb");
  if (blurb) {
    blurb.textContent = installBlurbs[method] || "";
  }
}

document.querySelectorAll(".platforms button, .methods button").forEach((button) => {
  button.addEventListener("click", () => {
    const group = button.closest("[role='tablist']");
    group.querySelectorAll("button").forEach((item) => {
      item.setAttribute("aria-selected", item === button ? "true" : "false");
    });
    syncInstall();
  });
});

document.querySelectorAll("button.copy").forEach((button) => {
  button.addEventListener("click", async () => {
    const text = button.dataset.copy || "";
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch (error) {
      button.textContent = "Copy";
    }
    setTimeout(() => {
      button.textContent = "Copy";
    }, 1600);
  });
});

if (/Windows/i.test(navigator.userAgent)) {
  const windows = document.querySelector(".platforms button[data-platform='windows']");
  if (windows) {
    windows.click();
  }
} else {
  syncInstall();
}

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installEvent = event;
  installButton.hidden = false;
});

installButton.addEventListener("click", async () => {
  if (!installEvent) {
    return;
  }
  installEvent.prompt();
  await installEvent.userChoice;
  installEvent = null;
  installButton.hidden = true;
});

if (location.hostname === "htmlpreview.github.io") {
  installHint.textContent = "This preview builds the brief in the browser. Home Screen install belongs to the page once it has its own address.";
} else if (window.matchMedia("(display-mode: standalone)").matches) {
  installHint.textContent = "This device is already running the installed page.";
} else if (/iphone|ipad/i.test(navigator.userAgent)) {
  installHint.textContent = "On an iPhone or iPad, use Share, then Add to Home Screen.";
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}

if ("launchQueue" in window) {
  window.launchQueue.setConsumer(async (params) => {
    if (!params.files || !params.files.length) {
      return;
    }
    useFile(await params.files[0].getFile());
  });
}

const shared = new URLSearchParams(location.search).get("shared");
if (shared) {
  caches.open("delivery-signal-share").then(async (cache) => {
    const response = await cache.match(new URL("shared-export", location.href));
    if (!response) {
      return;
    }
    const text = await response.text();
    const filename = response.headers.get("X-File-Name") || "export.csv";
    useFile(new File([text], filename, { type: "text/csv" }));
  }).catch(() => {});
}

function localDate() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

window.DeliverySignal = { ready, buildPortfolio, loadDirectory };

ready().catch(() => {});
