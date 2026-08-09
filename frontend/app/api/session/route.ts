import {
  controlPlaneFetch,
  forwardJson,
  isUuid,
  jsonError,
  problemResponse,
  readObject,
} from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface Organization {
  tenantId: string;
  name: string;
}

interface Project {
  tenantId: string;
  projectId: string;
  name: string;
}

interface WorkspaceContext {
  organization: Organization;
  project: Project;
  organizations: Organization[];
  projects: Project[];
}

function organizations(value: unknown): Organization[] | null {
  if (!Array.isArray(value)) return null;
  const result = value.filter((item): item is Organization => {
    if (!item || typeof item !== "object") return false;
    const candidate = item as Partial<Organization>;
    return isUuid(candidate.tenantId) && typeof candidate.name === "string";
  });
  return result.length === value.length ? result : null;
}

function projects(value: unknown): Project[] | null {
  if (!Array.isArray(value)) return null;
  const result = value.filter((item): item is Project => {
    if (!item || typeof item !== "object") return false;
    const candidate = item as Partial<Project>;
    return (
      isUuid(candidate.tenantId) &&
      isUuid(candidate.projectId) &&
      typeof candidate.name === "string"
    );
  });
  return result.length === value.length ? result : null;
}

async function listOrganizations(request: Request): Promise<Organization[] | Response> {
  const upstream = await controlPlaneFetch(request, "/api/v1/organizations", {
    signal: request.signal,
  });
  if (!upstream.ok) return forwardJson(upstream);
  const parsed = organizations(await upstream.json().catch(() => null));
  return parsed ?? problemResponse(
    request,
    "CONTROL_PLANE_INVALID_RESPONSE",
    502,
    "The control plane returned an invalid organization list.",
  );
}

async function listProjects(
  request: Request,
  organizationId: string,
): Promise<Project[] | Response> {
  const upstream = await controlPlaneFetch(
    request,
    "/api/v1/projects",
    { signal: request.signal },
    organizationId,
  );
  if (!upstream.ok) return forwardJson(upstream);
  const parsed = projects(await upstream.json().catch(() => null));
  return parsed ?? problemResponse(
    request,
    "CONTROL_PLANE_INVALID_RESPONSE",
    502,
    "The control plane returned an invalid project list.",
  );
}

function choose<T>(items: T[], selectedId: string | null, id: (item: T) => string): T | null {
  if (!selectedId) return items[0] ?? null;
  return items.find((item) => id(item) === selectedId) ?? null;
}

async function context(
  request: Request,
  selectedOrganizationId: string | null,
  selectedProjectId: string | null,
): Promise<WorkspaceContext | Response> {
  const availableOrganizations = await listOrganizations(request);
  if (availableOrganizations instanceof Response) return availableOrganizations;
  if (availableOrganizations.length === 0) {
    return problemResponse(
      request,
      "WORKSPACE_SETUP_REQUIRED",
      409,
      "No organization is available. Create a workspace explicitly to continue.",
      "Workspace setup required",
      { setup: { organizationMissing: true, projectMissing: true } },
    );
  }

  const organization = choose(
    availableOrganizations,
    selectedOrganizationId,
    (item) => item.tenantId,
  );
  if (!organization) {
    return problemResponse(
      request,
      "WORKSPACE_SELECTION_INVALID",
      409,
      "The selected organization is not available to this identity.",
    );
  }

  const availableProjects = await listProjects(request, organization.tenantId);
  if (availableProjects instanceof Response) return availableProjects;
  if (availableProjects.length === 0) {
    return problemResponse(
      request,
      "WORKSPACE_SETUP_REQUIRED",
      409,
      "The selected organization has no project. Create a workspace explicitly to continue.",
      "Workspace setup required",
      {
        organization,
        setup: { organizationMissing: false, projectMissing: true },
      },
    );
  }

  const project = choose(availableProjects, selectedProjectId, (item) => item.projectId);
  if (!project) {
    return problemResponse(
      request,
      "WORKSPACE_SELECTION_INVALID",
      409,
      "The selected project is not available in this organization.",
    );
  }
  return {
    organization,
    project,
    organizations: availableOrganizations,
    projects: availableProjects,
  };
}

