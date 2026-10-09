"""Launch the existing API with the optional eight-tool billing agent."""
import os
import uvicorn
from api.main import app
from agent.analytics_agent import AnalyticsBillingAgent
from config import settings

app.state.engine = AnalyticsBillingAgent()

if __name__ == "__main__":
    host = os.getenv("API_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not settings.API_KEY:
        raise SystemExit("Set BILLING_API_KEY before listening outside localhost.")
    uvicorn.run(app, host=host, port=int(os.getenv("API_PORT", "8000")), workers=1)
