"""Discord gateway stub — must not start a persistent gateway in bootstrap."""
from __future__ import annotations


def start_gateway() -> None:
    raise RuntimeError(
        "Discord gateway start refused: prep only; stop-control must be proven first."
    )


if __name__ == "__main__":
    start_gateway()
