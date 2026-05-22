"""
Create tables. Run once at setup.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine

from config import get_settings
from data.schema import Base


def main() -> None:
    settings = get_settings()
    print(f"Initializing DB at {settings.database_url.split('@')[-1]}...")
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    print("Done.")


if __name__ == "__main__":
    main()
