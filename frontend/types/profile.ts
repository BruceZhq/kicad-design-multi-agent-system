export const MAX_AVATAR_BYTES = 2 * 1024 * 1024;
export const AVATAR_MEDIA_TYPES = ["image/jpeg", "image/png", "image/webp"] as const;

export interface UserProfile {
  displayName: string;
  username: string;
  email: string | null;
  jobTitle: string;
  bio: string;
  locale: string;
  timeZone: string;
  hasAvatar: boolean;
  version: number;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface EditableProfile {
  displayName: string;
  jobTitle: string;
  bio: string;
  locale: string;
  timeZone: string;
}

export interface ProfileUpdate extends EditableProfile {
  version: number;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function timestamp(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && Number.isFinite(Date.parse(value)));
}

export function parseUserProfile(value: unknown): UserProfile | null {
  const item = record(value);
  if (
    typeof item.displayName !== "string" || item.displayName.trim().length < 1 || item.displayName.length > 120 ||
    typeof item.username !== "string" || item.username.length < 1 || item.username.length > 320 ||
    (item.email !== null && (typeof item.email !== "string" || item.email.length > 320)) ||
    typeof item.jobTitle !== "string" || item.jobTitle.length > 120 ||
    typeof item.bio !== "string" || item.bio.length > 1000 ||
    typeof item.locale !== "string" || item.locale.length < 1 || item.locale.length > 35 ||
    typeof item.timeZone !== "string" || item.timeZone.length < 1 || item.timeZone.length > 64 ||
    typeof item.hasAvatar !== "boolean" ||
    !Number.isSafeInteger(item.version) || Number(item.version) < 0 ||
    !timestamp(item.createdAt) || !timestamp(item.updatedAt)
  ) return null;

  return {
    displayName: item.displayName,
    username: item.username,
    email: item.email,
    jobTitle: item.jobTitle,
    bio: item.bio,
    locale: item.locale,
    timeZone: item.timeZone,
    hasAvatar: item.hasAvatar,
    version: Number(item.version),
    createdAt: item.createdAt,
    updatedAt: item.updatedAt,
  };
}

export function editableProfile(profile: UserProfile): EditableProfile {
  return {
    displayName: profile.displayName,
    jobTitle: profile.jobTitle,
    bio: profile.bio,
    locale: profile.locale,
    timeZone: profile.timeZone,
  };
}

export function parseProfileUpdate(value: unknown): ProfileUpdate | null {
  const item = record(value);
  if (
    !Number.isSafeInteger(item.version) || Number(item.version) < 0 ||
    typeof item.displayName !== "string" || item.displayName.trim().length < 1 || item.displayName.length > 120 ||
    typeof item.jobTitle !== "string" || item.jobTitle.length > 120 ||
    typeof item.bio !== "string" || item.bio.length > 1000 ||
    typeof item.locale !== "string" || item.locale.trim().length < 1 || item.locale.length > 35 ||
    typeof item.timeZone !== "string" || item.timeZone.trim().length < 1 || item.timeZone.length > 64
  ) return null;
  return {
    version: Number(item.version),
    displayName: item.displayName,
    jobTitle: item.jobTitle,
    bio: item.bio,
    locale: item.locale,
    timeZone: item.timeZone,
  };
}

export function profileInitials(value: string): string {
  const parts = value.trim().split(/[\s._-]+/).filter(Boolean);
  const result = parts.length > 1
    ? `${parts[0][0]}${parts[parts.length - 1][0]}`
    : value.trim().slice(0, 2);
  return result.toLocaleUpperCase() || "RN";
}

export function isAvatarFile(file: File): boolean {
  return AVATAR_MEDIA_TYPES.some((mediaType) => mediaType === file.type) &&
    file.size > 0 && file.size <= MAX_AVATAR_BYTES;
}
