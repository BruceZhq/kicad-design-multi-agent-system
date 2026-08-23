from observability.bootstrap import _enabled, _signal_endpoint


def test_local_observability_requires_explicit_opt_in() -> None:
    assert _enabled("true") is True
    assert _enabled("1") is True
    assert _enabled("") is False
    assert _enabled(None) is False


def test_signal_endpoint_is_stable() -> None:
    assert _signal_endpoint("http://collector:4318", "traces") == (
        "http://collector:4318/v1/traces"
    )
    assert _signal_endpoint("http://collector:4318/v1/metrics", "metrics") == (
        "http://collector:4318/v1/metrics"
    )
