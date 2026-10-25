// The chain registry as a capture client sees it: GET /api/chains.
// manifests/chains.json is the source of truth; this projects only what a
// client needs to pick and verify its device (never cal-file paths or notes).

export const CHAINS_SCHEMA_VERSION = "voice-journey.chains.v1";

export function chainsForClient(registry) {
  if (registry?.schemaVersion !== CHAINS_SCHEMA_VERSION) {
    throw new Error(`chain registry has unexpected schema: ${registry?.schemaVersion ?? "missing"}`);
  }
  return {
    schemaVersion: "voice-journey.chains-client.v1",
    chains: (registry.chains ?? []).map((chain) => ({
      id: chain.chainId,
      kind: chain.kind ?? null,
      label: chain.label ?? chain.chainId,
      device: chain.deviceName || chain.usbId ? { name: chain.deviceName ?? null, usb: chain.usbId ?? null } : null,
      sampleRate: chain.sampleRate ?? null,
      bitDepth: chain.bitDepth ?? null,
      eraStart: chain.era?.start ?? null,
      pinnedInputVolume: chain.pinnedInputVolume ?? null,
    })),
  };
}
