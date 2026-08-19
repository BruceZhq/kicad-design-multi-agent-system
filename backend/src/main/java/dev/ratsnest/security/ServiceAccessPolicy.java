package dev.ratsnest.security;

import dev.ratsnest.tenant.TenantAccessService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.web.server.ResponseStatusException;

@Component
public class ServiceAccessPolicy {

    private final TenantAccessService tenants;

    @Value("${ratsnest.security.mode:open}")
    private String securityMode;

    public ServiceAccessPolicy(TenantAccessService tenants) {
        this.tenants = tenants;
    }

    public void requireServiceOrOpenMode() {
        if (!"open".equalsIgnoreCase(securityMode)
                && !tenants.currentIsService()) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN,
                    "service identity required");
        }
    }
}
