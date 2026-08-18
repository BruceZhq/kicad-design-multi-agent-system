from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RunControl(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RUN_CONTROL_UNSPECIFIED: _ClassVar[RunControl]
    RUN_CONTROL_CANCEL: _ClassVar[RunControl]

class RunState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    RUN_STATE_UNSPECIFIED: _ClassVar[RunState]
    RUN_STATE_QUEUED: _ClassVar[RunState]
    RUN_STATE_RUNNING: _ClassVar[RunState]
    RUN_STATE_COMPLETED: _ClassVar[RunState]
    RUN_STATE_FAILED: _ClassVar[RunState]
    RUN_STATE_CANCELLED: _ClassVar[RunState]
    RUN_STATE_TIMED_OUT: _ClassVar[RunState]
    RUN_STATE_WAITING_FOR_INPUT: _ClassVar[RunState]
RUN_CONTROL_UNSPECIFIED: RunControl
RUN_CONTROL_CANCEL: RunControl
RUN_STATE_UNSPECIFIED: RunState
RUN_STATE_QUEUED: RunState
RUN_STATE_RUNNING: RunState
RUN_STATE_COMPLETED: RunState
RUN_STATE_FAILED: RunState
RUN_STATE_CANCELLED: RunState
RUN_STATE_TIMED_OUT: RunState
RUN_STATE_WAITING_FOR_INPUT: RunState

class RuntimeIdentity(_message.Message):
    __slots__ = ("principal_id", "tenant_id", "project_id")
    PRINCIPAL_ID_FIELD_NUMBER: _ClassVar[int]
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PROJECT_ID_FIELD_NUMBER: _ClassVar[int]
    principal_id: str
    tenant_id: str
    project_id: str
    def __init__(self, principal_id: _Optional[str] = ..., tenant_id: _Optional[str] = ..., project_id: _Optional[str] = ...) -> None: ...

class StartRunRequest(_message.Message):
    __slots__ = ("request_id", "thread_id", "identity", "message", "model", "timeout_seconds", "config_json", "stream_tokens")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    IDENTITY_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_JSON_FIELD_NUMBER: _ClassVar[int]
    STREAM_TOKENS_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    thread_id: str
    identity: RuntimeIdentity
    message: str
    model: str
    timeout_seconds: float
    config_json: str
    stream_tokens: bool
    def __init__(self, request_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., identity: _Optional[_Union[RuntimeIdentity, _Mapping]] = ..., message: _Optional[str] = ..., model: _Optional[str] = ..., timeout_seconds: _Optional[float] = ..., config_json: _Optional[str] = ..., stream_tokens: bool = ...) -> None: ...

class GetRunRequest(_message.Message):
    __slots__ = ("request_id", "identity")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    IDENTITY_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    identity: RuntimeIdentity
    def __init__(self, request_id: _Optional[str] = ..., identity: _Optional[_Union[RuntimeIdentity, _Mapping]] = ...) -> None: ...

class ControlRunRequest(_message.Message):
    __slots__ = ("run", "control")
    RUN_FIELD_NUMBER: _ClassVar[int]
    CONTROL_FIELD_NUMBER: _ClassVar[int]
    run: GetRunRequest
    control: RunControl
    def __init__(self, run: _Optional[_Union[GetRunRequest, _Mapping]] = ..., control: _Optional[_Union[RunControl, str]] = ...) -> None: ...

class ResumeRunRequest(_message.Message):
    __slots__ = ("run", "interaction_id", "response_request_id", "answer", "state_version", "model", "timeout_seconds", "config_json")
    RUN_FIELD_NUMBER: _ClassVar[int]
    INTERACTION_ID_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    ANSWER_FIELD_NUMBER: _ClassVar[int]
    STATE_VERSION_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_JSON_FIELD_NUMBER: _ClassVar[int]
    run: GetRunRequest
    interaction_id: str
    response_request_id: str
    answer: str
    state_version: int
    model: str
    timeout_seconds: float
    config_json: str
    def __init__(self, run: _Optional[_Union[GetRunRequest, _Mapping]] = ..., interaction_id: _Optional[str] = ..., response_request_id: _Optional[str] = ..., answer: _Optional[str] = ..., state_version: _Optional[int] = ..., model: _Optional[str] = ..., timeout_seconds: _Optional[float] = ..., config_json: _Optional[str] = ...) -> None: ...

class SubscribeRunEventsRequest(_message.Message):
    __slots__ = ("run", "last_event_seq")
    RUN_FIELD_NUMBER: _ClassVar[int]
    LAST_EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    run: GetRunRequest
    last_event_seq: int
    def __init__(self, run: _Optional[_Union[GetRunRequest, _Mapping]] = ..., last_event_seq: _Optional[int] = ...) -> None: ...

class Run(_message.Message):
    __slots__ = ("request_id", "graph_run_id", "kind", "state", "agent_id", "thread_id", "created_at", "started_at", "finished_at", "event_count", "oldest_event_seq", "newest_event_seq", "error_code", "error", "result_json")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    GRAPH_RUN_ID_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    THREAD_ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    FINISHED_AT_FIELD_NUMBER: _ClassVar[int]
    EVENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    OLDEST_EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    NEWEST_EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    RESULT_JSON_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    graph_run_id: str
    kind: str
    state: RunState
    agent_id: str
    thread_id: str
    created_at: str
    started_at: str
    finished_at: str
    event_count: int
    oldest_event_seq: int
    newest_event_seq: int
    error_code: str
    error: str
    result_json: str
    def __init__(self, request_id: _Optional[str] = ..., graph_run_id: _Optional[str] = ..., kind: _Optional[str] = ..., state: _Optional[_Union[RunState, str]] = ..., agent_id: _Optional[str] = ..., thread_id: _Optional[str] = ..., created_at: _Optional[str] = ..., started_at: _Optional[str] = ..., finished_at: _Optional[str] = ..., event_count: _Optional[int] = ..., oldest_event_seq: _Optional[int] = ..., newest_event_seq: _Optional[int] = ..., error_code: _Optional[str] = ..., error: _Optional[str] = ..., result_json: _Optional[str] = ...) -> None: ...

class RunEvent(_message.Message):
    __slots__ = ("event_seq", "run_id", "type", "payload_json", "created_at")
    EVENT_SEQ_FIELD_NUMBER: _ClassVar[int]
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_JSON_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    event_seq: int
    run_id: str
    type: str
    payload_json: str
    created_at: str
    def __init__(self, event_seq: _Optional[int] = ..., run_id: _Optional[str] = ..., type: _Optional[str] = ..., payload_json: _Optional[str] = ..., created_at: _Optional[str] = ...) -> None: ...
