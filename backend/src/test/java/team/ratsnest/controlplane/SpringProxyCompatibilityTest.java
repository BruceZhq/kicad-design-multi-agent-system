package team.ratsnest.controlplane;

import static org.assertj.core.api.Assertions.assertThat;

import java.lang.reflect.Modifier;

import org.junit.jupiter.api.Test;

import team.ratsnest.controlplane.evolution.api.EvolutionAdminController;
import team.ratsnest.controlplane.evolution.application.EvolutionCollector;
import team.ratsnest.controlplane.harness.api.HarnessReleaseAdminController;
import team.ratsnest.controlplane.run.infrastructure.persistence.JdbcRunEventIngestionStore;

class SpringProxyCompatibilityTest {

    @Test
    void validatedAndTransactionalBeansRemainProxyable() {
        assertThat(Modifier.isFinal(EvolutionCollector.class.getModifiers())).isFalse();
        assertThat(Modifier.isFinal(EvolutionAdminController.class.getModifiers())).isFalse();
        assertThat(Modifier.isFinal(HarnessReleaseAdminController.class.getModifiers())).isFalse();
        assertThat(Modifier.isFinal(JdbcRunEventIngestionStore.class.getModifiers())).isFalse();
    }
}
