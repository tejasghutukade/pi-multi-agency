import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { getBrokerLaunchSpec, getTsxCliPath } from "../broker/spawn.ts";

test("broker launch falls back to npx --yes tsx when linked checkout has no local tsx", () => {
	const extensionDir = mkdtempSync(join(tmpdir(), "agency-extension-no-tsx-"));
	try {
		assert.equal(getTsxCliPath(extensionDir), null);
		const spec = getBrokerLaunchSpec(
			join(extensionDir, "broker.ts"),
			"npx",
			["--no-install", "tsx"],
			extensionDir,
			"darwin",
			join(extensionDir, "runtime"),
			"/usr/local/bin/node",
		);
		assert.deepEqual(spec, {
			kind: "direct",
			command: "npx",
			args: ["--yes", "tsx", join(extensionDir, "broker.ts")],
		});
	} finally {
		rmSync(extensionDir, { recursive: true, force: true });
	}
});
