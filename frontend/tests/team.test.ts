import assert from "node:assert/strict";
import test from "node:test";

import { OPTIONAL_ROLES, selectedOptionalRoleIds } from "../types/team.ts";

test("custom roles are not double-counted as selected built-in roles when editing", () => {
  const custom = {
    role_id: "custom-rf",
    name: "射频工程师",
    responsibility: "射频审查",
    badge: "射",
    core: false,
  };
  assert.deepEqual(selectedOptionalRoleIds([OPTIONAL_ROLES[0], custom]), [
    OPTIONAL_ROLES[0].role_id,
  ]);
});
