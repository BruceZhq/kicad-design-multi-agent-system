"""Generated Agent Runtime v1 protobuf bindings."""

import sys

from service.proto import agent_runtime_pb2

# grpc_tools emits an absolute sibling import when a single proto include root
# is used. Register that generated sibling before loading the service stubs.
sys.modules.setdefault("agent_runtime_pb2", agent_runtime_pb2)

from service.proto import agent_runtime_pb2_grpc  # noqa: E402

__all__ = ["agent_runtime_pb2", "agent_runtime_pb2_grpc"]
