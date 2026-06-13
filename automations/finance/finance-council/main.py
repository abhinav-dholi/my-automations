"""finance-council — run the parallel investor-council debate + mediator."""
from __future__ import annotations

import council_cli


def main() -> None:
    raise SystemExit(council_cli.cmd_council(None))


if __name__ == "__main__":
    main()
