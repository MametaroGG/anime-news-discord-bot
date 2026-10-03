"""Compatibility entry point. No RSS fetching, heuristic filtering or test posts."""
from news_delivery.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
