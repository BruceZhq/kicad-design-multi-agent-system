"use client";

import { useEffect, useState } from "react";

import { ChatConsole } from "@/components/chat-console";
import { ProfilePage } from "@/components/profile-page";
import { TeamBuilder } from "@/components/team-builder";
import { DEFAULT_TEAM, TeamConfig, loadTeam, saveTeam } from "@/types/team";

type ProductView = "team" | "workspace" | "profile";

function routeView(): ProductView {
  if (window.location.hash === "#workspace") return "workspace";
  if (window.location.hash === "#profile") return "profile";
  return "team";
}

export function ProductApp() {
  const [ready, setReady] = useState(false);
  const [view, setView] = useState<ProductView>("team");
  const [team, setTeam] = useState<TeamConfig>(DEFAULT_TEAM);

  useEffect(() => {
    const stored = loadTeam();
    setTeam(stored ?? DEFAULT_TEAM);
    const initialView = routeView();
    setView(initialView === "workspace" && !stored ? "team" : initialView);
    setReady(true);
    const onHashChange = () => {
      const next = routeView();
      setView(next === "workspace" && !loadTeam() ? "team" : next);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  if (!ready) return <div className="app-loading">正在准备 RatsNest 工作区…</div>;

  if (view === "profile") {
    return <ProfilePage onBack={() => { window.location.hash = loadTeam() ? "workspace" : "team"; }} />;
  }

  if (view === "team") {
    return (
      <TeamBuilder
        initialTeam={team}
        onEnter={(nextTeam) => {
          saveTeam(nextTeam);
          setTeam(nextTeam);
          window.location.hash = "workspace";
        }}
      />
    );
  }

  return <ChatConsole team={team} onEditTeam={() => { window.location.hash = "team"; }} />;
}
