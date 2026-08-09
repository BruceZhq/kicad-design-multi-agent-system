"use client";

import { useEffect, useState } from "react";

import { parseUserProfile, profileInitials } from "@/types/profile";

interface Account {
  displayName: string;
  username: string;
  email: string | null;
  hasAvatar: boolean;
  version: number;
}

type AccountState =
  | { status: "loading" | "anonymous" | "error" }
  | { status: "ready"; account: Account };

function parseAccount(value: unknown): Account | null {
  if (!value || typeof value !== "object") return null;
  const account = value as Partial<Account>;
  if (
    typeof account.displayName !== "string" ||
    typeof account.username !== "string" ||
    (account.email !== null && typeof account.email !== "string")
  ) return null;
  return { ...account as Omit<Account, "hasAvatar" | "version">, hasAvatar: false, version: 0 };
}

export function AccountMenu() {
  const [state, setState] = useState<AccountState>({ status: "loading" });
  const [loginHref, setLoginHref] = useState("/oauth2/start?rd=%2F");
  const [switchHref, setSwitchHref] = useState(
    "/oauth2/sign_out?rd=%2Foauth2%2Fstart%3Frd%3D%252F",
  );
  const [avatarFailed, setAvatarFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const returnPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const startPath = `/oauth2/start?rd=${encodeURIComponent(returnPath)}`;
    setLoginHref(startPath);
    setSwitchHref(`/oauth2/sign_out?rd=${encodeURIComponent(startPath)}`);
    const load = async () => {
      try {
        const profileResponse = await fetch("/api/profile", {
          cache: "no-store",
          signal: controller.signal,
        });
        const profile = parseUserProfile(await profileResponse.json().catch(() => null));
        if (profileResponse.ok && profile) {
          setAvatarFailed(false);
          setState({ status: "ready", account: profile });
          return;
        }

        const accountResponse = await fetch("/api/account", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (accountResponse.status === 401) {
          setState({ status: "anonymous" });
          return;
        }
        const account = parseAccount(await accountResponse.json().catch(() => null));
        setState(accountResponse.ok && account ? { status: "ready", account } : { status: "error" });
      } catch {
        if (!controller.signal.aborted) setState({ status: "error" });
      }
    };
    void load();
    const reload = () => { void load(); };
    window.addEventListener("ratsnest:profile-updated", reload);
    return () => {
      controller.abort();
      window.removeEventListener("ratsnest:profile-updated", reload);
    };
  }, []);

  if (state.status === "anonymous") {
    return <a className="protected-badge account-login" href={loginHref}><i /> 登录</a>;
  }
  if (state.status !== "ready") {
    return (
      <span className="protected-badge" title={state.status === "error" ? "账号信息暂不可用" : undefined}>
        <i /> {state.status === "error" ? "已保护" : "读取账号…"}
      </span>
    );
  }

  const { account } = state;
  return (
    <details className="account-menu">
      <summary aria-label={`账号：${account.displayName}`}>
        <span className="account-avatar" aria-hidden="true">
          {account.hasAvatar && !avatarFailed
            ? <img src={`/api/profile/avatar?v=${account.version}`} alt="" onError={() => setAvatarFailed(true)} />
            : profileInitials(account.displayName)}
        </span>
        <span className="account-copy">
          <strong>{account.displayName}</strong>
          <small>企业账号</small>
        </span>
        <span className="account-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="account-popover">
        <div>
          <strong>{account.displayName}</strong>
          <small>{account.email ?? account.username}</small>
        </div>
        <a href="#profile">个人资料</a>
        <a href="/oauth2/sign_out?rd=%2F">退出登录</a>
        <a href={switchHref}>切换账号</a>
      </div>
    </details>
  );
}
