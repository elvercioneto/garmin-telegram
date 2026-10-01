import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GARMIN_MCP = (
    r"C:\Users\Foton\garmin_mcp_server"
    r"\venv\Scripts\garmin-mcp.exe"
)
COOKIES = r"C:\Users\Foton\.garminconnect\cookies.json"

NOMES = {
    "get_scheduled_workouts",
    "get_training_plan_workouts",
    "get_workout_by_id",
    "get_workouts",
}

async def main():
    params = StdioServerParameters(
        command=GARMIN_MCP,
        args=[],
        env={
            **os.environ,
            "GARMIN_COOKIES_FILE": COOKIES,
        },
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()

            for tool in result.tools:
                if tool.name in NOMES:
                    print(f"\n--- {tool.name} ---")
                    print(tool.description or "(sem descrição)")
                    print("Parâmetros:")
                    print(tool.inputSchema)

asyncio.run(main())
