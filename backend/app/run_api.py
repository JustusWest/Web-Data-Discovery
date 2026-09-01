from __future__ import annotations

import uvicorn

from app.api.main import app
from app.settings import get_settings


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, reload=False)
