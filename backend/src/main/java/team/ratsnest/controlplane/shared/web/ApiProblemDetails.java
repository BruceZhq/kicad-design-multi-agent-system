package team.ratsnest.controlplane.shared.web;

import java.net.URI;

import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.ProblemDetail;

import jakarta.servlet.http.HttpServletRequest;

public final class ApiProblemDetails {

    private ApiProblemDetails() {
    }

    public static ProblemDetail create(
            HttpStatusCode status,
            String code,
            String detail,
            HttpServletRequest request) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(status, detail);
        return enrich(problem, status, code, request);
    }

    public static ProblemDetail enrich(
            ProblemDetail problem,
            HttpStatusCode status,
            String code,
            HttpServletRequest request) {
        problem.setStatus(status.value());
        if (problem.getTitle() == null) {
            HttpStatus resolved = HttpStatus.resolve(status.value());
            problem.setTitle(resolved == null ? "Request failed" : resolved.getReasonPhrase());
        }
        if (problem.getDetail() == null || problem.getDetail().isBlank()) {
            problem.setDetail("The request could not be completed.");
        }
        problem.setInstance(URI.create(request.getRequestURI()));
        problem.setProperty("code", code);
        problem.setProperty("traceId", requestId(request));
        return problem;
    }

    public static String requestId(HttpServletRequest request) {
        Object value = request.getAttribute(RequestIdFilter.ATTRIBUTE_NAME);
        return value == null ? "unavailable" : value.toString();
    }
}
