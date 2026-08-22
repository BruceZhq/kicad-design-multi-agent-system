package team.ratsnest.controlplane.bootstrap;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication(scanBasePackages = "team.ratsnest.controlplane")
@EnableScheduling
public class RatsNestControlPlaneApplication {

    public static void main(String[] args) {
        SpringApplication.run(RatsNestControlPlaneApplication.class, args);
    }
}
