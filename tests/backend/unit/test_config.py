from app.config import ExecutionMode, Settings


def test_mempool_copying_is_on_for_paper_and_opt_in_for_live():
    def enabled(**kw):
        return Settings(_env_file=None, **kw).mempool_enabled  # type: ignore[call-arg]

    assert enabled(execution_mode=ExecutionMode.PAPER) is True
    assert enabled(execution_mode=ExecutionMode.LIVE) is False
    assert enabled(execution_mode=ExecutionMode.LIVE, chain_mempool=True) is True
    assert enabled(execution_mode=ExecutionMode.PAPER, chain_mempool=False) is False


def test_mempool_endpoints_race_the_chain_stream_and_extra_urls_without_duplicates():
    def urls(**kw):
        return Settings(_env_file=None, **kw).mempool_ws_urls  # type: ignore[call-arg]

    rpc = "https://polygon.example"
    paid = "wss://paid.example/ws"
    assert urls(polygon_rpc_url=rpc) == ("wss://polygon.example",)
    assert urls(polygon_rpc_url=rpc, chain_mempool_ws_urls=[paid, "wss://polygon.example"]) == (
        "wss://polygon.example",
        paid,
    )
    assert urls(execution_mode=ExecutionMode.LIVE, chain_mempool_ws_urls=[paid]) == ()
