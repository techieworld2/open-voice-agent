"""Run the installed standard-library WebSocket load harness."""

from voice_engine.benchmarks.stress_tester import main, run_benchmark, markdown_table

__all__ = ["main", "run_benchmark", "markdown_table"]

if __name__ == "__main__":
    main()
