"use client";

import { FormEvent, useMemo, useState } from "react";

import { AccountMenu } from "@/components/account-menu";
import {
  CORE_ROLES,
  OPTIONAL_ROLES,
  TeamConfig,
  TeamRole,
  selectedOptionalRoleIds,
} from "@/types/team";

export function TeamBuilder({
  initialTeam,
  onEnter,
}: {
  initialTeam: TeamConfig;
  onEnter: (team: TeamConfig) => void;
}) {
  const initialOptional = initialTeam.roles.filter((role) => !role.core);
  const [name, setName] = useState(initialTeam.name);
  const [selected, setSelected] = useState<string[]>(
    selectedOptionalRoleIds(initialOptional),
  );
  const [customRoles, setCustomRoles] = useState<TeamRole[]>(
    initialOptional.filter(
      (role) => !OPTIONAL_ROLES.some((candidate) => candidate.role_id === role.role_id),
    ),
  );
  const [customName, setCustomName] = useState("");
  const [customResponsibility, setCustomResponsibility] = useState("");

  const roles = useMemo(
    () => [
      ...CORE_ROLES,
      ...OPTIONAL_ROLES.filter((role) => selected.includes(role.role_id)),
      ...customRoles,
    ].slice(0, 16),
    [customRoles, selected],
  );

  function toggleRole(roleId: string) {
    setSelected((current) => {
      if (current.includes(roleId)) return current.filter((value) => value !== roleId);
      if (current.length + customRoles.length >= 3) return current;
      return [...current, roleId];
    });
  }

  function addCustomRole(event: FormEvent) {
    event.preventDefault();
    const roleName = customName.trim();
    const responsibility = customResponsibility.trim();
    if (!roleName || !responsibility || selected.length + customRoles.length >= 3) return;
    setCustomRoles((current) => [
      ...current,
      {
        role_id: `custom-${Date.now().toString(36)}`,
        name: roleName.slice(0, 80),
        responsibility: responsibility.slice(0, 500),
        badge: roleName.slice(0, 1),
        core: false,
      },
    ]);
    setCustomName("");
    setCustomResponsibility("");
  }

  return (
    <main className="team-page">
      <header className="product-header">
        <a className="wordmark" href="#team" aria-label="CircuitFoundry">
          <span className="wordmark-symbol">CF</span>
          <span>CircuitFoundry</span>
        </a>
        <div className="header-center">构建 KiCad 硬件设计团队</div>
        <AccountMenu />
      </header>

      <section className="team-builder">
        <div className="team-intro">
          <p className="section-kicker">TEAM SETUP</p>
          <h1>先组建团队，再开始工程任务</h1>
          <p>
            任务类型固定为 KiCad 硬件设计。五个核心角色对应真实的
            CircuitFoundry LangGraph 子智能体；你还可以添加本次任务需要的专职审查角色。
          </p>
        </div>

        <div className="team-form-grid">
          <section className="setup-card setup-summary">
            <span className="card-label">团队信息</span>
            <label htmlFor="team-name">团队名称</label>
            <input
              id="team-name"
              value={name}
              maxLength={80}
              onChange={(event) => setName(event.target.value)}
            />
            <label>团队任务类型</label>
            <div className="locked-field">
              <strong>KiCad 硬件设计</strong>
              <span>需求理解 → 资料与器件 → 原理图 / PCB → 审查 → 工程交付</span>
            </div>
            <div className="team-count">
              <strong>{roles.length}</strong>
              <span>位 AI 员工已加入团队</span>
            </div>
          </section>

          <section className="setup-card role-section">
            <div className="section-heading">
              <div><span className="card-label">核心执行角色</span><h2>真实工作流成员</h2></div>
              <span className="required-pill">必需</span>
            </div>
            <div className="role-grid">
              {CORE_ROLES.map((role) => <RoleCard key={role.role_id} role={role} selected locked />)}
            </div>
          </section>

          <section className="setup-card role-section wide-card">
            <div className="section-heading">
              <div><span className="card-label">扩展专职角色</span><h2>按任务选择专业审查视角</h2></div>
              <span className="soft-note">最多 3 位扩展专家</span>
            </div>
            <div className="role-grid optional-grid">
              {OPTIONAL_ROLES.map((role) => (
                <RoleCard
                  key={role.role_id}
                  role={role}
                  selected={selected.includes(role.role_id)}
                  onClick={() => toggleRole(role.role_id)}
                />
              ))}
              {customRoles.map((role) => (
                <RoleCard
                  key={role.role_id}
                  role={role}
                  selected
                  onClick={() => setCustomRoles((items) => items.filter((item) => item.role_id !== role.role_id))}
                  removeLabel="移除"
                />
              ))}
            </div>

            <form className="custom-role-form" onSubmit={addCustomRole}>
              <input
                aria-label="自定义角色名称"
                placeholder="角色名称，例如：射频工程师"
                value={customName}
                maxLength={80}
                onChange={(event) => setCustomName(event.target.value)}
              />
              <input
                aria-label="自定义角色职责"
                placeholder="该角色负责什么"
                value={customResponsibility}
                maxLength={500}
                onChange={(event) => setCustomResponsibility(event.target.value)}
              />
              <button type="submit" disabled={!customName.trim() || !customResponsibility.trim() || selected.length + customRoles.length >= 3}>
                + 添加角色
              </button>
            </form>
          </section>
        </div>

        <footer className="team-actions">
          <div><strong>{name || "KiCad 硬件设计团队"}</strong><span>{roles.length} 位成员 · ratsnestpro-multi-agent</span></div>
          <button
            type="button"
            onClick={() => onEnter({ version: 1, name: name.trim() || "KiCad 硬件设计团队", taskType: "kicad-hardware-design", roles })}
          >
            <span className="action-label">进入团队工作区</span><span aria-hidden="true">→</span>
          </button>
        </footer>
      </section>
    </main>
  );
}

function RoleCard({
  role,
  selected,
  locked = false,
  onClick,
  removeLabel,
}: {
  role: TeamRole;
  selected: boolean;
  locked?: boolean;
  onClick?: () => void;
  removeLabel?: string;
}) {
  return (
    <button
      className={`role-card ${selected ? "selected" : ""}`}
      type="button"
      onClick={onClick}
      disabled={locked}
      aria-pressed={selected}
    >
      <span className="role-badge">{role.badge}</span>
      <span className="role-copy"><strong>{role.name}</strong><small>{role.responsibility}</small></span>
      <span className="role-check">{removeLabel ?? (locked ? "固定" : selected ? "已选择" : "添加")}</span>
    </button>
  );
}
