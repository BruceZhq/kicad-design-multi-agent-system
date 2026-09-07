import sys

from ratsnestpro.eda.router_process import routing_command, run_router


def test_router_may_not_invent_width_exceptions():
    args = routing_command("freerouting", "board with spaces.dsn", "board.ses", "31")
    assert args[2] == "board with spaces.dsn"
    assert "--router.fanout.enabled=false" in args
    assert "--router.automatic_neckdown=false" in args
    assert "--router.neck_width_um=0" in args
    assert not any("seed" in value for value in args)


def test_success_with_warnings_is_not_routing_failure():
    result = run_router([sys.executable, "-c", "print('ordinary warning')"], timeout=3)
    assert result.returncode == 0
    assert result.failure_kind == ""


def test_normalization_livelock_is_bounded():
    code = "import time; print(('max normalization depth\\n'*5),flush=True); time.sleep(10)"
    result = run_router([sys.executable, "-c", code], timeout=3,
                        no_progress_seconds=0.05, normalization_limit=3)
    assert result.failure_kind == "router_normalization_livelock"
    assert result.returncode != 0
    assert result.normalization_warnings == 5


def test_completed_pass_resets_pathology_counter():
    code = ("import time; print(('max normalization depth\\n'*5),flush=True); "
            "print('Auto-router pass #1 was completed',flush=True); time.sleep(.2)")
    result = run_router([sys.executable, "-c", code], timeout=3,
                        no_progress_seconds=0.1, normalization_limit=3)
    assert result.failure_kind == ""
    assert result.completed_passes == 1


def test_silent_hang_obeys_deadline():
    result = run_router([sys.executable, "-c", "import time; time.sleep(10)"], timeout=.2)
    assert result.failure_kind == "router_timeout"
