package dev.ratsnest.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.Set;

@Component
public class SameOriginMutationFilter extends OncePerRequestFilter {

    public static final String HEADER = "X-RatsNest-Client";
    private static final Set<String> SAFE_METHODS =
            Set.of("GET", "HEAD", "OPTIONS");

    @Value("${ratsnest.security.mode:open}")
    private String securityMode;

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain chain)
            throws ServletException, IOException {
        if (!"jwt".equalsIgnoreCase(securityMode)
                || SAFE_METHODS.contains(request.getMethod())
                || !request.getRequestURI().startsWith("/api/")
                || serviceCaller()
                || explicitBearer(request)
                || "web".equals(request.getHeader(HEADER))) {
            chain.doFilter(request, response);
            return;
        }
        response.sendError(HttpServletResponse.SC_FORBIDDEN,
                "missing same-origin mutation header");
    }

    private static boolean serviceCaller() {
        Authentication auth = SecurityContextHolder.getContext()
                .getAuthentication();
        return auth != null && auth.getAuthorities().stream()
                .anyMatch(authority -> "ROLE_SERVICE".equals(
                        authority.getAuthority()));
    }

    private static boolean explicitBearer(HttpServletRequest request) {
        return request.getAttribute(
                CookieBearerFilter.COOKIE_AUTH_ATTRIBUTE) == null
                && request.getHeader("Authorization") != null;
    }
}
