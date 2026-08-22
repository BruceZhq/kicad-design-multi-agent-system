"use client";

import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";

import { AccountMenu } from "@/components/account-menu";
import {
  EditableProfile,
  MAX_AVATAR_BYTES,
  UserProfile,
  editableProfile,
  isAvatarFile,
  parseUserProfile,
  profileInitials,
} from "@/types/profile";

class ProfileRequestError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

function problemMessage(value: unknown, fallback: string): string {
  if (!value || typeof value !== "object") return fallback;
  const detail = (value as { detail?: unknown }).detail;
  return typeof detail === "string" ? detail : fallback;
}

async function responseProfile(response: Response): Promise<UserProfile> {
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new ProfileRequestError(response.status, problemMessage(value, "资料请求失败。"));
  const profile = parseUserProfile(value);
  if (!profile) throw new ProfileRequestError(502, "资料服务返回了无法识别的数据。");
  return profile;
}

function uploadAvatar(
  file: File,
  version: number,
  onProgress: (value: number) => void,
): Promise<UserProfile> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", "/api/profile/avatar");
    request.responseType = "json";
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    request.onerror = () => reject(new ProfileRequestError(0, "头像上传连接中断。"));
    request.onload = () => {
      const value: unknown = request.response;
      if (request.status < 200 || request.status >= 300) {
        reject(new ProfileRequestError(request.status, problemMessage(value, "头像上传失败。")));
        return;
      }
      const profile = parseUserProfile(value);
      if (!profile) {
        reject(new ProfileRequestError(502, "头像服务返回了无法识别的数据。"));
        return;
      }
      onProgress(100);
      resolve(profile);
    };
    const body = new FormData();
    body.set("version", String(version));
    body.set("file", file, file.name);
    request.send(body);
  });
}

