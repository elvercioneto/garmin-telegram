import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GARMIN_MCP = (
    r"C:\Users\Foton\garmin_mcp_server"
    r"\venv\Scripts\garmin-mcp.exe"
)
COOKIES = r"C:\Users\Foton\.garminconnect\cookies.json"

INTERESSE = {
    "get_scheduled_workouts",
    "get_training_plan_workouts",
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
            resultado = await session.list_tools()

            encontrados = [
                ferramenta
                for ferramenta in resultado.tools
                if ferramenta.name in INTERESSE
            ]

            if not encontrados:
                print("As ferramentas procuradas não apareceram.")
                print("Ferramentas com nomes relacionados:")
                for ferramenta in resultado.tools:
                    nome = ferramenta.name.lower()
                    if "workout" in nome or "training" in nome or "schedul" in nome:
                        print(f"- {ferramenta.name}: {ferramenta.description}")
                return

            for ferramenta in encontrados:
                print(f"\n=== {ferramenta.name} ===")
                print(ferramenta.description)
                print(json.dumps(
                    ferramenta.inputSchema,
                    indent=2,
                    ensure_ascii=False,
                ))


if __name__ == "__main__":
    asyncio.run(main())
