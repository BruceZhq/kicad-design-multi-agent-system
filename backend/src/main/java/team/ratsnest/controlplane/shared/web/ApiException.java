package team.ratsnest.controlplane.shared.web;

import java.util.Objects;

import org.springframework.http.HttpStatus;

public final class ApiException extends RuntimeException {

    private final String code;
    private final HttpStatus status;

    public ApiException(String code, HttpStatus status, String detail) {
        super(Objects.requireNonNull(detail, "detail"));
        this.code = Objects.requireNonNull(code, "code");
        this.status = Objects.requireNonNull(status, "status");
    }

    public String code() {
        return code;
    }

    public HttpStatus status() {
        return status;
    }
}