export function ProfilePage({ onBack }: { onBack: () => void }) {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [draft, setDraft] = useState<EditableProfile | null>(null);
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [avatarPreview, setAvatarPreview] = useState<string | null>(null);
  const [avatarError, setAvatarError] = useState(false);
  const [status, setStatus] = useState<"loading" | "ready" | "saving" | "error">("loading");
  const [uploadProgress, setUploadProgress] = useState(0);
  const [notice, setNotice] = useState("");
  const [authenticationRequired, setAuthenticationRequired] = useState(false);

  async function loadProfile(signal?: AbortSignal): Promise<UserProfile> {
    const response = await fetch("/api/profile", { cache: "no-store", signal });
    return responseProfile(response);
  }

  useEffect(() => {
    const controller = new AbortController();
    void loadProfile(controller.signal).then((loaded) => {
      setProfile(loaded);
      setDraft(editableProfile(loaded));
      setStatus("ready");
    }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      setAuthenticationRequired(error instanceof ProfileRequestError && error.status === 401);
      setNotice(error instanceof Error ? error.message : "无法读取用户资料。");
      setStatus("error");
    });
    return () => controller.abort();
  }, []);

  useEffect(() => () => {
    if (avatarPreview) URL.revokeObjectURL(avatarPreview);
  }, [avatarPreview]);

  const changed = useMemo(
    () => profile !== null && draft !== null &&
      (Object.keys(draft) as Array<keyof EditableProfile>).some((key) => draft[key] !== editableProfile(profile)[key]),
    [draft, profile],
  );

  function chooseAvatar(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] ?? null;
    event.target.value = "";
    if (!file) return;
    if (!isAvatarFile(file)) {
      setNotice(`头像必须是 JPEG、PNG 或 WebP，且不超过 ${MAX_AVATAR_BYTES / 1024 / 1024} MiB。`);
      return;
    }
    setAvatarFile(file);
    setAvatarPreview(URL.createObjectURL(file));
    setAvatarError(false);
    setNotice("");
  }

  async function refreshAfterConflict() {
    try {
      const latest = await loadProfile();
      setProfile(latest);
      setDraft(editableProfile(latest));
      setAvatarFile(null);
      setAvatarPreview(null);
      setAvatarError(false);
      setNotice("资料已被其他会话更新，已载入最新版本，请检查后再次保存。");
      setStatus("ready");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "重新载入资料失败。");
      setStatus("error");
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!profile || !draft || status === "saving") return;
    if (!draft.displayName.trim()) {
      setNotice("显示名称不能为空。");
      return;
    }
    if (!changed && !avatarFile) {
      setNotice("没有需要保存的更改。");
      return;
    }

    setStatus("saving");
    setNotice("");
    setUploadProgress(0);
    try {
      let saved = profile;
      if (changed) {
        saved = await responseProfile(await fetch("/api/profile", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ version: profile.version, ...draft }),
        }));
      }
      if (avatarFile) saved = await uploadAvatar(avatarFile, saved.version, setUploadProgress);
      setProfile(saved);
      setDraft(editableProfile(saved));
      setAvatarFile(null);
      setAvatarPreview(null);
      setAvatarError(false);
      setNotice("个人资料已保存。");
      setStatus("ready");
      window.dispatchEvent(new CustomEvent("ratsnest:profile-updated"));
    } catch (error) {
      if (error instanceof ProfileRequestError && error.status === 409) {
        await refreshAfterConflict();
        return;
      }
      setNotice(error instanceof Error ? error.message : "保存用户资料失败。");
      setStatus("ready");
    }
  }

  if (status === "loading") return <div className="app-loading">正在读取个人资料…</div>;
  if (!profile || !draft) {
    return (
      <main className="profile-page">
        <header className="product-header">
          <a className="wordmark" href="#team"><span className="wordmark-symbol">KDMAS</span><span>KiCad Design Multi-Agent System</span></a>
          <div className="header-center">个人资料</div>
          <AccountMenu />
        </header>
        <section className="profile-unavailable">
          <h1>暂时无法打开个人资料</h1>
          <p role="alert">{notice}</p>
          <div>
            <button type="button" onClick={onBack}>返回工作区</button>
            {authenticationRequired && <a href="/oauth2/start?rd=%2F%23profile">重新登录</a>}
          </div>
        </section>
      </main>
    );
  }

  const avatarSource = avatarPreview ?? (profile.hasAvatar ? `/api/profile/avatar?v=${profile.version}` : null);
  return (
    <main className="profile-page">
      <header className="product-header">
        <a className="wordmark" href="#team"><span className="wordmark-symbol">KDMAS</span><span>KiCad Design Multi-Agent System</span></a>
        <div className="header-center">账号与个人资料</div>
        <AccountMenu />
      </header>

      <section className="profile-shell">
        <button className="profile-back" type="button" onClick={onBack}>← 返回团队</button>
        <div className="profile-heading">
          <p className="section-kicker">YOUR PROFILE</p>
          <h1>管理你的工程身份</h1>
          <p>资料用于团队协作界面；账号标识由企业身份服务管理，不能在这里修改。</p>
        </div>

        <form className="profile-card" onSubmit={save}>
          <aside className="profile-avatar-panel">
            <div className="profile-avatar-large" aria-label="当前头像">
              {avatarSource && !avatarError
                ? <img src={avatarSource} alt="" onError={() => setAvatarError(true)} />
                : <span>{profileInitials(draft.displayName)}</span>}
            </div>
            <strong>{draft.displayName}</strong>
            <small>{draft.jobTitle || "KiCad 硬件工程团队成员"}</small>
            <label className="avatar-picker">
              选择新头像
              <input type="file" accept="image/jpeg,image/png,image/webp" onChange={chooseAvatar} />
            </label>
            <p>JPEG、PNG 或 WebP，最大 2 MiB</p>
            {avatarFile && <button className="avatar-reset" type="button" onClick={() => {
              setAvatarFile(null);
              setAvatarPreview(null);
              setAvatarError(false);
            }}>取消本次头像更改</button>}
          </aside>

          <div className="profile-fields">
            <div className="profile-field-row">
              <label><span>显示名称</span><input required maxLength={120} value={draft.displayName} onChange={(event) => setDraft({ ...draft, displayName: event.target.value })} /></label>
              <label><span>职位 / 专业方向</span><input maxLength={120} value={draft.jobTitle} placeholder="例如：硬件系统工程师" onChange={(event) => setDraft({ ...draft, jobTitle: event.target.value })} /></label>
            </div>
            <label><span>个人简介</span><textarea maxLength={1000} rows={5} value={draft.bio} placeholder="介绍你的硬件设计经验、关注领域或团队职责" onChange={(event) => setDraft({ ...draft, bio: event.target.value })} /><small>{draft.bio.length} / 1000</small></label>
            <div className="profile-field-row">
              <label><span>界面语言</span><input maxLength={35} list="profile-locales" value={draft.locale} onChange={(event) => setDraft({ ...draft, locale: event.target.value })} /><datalist id="profile-locales"><option value="zh-CN" /><option value="en-US" /></datalist></label>
              <label><span>时区</span><input maxLength={64} list="profile-time-zones" value={draft.timeZone} onChange={(event) => setDraft({ ...draft, timeZone: event.target.value })} /><datalist id="profile-time-zones"><option value="Asia/Shanghai" /><option value="Asia/Tokyo" /><option value="UTC" /><option value="Europe/London" /><option value="America/New_York" /></datalist></label>
            </div>

            <div className="profile-readonly">
              <div><span>企业用户名</span><strong>{profile.username}</strong></div>
              <div><span>企业邮箱</span><strong>{profile.email ?? "身份提供商未提供"}</strong></div>
              <small>
                这两项来自 OIDC 身份令牌，请在
                <a href="/api/account-center" target="_blank" rel="noreferrer">企业账号中心</a>
                修改。
              </small>
            </div>

            {notice && <p className={notice.includes("已保存") ? "profile-notice success" : "profile-notice"} role="status">{notice}</p>}
            {status === "saving" && avatarFile && (
              <div className="avatar-progress" aria-label={`头像上传 ${uploadProgress}%`}><i style={{ width: `${uploadProgress}%` }} /><span>{uploadProgress}%</span></div>
            )}
            <div className="profile-actions">
              <span>版本 {profile.version} · 乐观锁保护</span>
              <button type="submit" disabled={status === "saving" || (!changed && !avatarFile)}>{status === "saving" ? "保存中…" : "保存资料"}</button>
            </div>
          </div>
        </form>
      </section>
    </main>
  );
}
