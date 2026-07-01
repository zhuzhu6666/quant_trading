import fs from "node:fs";
import path from "node:path";

const root = path.resolve("dist");

function chmodRecursive(target) {
  const stat = fs.statSync(target);
  if (stat.isDirectory()) {
    fs.chmodSync(target, 0o755);
    for (const entry of fs.readdirSync(target)) {
      chmodRecursive(path.join(target, entry));
    }
    return;
  }
  fs.chmodSync(target, 0o644);
}

if (fs.existsSync(root)) {
  chmodRecursive(root);
}
