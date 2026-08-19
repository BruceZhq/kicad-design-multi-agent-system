package dev.ratsnest.security;

import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.test.util.ReflectionTestUtils;

import static org.assertj.core.api.Assertions.assertThat;

class SameOriginMutationFilterTest {

    @Test
    void cookieMutationsRequireWebHeader() throws Exception {
        SameOriginMutationFilter filter = jwtFilter();
        MockHttpServletRequest request = new MockHttpServletRequest(
                "POST", "/api/designs");
        request.setAttribute(CookieBearerFilter.COOKIE_AUTH_ATTRIBUTE, true);
        request.addHeader("Authorization", "Bearer cookie-token");
        MockHttpServletResponse response = new MockHttpServletResponse();

        filter.doFilter(request, response, new MockFilterChain());

        assertThat(response.getStatus()).isEqualTo(403);
    }

    @Test
    void webHeaderAndExplicitBearerAreAccepted() throws Exception {
        SameOriginMutationFilter filter = jwtFilter();
        MockHttpServletRequest web = new MockHttpServletRequest(
                "POST", "/api/designs");
        web.addHeader(SameOriginMutationFilter.HEADER, "web");
        MockHttpServletResponse webResponse = new MockHttpServletResponse();
        filter.doFilter(web, webResponse, new MockFilterChain());
        assertThat(webResponse.getStatus()).isEqualTo(200);

        MockHttpServletRequest bearer = new MockHttpServletRequest(
                "POST", "/api/designs");
        bearer.addHeader("Authorization", "Bearer api-token");
        MockHttpServletResponse bearerResponse = new MockHttpServletResponse();
        filter.doFilter(bearer, bearerResponse, new MockFilterChain());
        assertThat(bearerResponse.getStatus()).isEqualTo(200);
    }

    private static SameOriginMutationFilter jwtFilter() {
        SameOriginMutationFilter filter = new SameOriginMutationFilter();
        ReflectionTestUtils.setField(filter, "securityMode", "jwt");
        return filter;
    }
}
