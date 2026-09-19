from app.db import models  # noqa: F401 - ensure models are registered on Base.metadata
from app.db.base import Base, engine


def main() -> None:
    Base.metadata.create_all(bind=engine)
    print("database tables created")


if __name__ == "__main__":
    main()
