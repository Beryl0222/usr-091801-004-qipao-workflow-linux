"use strict";

const { spawnSync } = require("node:child_process");

// 先验证基础服务契约，再运行领域与验收测试（tests/）
const runs = [
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
  ["python3", ["-m", "unittest", "discover", "-s", "tests", "-v"]],
];

for (const [cmd, args] of runs) {
  const result = spawnSync(cmd, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}
