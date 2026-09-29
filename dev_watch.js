const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const projectDir = __dirname;
const watchedFiles = [
  "whatsapp_bot.js",
  "flight_parser.js",
  "trip_list.js",
  "flight_time_selection.js",
  "whatsapp_service.py",
  "flight_core.py",
  "airline-alias-database.json",
].map((file) => path.join(projectDir, file));

let child;
let restartTimer;
let stopping = false;
let restarting = false;

function startBot() {
  if (stopping) return;
  child = spawn(process.execPath, [path.join(projectDir, "whatsapp_bot.js")], {
    cwd: projectDir,
    env: process.env,
    stdio: "inherit",
  });
  child.on("exit", (code, signal) => {
    child = undefined;
    if (!stopping && !restarting) {
      console.error(`הבוט נעצר (code=${code}, signal=${signal || "none"}). ניסיון הפעלה מחדש בעוד שנייה...`);
      setTimeout(startBot, 1000);
    }
  });
}

function restartBot(fileName) {
  if (stopping || restarting || restartTimer) return;
  restartTimer = setTimeout(() => {
    restartTimer = undefined;
    restarting = true;
    console.log(`\nזוהה שינוי ב־${fileName}. מפעיל מחדש את הבוט...`);
    if (!child) {
      restarting = false;
      startBot();
      return;
    }
    child.once("exit", () => {
      restarting = false;
      startBot();
    });
    child.kill("SIGTERM");
  }, 250);
}

for (const filePath of watchedFiles) {
  fs.watch(filePath, () => restartBot(path.basename(filePath)));
}

function shutdown(signal) {
  stopping = true;
  if (restartTimer) clearTimeout(restartTimer);
  console.log(`\n${signal}: עוצר את ה־watcher...`);
  if (!child) {
    process.exit(0);
    return;
  }
  child.once("exit", () => process.exit(0));
  child.kill(signal);
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));

console.log("מצב פיתוח פעיל: שינוי בקבצי הקוד יפעיל מחדש את הבוט אוטומטית.");
startBot();
