from app.config import ExecutionMode, Settings


def test_mempool_copying_is_on_for_paper_and_opt_in_for_live():
    def enabled(**kw):
        return Settings(_env_file=None, **kw).mempool_enabled  # type: ignore[call-arg]

    assert enabled(execution_mode=ExecutionMode.PAPER) is True
    assert enabled(execution_mode=ExecutionMode.LIVE) is False
    assert enabled(execution_mode=ExecutionMode.LIVE, chain_mempool=True) is True
    assert enabled(execution_mode=ExecutionMode.PAPER, chain_mempool=False) is False
