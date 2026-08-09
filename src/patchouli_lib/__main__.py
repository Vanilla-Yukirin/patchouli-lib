import uvicorn

from patchouli_lib.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(
        "patchouli_lib.app:app",
        host="0.0.0.0",  # noqa: S104 - container listener; host binding is controlled outside
        port=8000,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
