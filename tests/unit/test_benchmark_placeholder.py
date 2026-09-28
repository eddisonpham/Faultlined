import pytest
from benchmarks.harness import main


@pytest.mark.unit
def test_benchmark_placeholder_is_explicit() -> None:
    with pytest.raises(SystemExit, match="benchmark harness is not implemented yet"):
        main()
