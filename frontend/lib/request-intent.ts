const NEW_PROJECT_RE = /(?:\bnew\s+(?:kicad\s+)?(?:project|build|board|design|task)\b|\bstart\s+(?:a\s+)?(?:new|fresh)\s+(?:kicad\s+)?(?:project|build|board|design|task)\b|\bstart\s+from\s+scratch\b|(?:\u8fd9\u662f(?:\u4e00\u4e2a|\u4e00\u9879)?|\u8bf7)?(?:\u65b0\u5efa|\u5168\u65b0\u521b\u5efa)(?:\u4e00\u4e2a|\u4e00\u5757|\u4e00\u9879)?\s*(?:KiCad\s*)?(?:\u5de5\u7a0b|\u9879\u76ee|\u8bbe\u8ba1|\u677f\u5361|\u4efb\u52a1))/i;

const CAPABILITY_PROFILE_RE = /capability[_\s-]*profile\s*[:\uff1a=]\s*["'`]?([a-z0-9][a-z0-9-]{1,63}@[0-9]+\.[0-9]+(?:\.[0-9]+)?)/i;

export function startsNewProject(message: string): boolean {
  return NEW_PROJECT_RE.test(message);
}

export function requestedCapabilityProfile(message: string): string | null {
  return CAPABILITY_PROFILE_RE.exec(message)?.[1]?.toLowerCase() ?? null;
}

export interface CapabilityProfileSnapshotRef {
  id: string;
  version: string;
  digest: string;
}

export type RunSubmissionMode = "initial" | "revision" | "explicit-new-project" | "profile-migration";

export function runSubmissionMode(
  message: string,
  selected: CapabilityProfileSnapshotRef,
  active: CapabilityProfileSnapshotRef | null | undefined,
): RunSubmissionMode {
  if (startsNewProject(message)) return "explicit-new-project";
  if (active === undefined) return "initial";
  if (
    active === null ||
    active.id !== selected.id ||
    active.version !== selected.version ||
    active.digest !== selected.digest
  ) return "profile-migration";
  return "revision";
}

export function profileForkRequestBody(
  changeRequest: string,
  profile: Pick<CapabilityProfileSnapshotRef, "id" | "version">,
  model: string | null,
  teamMembers: Array<{ roleId: string; name: string; responsibility: string }>,
): Record<string, unknown> {
  return {
    capabilityProfile: { id: profile.id, version: profile.version },
    replayMode: "THROUGH_SOURCE_REVISION",
    changeRequest,
    model,
    teamMembers,
  };
}

export function requiresNewRun(
  message: string,
  selectedProfileReference: string,
  activeRunProfileReference: string | null | undefined,
): boolean {
  return startsNewProject(message) || (
    activeRunProfileReference !== undefined &&
    activeRunProfileReference !== selectedProfileReference
  );
}
