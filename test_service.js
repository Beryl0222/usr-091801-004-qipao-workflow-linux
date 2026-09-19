"use strict";

const { spawnSync } = require("node:child_process");

// 先跑领域验收测试（tests/ 下全部用例），再跑基线服务契约
const runs = [
  ["python3", ["-m", "unittest", "discover", "-s", "tests"]],
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
];

let failed = false;
for (const [cmd, args] of runs) {
  const result = spawnSync(cmd, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    failed = true;
    break;
  }
}
process.exit(failed ? 1 : 0);
