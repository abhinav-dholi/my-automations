"""finance-categorize — Claude labels the Other/Uncategorized transactions."""
from __future__ import annotations

import categorizer


def main() -> None:
    result = categorizer.categorize_uncategorized()
    print(f"[finance-categorize] labeled {result['labeled']} of "
          f"{result['candidates']} uncategorized transactions.")


if __name__ == "__main__":
    main()
