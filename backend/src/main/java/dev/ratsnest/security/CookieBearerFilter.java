package dev.ratsnest.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.Cookie;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;

/** Bridges cookie auth to bearer auth: when a request carries the HttpOnly
 *  `ratsnest_token` cookie (set by /api/auth/login) and no Authorization
 *  header, the token is presented as a Bearer header — so the untouched
 *  frontend works in jwt mode after one visit to /login.html. */
@Component
public class CookieBearerFilter extends OncePerRequestFilter {

    public static final String COOKIE = "ratsnest_token";
    public static final String COOKIE_AUTH_ATTRIBUTE =
            "dev.ratsnest.security.cookie-auth";

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain chain)
            throws ServletException, IOException {
        if (request.getHeader("Authorization") == null
                && request.getCookies() != null) {
            for (Cookie cookie : request.getCookies()) {
                if (COOKIE.equals(cookie.getName())
                        && cookie.getValue() != null
                        && !cookie.getValue().isBlank()) {
                    String bearer = "Bearer " + cookie.getValue();
                    request.setAttribute(COOKIE_AUTH_ATTRIBUTE, Boolean.TRUE);
                    request = new HttpServletRequestWrapper(request) {
                        @Override
                        public String getHeader(String name) {
                            return "Authorization".equalsIgnoreCase(name)
                                    ? bearer : super.getHeader(name);
                        }

                        @Override
                        public Enumeration<String> getHeaders(String name) {
                            return "Authorization".equalsIgnoreCase(name)
                                    ? Collections.enumeration(List.of(bearer))
                                    : super.getHeaders(name);
                        }
                    };
                    break;
                }
            }
        }
        chain.doFilter(request, response);
    }
}
