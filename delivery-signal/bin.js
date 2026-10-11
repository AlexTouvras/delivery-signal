#!/usr/bin/env node
"use strict";

const { spawn } = require("child_process");
const path = require("path");

const script = path.join(__dirname, "pipeline.py");
const page = "https://htmlpreview.github.io/?https://github.com/AlexTouvras/delivery-signal/blob/main/docs/index.html";
const extra = process.argv.slice(2);
const attempts = process.platform === "win32"
  ? [["py", ["-3", script].concat(extra)], ["python", [script].concat(extra)], ["python3", [script].concat(extra)]]
  : [["python3", [script].concat(extra)], ["python", [script].concat(extra)], ["py", ["-3", script].concat(extra)]];

function run(command, args) {
  return new Promise((resolve) => {
    let settled = false;
    const child = spawn(command, args, { stdio: "inherit" });
    child.on("error", () => {
      if (!settled) {
        settled = true;
        resolve(null);
      }
    });
    child.on("exit", (code) => {
      if (!settled) {
        settled = true;
        resolve(code === null ? 1 : code);
      }
    });
  });
}

(async () => {
  for (const [command, args] of attempts) {
    const code = await run(command, args);
    if (code !== null) {
      process.exit(code);
    }
  }
  console.error("Python is not on this machine. Open the page and choose the CSV:");
  console.error(page);
  process.exit(1);
})();
