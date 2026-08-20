export interface TeamRole {
  role_id: string;
  name: string;
  responsibility: string;
  badge: string;
  core: boolean;
}

export interface TeamConfig {
  version: 1;
  name: string;
  taskType: "kicad-hardware-design";
  roles: TeamRole[];
}

export const TEAM_STORAGE_KEY = "ratsnest.team.v1";

export const CORE_ROLES: TeamRole[] = [
  {
    role_id: "supervisor-ratsnestpro",
    name: "Supervisor",
    responsibility: "识别意图、拆分任务、调度角色并汇总交付",
    badge: "组",
    core: true,
  },
  {
    role_id: "sub-agent-ratsnest-architect",
    name: "Architect",
    responsibility: "检索资料、建立设计依据并定义系统架构",
    badge: "构",
    core: true,
  },
  {
    role_id: "sub-agent-ratsnest-parts-specialist",
    name: "Parts Specialist",
    responsibility: "验证器件、封装、可采购性与替代关系",
    badge: "器",
    core: true,
  },
  {
    role_id: "sub-agent-ratsnest-hardware-engineer",
    name: "Hardware Engineer",
    responsibility: "通过 Temporal 执行原理图、PCB、布线和制造输出",
    badge: "板",
    core: true,
  },
  {
    role_id: "sub-agent-ratsnest-reviewer",
    name: "Reviewer",
    responsibility: "独立执行 ERC、DRC、连接性、DFM 与风险审查",
    badge: "审",
    core: true,
  },
];

export const OPTIONAL_ROLES: TeamRole[] = [
  {
    role_id: "power-integrity-specialist",
    name: "电源完整性专家",
    responsibility: "审查电源树、保护、热设计、纹波与去耦策略",
    badge: "电",
    core: false,
  },
  {
    role_id: "signal-integrity-specialist",
    name: "信号完整性专家",
    responsibility: "审查高速接口、阻抗、参考平面和串扰约束",
    badge: "信",
    core: false,
  },
  {
    role_id: "emc-esd-specialist",
    name: "EMC / ESD 专家",
    responsibility: "审查接口保护、回流路径、共模干扰和屏蔽策略",
    badge: "护",
    core: false,
  },
  {
    role_id: "manufacturing-specialist",
    name: "制造工程专家",
    responsibility: "审查装配、可测试性、工艺边界和交付文件完整性",
    badge: "造",
    core: false,
  },
  {
    role_id: "firmware-interface-specialist",
    name: "固件接口专家",
    responsibility: "审查启动、调试、引脚复用、外设冲突和可编程性",
    badge: "软",
    core: false,
  },
];

export const DEFAULT_TEAM: TeamConfig = {
  version: 1,
  name: "KiCad 硬件设计团队",
  taskType: "kicad-hardware-design",
  roles: [...CORE_ROLES],
};

export function selectedOptionalRoleIds(roles: TeamRole[]): string[] {
  const builtInIds = new Set(OPTIONAL_ROLES.map((role) => role.role_id));
  return roles
    .filter((role) => !role.core && builtInIds.has(role.role_id))
    .map((role) => role.role_id);
}

function isRole(value: unknown): value is TeamRole {
  if (!value || typeof value !== "object") return false;
  const role = value as Partial<TeamRole>;
  return (
    typeof role.role_id === "string" &&
    /^[a-z0-9][a-z0-9-]{1,63}$/.test(role.role_id) &&
    typeof role.name === "string" &&
    role.name.length > 0 &&
    role.name.length <= 80 &&
    typeof role.responsibility === "string" &&
    role.responsibility.length > 0 &&
    role.responsibility.length <= 500 &&
    typeof role.badge === "string" &&
    typeof role.core === "boolean"
  );
}

export function loadTeam(): TeamConfig | null {
  try {
    const raw = localStorage.getItem(TEAM_STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<TeamConfig>;
    if (
      value.version !== 1 ||
      value.taskType !== "kicad-hardware-design" ||
      typeof value.name !== "string" ||
      !Array.isArray(value.roles)
    ) {
      return null;
    }
    const validRoles = value.roles.filter(isRole);
    if (!CORE_ROLES.every((core) => validRoles.some((role) => role.role_id === core.role_id))) {
      return null;
    }
    const roles = [
      ...CORE_ROLES,
      ...validRoles.filter((role) => !role.core).slice(0, 3),
    ];
    return { version: 1, taskType: "kicad-hardware-design", name: value.name, roles };
  } catch {
    return null;
  }
}

export function saveTeam(team: TeamConfig): void {
  localStorage.setItem(TEAM_STORAGE_KEY, JSON.stringify(team));
}
