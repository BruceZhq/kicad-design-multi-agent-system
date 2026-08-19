package dev.ratsnest.tenant;

import dev.ratsnest.approval.RunApprovalService;
import dev.ratsnest.auth.UserAccount;
import dev.ratsnest.auth.UserAccountRepository;
import dev.ratsnest.core.DesignRun;
import dev.ratsnest.core.DesignRunRepository;
import dev.ratsnest.security.RunAccessPolicy;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.domain.PageRequest;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.transaction.annotation.Transactional;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest(properties = {
        "spring.datasource.url=jdbc:h2:mem:tenant-test;DB_CLOSE_DELAY=-1",
        "spring.jpa.hibernate.ddl-auto=create-drop",
        "spring.task.scheduling.enabled=false",
        "ratsnest.security.mode=jwt"
})
@Transactional
class TenantIsolationIntegrationTest {

    @Autowired UserAccountRepository users;
    @Autowired TenantProvisioningService provisioning;
    @Autowired DesignRunRepository runs;
    @Autowired RunAccessPolicy access;
    @Autowired TenantAccessService tenantAccess;
    @Autowired RunApprovalService approvals;

    @TempDir Path tempDir;

    @AfterEach
    void clearSecurityContext() {
        SecurityContextHolder.clearContext();
    }

    @Test
    void organizationMembershipScopesRunsAndApprovals() throws Exception {
        UserAccount alice = users.save(UserAccount.create(
                "alice", "hash", "USER"));
        UserAccount bob = users.save(UserAccount.create(
                "bob", "hash", "USER"));
        var aliceTenant = provisioning.provisionPersonalTenant(alice);
        var bobTenant = provisioning.provisionPersonalTenant(bob);

        Path aliceProjectDir = Files.createDirectory(tempDir.resolve("alice"));
        Files.writeString(aliceProjectDir.resolve("boardplan.json"),
                "{\"topology\":\"ldo\",\"components\":[]}");
        DesignRun aliceRun = DesignRun.createDesign(
                "12V to 5V", aliceProjectDir.toString(), 4, "crew");
        aliceRun.setOwner(alice.getUsername());
        aliceRun.assignProject(aliceTenant.project(), alice.getId());
        aliceRun.setStatus("converged");
        runs.save(aliceRun);

        DesignRun bobRun = DesignRun.createDesign(
                "12V to 3.3V", tempDir.resolve("bob").toString(), 4, "crew");
        bobRun.setOwner(bob.getUsername());
        bobRun.assignProject(bobTenant.project(), bob.getId());
        runs.save(bobRun);

        authenticate(alice);
        assertThat(access.canAccess(aliceRun)).isTrue();
        assertThat(access.canAccess(bobRun)).isFalse();
        assertThat(access.canApprove(aliceRun)).isTrue();
        assertThat(access.canApprove(bobRun)).isFalse();
        assertThat(runs.findVisibleToUser(
                tenantAccess.currentOrganizationIds(), alice.getUsername(),
                PageRequest.of(0, 20)).getContent())
                .extracting(DesignRun::getId)
                .containsExactly(aliceRun.getId());

        aliceRun.setPlanSha256("a".repeat(64));
        var planApproval = approvals.ensurePlanReview(aliceRun).orElseThrow();
        assertThat(planApproval.getStatus()).isEqualTo("pending");
        approvals.decide(aliceRun, RunApprovalService.BOARD_PLAN,
                "approved", alice.getUsername(), "plan checked");
        DesignRun reviewedRun = runs.findById(aliceRun.getId()).orElseThrow();
        reviewedRun.setStatus("converged");
        reviewedRun.setPlanContractVersion("ratsnest.design-plan.v2");
        reviewedRun.setResultJson(passedProductionResult());
        var approval = approvals.ensureReleaseReview(reviewedRun).orElseThrow();
        assertThat(approval.getStatus()).isEqualTo("pending");
        approvals.decide(reviewedRun, "approved", alice.getUsername(), "checked");
        assertThat(reviewedRun.getReleaseStatus()).isEqualTo("approved");
        assertThat(approvals.decide(
                reviewedRun, "approved", alice.getUsername(), "retry")
                .getStatus()).isEqualTo("approved");
        assertThatThrownBy(() -> approvals.decide(
                reviewedRun, "rejected", alice.getUsername(), "change"))
                .hasMessageContaining("immutable");
    }

    private static String passedProductionResult() {
        return """
                {
                  "status": "converged",
                  "iterations": [{
                    "scorecard": {
                      "required_gates_passed": true,
                      "gate_results": {
                        "catalog": {"status": "passed", "required": true},
                        "bom": {"status": "passed", "required": true},
                        "erc": {"status": "passed", "required": true},
                        "drc": {"status": "passed", "required": true},
                        "spice": {"status": "passed", "required": true},
                        "thermal": {"status": "passed", "required": true},
                        "emc": {"status": "passed", "required": true}
                      }
                    }
                  }]
                }
                """;
    }

    private static void authenticate(UserAccount user) {
        SecurityContextHolder.getContext().setAuthentication(
                new UsernamePasswordAuthenticationToken(
                        user.getUsername(), null,
                        List.of(new SimpleGrantedAuthority("ROLE_USER"))));
    }
}