function selection(value: string | null): string | null | false {
  if (value === null || value === "") return null;
  return isUuid(value) ? value : false;
}

export async function GET(request: Request): Promise<Response> {
  try {
    const url = new URL(request.url);
    const organizationId = selection(url.searchParams.get("organization_id"));
    const projectId = selection(url.searchParams.get("project_id"));
    if (organizationId === false || projectId === false) {
      return jsonError(request, "organization_id and project_id must be UUIDs.");
    }
    const result = await context(request, organizationId, projectId);
    return result instanceof Response
      ? result
      : Response.json(result, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The control plane is unavailable.",
    );
  }
}

export async function POST(request: Request): Promise<Response> {
  const input = await readObject(request);
  if (!input) return jsonError(request, "A JSON request body is required.");
  const workspaceName = typeof input.workspace_name === "string" ? input.workspace_name.trim() : "";
  if (!workspaceName || workspaceName.length > 200) {
    return jsonError(request, "workspace_name must contain between 1 and 200 characters.");
  }
  const requestedOrganizationId = selection(
    typeof input.organization_id === "string" ? input.organization_id : null,
  );
  const requestedProjectId = selection(
    typeof input.project_id === "string" ? input.project_id : null,
  );
  if (requestedOrganizationId === false || requestedProjectId === false) {
    return jsonError(request, "organization_id and project_id must be UUIDs.");
  }
  if (requestedProjectId && !requestedOrganizationId) {
    return jsonError(request, "organization_id is required when project_id is selected.");
  }

  try {
    let availableOrganizations = await listOrganizations(request);
    if (availableOrganizations instanceof Response) return availableOrganizations;
    let created = false;
    let organization = choose(
      availableOrganizations,
      requestedOrganizationId,
      (item) => item.tenantId,
    );

    if (requestedOrganizationId && !organization) {
      return problemResponse(
        request,
        "WORKSPACE_SELECTION_INVALID",
        409,
        "The selected organization is not available to this identity.",
      );
    }
    if (!organization) {
      const upstream = await controlPlaneFetch(request, "/api/v1/organizations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: workspaceName }),
        signal: request.signal,
      });
      if (!upstream.ok) return forwardJson(upstream);
      const candidate = await upstream.json().catch(() => null);
      const parsed = organizations([candidate]);
      if (!parsed) {
        return problemResponse(
          request,
          "CONTROL_PLANE_INVALID_RESPONSE",
          502,
          "The control plane returned an invalid organization.",
        );
      }
      organization = parsed[0];
      availableOrganizations = [organization];
      created = true;
    }

    let availableProjects = await listProjects(request, organization.tenantId);
    if (availableProjects instanceof Response) return availableProjects;
    let project = choose(availableProjects, requestedProjectId, (item) => item.projectId);
    if (requestedProjectId && !project) {
      return problemResponse(
        request,
        "WORKSPACE_SELECTION_INVALID",
        409,
        "The selected project is not available in this organization.",
      );
    }
    if (!project) {
      const upstream = await controlPlaneFetch(
        request,
        "/api/v1/projects",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: workspaceName }),
          signal: request.signal,
        },
        organization.tenantId,
      );
      if (!upstream.ok) return forwardJson(upstream);
      const candidate = await upstream.json().catch(() => null);
      const parsed = projects([candidate]);
      if (!parsed) {
        return problemResponse(
          request,
          "CONTROL_PLANE_INVALID_RESPONSE",
          502,
          "The control plane returned an invalid project.",
        );
      }
      project = parsed[0];
      availableProjects = [project];
      created = true;
    }

    return Response.json(
      { organization, project, organizations: availableOrganizations, projects: availableProjects },
      { status: created ? 201 : 200, headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The control plane is unavailable.",
    );
  }
}
