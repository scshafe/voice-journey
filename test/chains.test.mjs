import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";

import { chainsForClient } from "../src/chains.mjs";
import { closeServers, startServers } from "../src/corpus-browser.mjs";

const REGISTRY = {
  schemaVersion: "voice-journey.chains.v1",
  chains: [
    { chainId: "iphone-voicememos", kind: "phone", label: "iPhone", controlled: false, era: { start: "2019-07-14" }, notes: "private note" },
    {
      chainId: "umik1", kind: "measurement", label: "UMIK-1", controlled: true, era: { start: null },
      deviceName: "umik", usbId: "2752:0007", sampleRate: 48000, bitDepth: 24, distanceCm: 35,
      pinnedInputVolume: 70, calFile: "local-artifacts/calibration/umik1-cal.txt", notes: "private note",
    },
  ],
};

test("chainsForClient projects only the fields a client needs", () => {
  const { chains } = chainsForClient(REGISTRY);
  assert.deepEqual(chains[1], {
    id: "umik1", kind: "measurement", label: "UMIK-1",
    device: { name: "umik", usb: "2752:0007" },
    sampleRate: 48000, bitDepth: 24, eraStart: null, pinnedInputVolume: 70,
  });
  assert.equal(chains[0].device, null);
  assert.equal(chains[0].eraStart, "2019-07-14");
  assert.equal(chains[0].pinnedInputVolume, null);
  const text = JSON.stringify(chains);
  assert.doesNotMatch(text, /private note|calFile|distanceCm|cal\.txt/u);
});

test("chainsForClient rejects a registry with the wrong schema", () => {
  assert.throws(() => chainsForClient({ schemaVersion: "nope" }), /unexpected schema/u);
});

test("the tracked registry declares the UMIK-1 for clients", async () => {
  const tracked = JSON.parse(await readFile(new URL("../manifests/chains.json", import.meta.url), "utf8"));
  const umik = chainsForClient(tracked).chains.find((chain) => chain.id === "umik1");
  assert.deepEqual(umik.device, { name: "umik", usb: "2752:0007" });
  assert.equal(umik.sampleRate, 48000);
  assert.equal(umik.bitDepth, 24);
});

test("GET /api/chains serves the registry; 503 when it is missing", async () => {
  const dir = await mkdtemp(path.join(tmpdir(), "vj-chains-"));
  const chainsPath = path.join(dir, "chains.json");
  await writeFile(chainsPath, JSON.stringify(REGISTRY));
  const base = { hosts: ["127.0.0.1"], port: 0, state: "unused-state.json", webDist: dir };
  let servers = await startServers({ rows: [], reviewQueue: [] }, { ...base, chainsPath });
  try {
    const [{ port }] = servers;
    const response = await fetch(`http://127.0.0.1:${port}/api/chains`);
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.deepEqual(body.chains.map((chain) => chain.id), ["iphone-voicememos", "umik1"]);
    assert.equal(body.chains[1].device.usb, "2752:0007");
    const post = await fetch(`http://127.0.0.1:${port}/api/chains`, { method: "POST" });
    assert.notEqual(post.status, 200);
  } finally {
    await closeServers(servers);
  }
  servers = await startServers({ rows: [], reviewQueue: [] }, { ...base, chainsPath: path.join(dir, "missing.json") });
  try {
    const [{ port }] = servers;
    const response = await fetch(`http://127.0.0.1:${port}/api/chains`);
    assert.equal(response.status, 503);
    assert.equal((await response.json()).error, "chain_registry_unavailable");
  } finally {
    await closeServers(servers);
  }
});
