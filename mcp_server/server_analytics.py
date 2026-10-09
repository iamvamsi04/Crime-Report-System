"""Extend the original six registrations with two read-only billing tools."""
from mcp_server.server import mcp
from mcp_server.tools.analytics_tools import billing_analysis, billing_anomalies

mcp.tool(billing_analysis)
mcp.tool(billing_anomalies)

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
