import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { READER_BASE_URI, READER_RECORDS } from "./readerRecords.ts";

const BACKEND = fileURLToPath(new URL("../../backend", import.meta.url));
const SEED_SCRIPT = fileURLToPath(new URL("../../scripts/seed_e2e_records.py", import.meta.url));

/**
 * Import the Value Sets of `profiles/vocab/` and store the records the e2e tests open
 * (`readerRecords.ts`) in the e2e API's database (`LAE_DATABASE_URL`, as for the API itself).
 * Both replace what is there, so reruns are safe.
 */
export default function globalSetup(): void {
  const directory = mkdtempSync(join(tmpdir(), "lae-e2e-"));
  const input = join(directory, "records.json");
  try {
    // The editor's Value Set bundle (`GET /api/vocab/bundle`) reads the registry.
    execFileSync("uv", ["run", "--project", BACKEND, "--locked", "lae", "vocab", "import"], {
      stdio: "inherit",
    });
    writeFileSync(input, JSON.stringify(READER_RECORDS));
    execFileSync(
      "uv",
      ["run", "--project", BACKEND, "--locked", "python", SEED_SCRIPT, "--input", input],
      { stdio: "inherit", env: { ...process.env, LAE_BASE_URI: READER_BASE_URI } },
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}
