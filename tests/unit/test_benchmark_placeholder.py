import pytest
from benchmarks.harness import _ingest_microbenchmark


@pytest.mark.unit
def test_ingest_benchmark_invocation_has_required_provenance() -> None:
    result = _ingest_microbenchmark()
    assert result.status == "ok"
    assert result.summary.n == 10
    assert result.provenance.workload == "synthetic-episode-ingest"
