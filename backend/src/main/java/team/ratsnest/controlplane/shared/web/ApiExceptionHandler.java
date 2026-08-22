package team.ratsnest.controlplane.shared.web;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.context.request.ServletWebRequest;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

import jakarta.servlet.http.HttpServletRequest;

@RestControllerAdvice
public final class ApiExceptionHandler extends ResponseEntityExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);

    @ExceptionHandler(ApiException.class)
    ResponseEntity<Object> handleApiException(ApiException exception, WebRequest request) {
        ProblemDetail problem = ApiProblemDetails.create(
                exception.status(), exception.code(), exception.getMessage(), servletRequest(request));
        if (exception.status() == HttpStatus.UNAUTHORIZED) {
            return ResponseEntity.status(exception.status())
                    .header(HttpHeaders.WWW_AUTHENTICATE, "Bearer")
                    .body(problem);
        }
        return ResponseEntity.status(exception.status()).body(problem);
    }

    @ExceptionHandler(Exception.class)
    ResponseEntity<Object> handleUnexpectedException(Exception exception, WebRequest request) {
        HttpServletRequest servletRequest = servletRequest(request);
        String requestId = ApiProblemDetails.requestId(servletRequest);
        log.error("Unhandled request failure requestId={}", requestId, exception);

        ProblemDetail problem = ApiProblemDetails.create(
                HttpStatus.INTERNAL_SERVER_ERROR,
                "INTERNAL_ERROR",
                "An unexpected server error occurred.",
                servletRequest);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(problem);
    }

    @Override
    protected ResponseEntity<Object> handleExceptionInternal(
            Exception exception,
            Object body,
            HttpHeaders headers,
            HttpStatusCode statusCode,
            WebRequest request) {
        ProblemDetail problem = body instanceof ProblemDetail detail
                ? detail
                : ProblemDetail.forStatus(statusCode);
        ApiProblemDetails.enrich(
                problem,
                statusCode,
                "HTTP_" + statusCode.value(),
                servletRequest(request));
        return super.handleExceptionInternal(exception, problem, headers, statusCode, request);
    }

    private HttpServletRequest servletRequest(WebRequest request) {
        return ((ServletWebRequest) request).getRequest();
    }
}
