package team.ratsnest.controlplane.agentgateway.domain.model;

public final class AgentRuntimeException extends RuntimeException {

    private final int status;

    public AgentRuntimeException(int status, String message) {
        super(message);
        this.status = status;
    }

    public int status() {
        return status;
    }
}
