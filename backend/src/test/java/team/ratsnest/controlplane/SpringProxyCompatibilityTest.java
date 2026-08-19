package team.ratsnest.controlplane;

import static org.assertj.core.api.Assertions.assertThat;

import java.lang.reflect.Modifier;

import org.junit.jupiter.api.Test;

import team.ratsnest.controlplane.evolution.EvolutionAdminController;
import team.ratsnest.controlplane.evolution.EvolutionCollector;
import team.ratsnest.controlplane.harness.HarnessReleaseAdminController;

class SpringProxyCompatibilityTest {

    @Test
    void validatedAndTransactionalBeansRemainProxyable() {
        assertThat(Modifier.isFinal(EvolutionCollector.class.getModifiers())).isFalse();
        assertThat(Modifier.isFinal(EvolutionAdminController.class.getModifiers())).isFalse();
        assertThat(Modifier.isFinal(HarnessReleaseAdminController.class.getModifiers())).isFalse();
    }
}
