import asyncio
import json
import os
from datetime import date, datetime
from pathlib import Path

import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


GARMIN_MCP = r"C:\Users\Foton\garmin_mcp_server\venv\Scripts\garmin-mcp.exe"
COOKIES = r"C:\Users\Foton\.garminconnect\cookies.json"

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "qwen2.5:3b"

# O arquivo de estado fica ao lado deste script.
STATE_FILE = Path(__file__).resolve().parent / "ultima_atividade.txt"


def result_text(result):
    """Extrai o texto retornado por uma ferramenta MCP."""
    texts = [
        item.text
        for item in result.content
        if getattr(item, "text", None)
    ]

    text = "\n".join(texts)

    if getattr(result, "isError", False):
        raise RuntimeError(text or "O servidor Garmin MCP retornou um erro.")

    return text


def parse_json_or_text(text):
    """Converte texto JSON; se não for JSON, devolve o texto original."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def parse_activities(text):
    """Converte a resposta da ferramenta get_activities em uma lista."""
    data = parse_json_or_text(text)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("activities", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def get_activity_id(activity):
    value = (
        activity.get("activityId")
        or activity.get("activity_id")
        or activity.get("id")
    )
    return str(value) if value is not None else ""


def get_activity_name(activity):
    return (
        activity.get("activityName")
        or activity.get("activity_name")
        or activity.get("name")
        or "Treino Garmin"
    )


def get_activity_date(activity):
    """
    Tenta obter a data em que a atividade foi realizada.
    Se não encontrar uma data reconhecível, usa a data de hoje.
    """
    date_fields = (
        "startTimeLocal",
        "start_time_local",
        "startTimeGMT",
        "start_time_gmt",
        "startTime",
        "start_time",
        "activityDate",
        "date",
    )

    for field in date_fields:
        value = activity.get(field)

        if value is None:
            continue

        # Datas ISO, por exemplo: 2026-09-30 ou 2026-09-30T07:15:00.
        if isinstance(value, str):
            value = value.strip()

            if len(value) >= 10:
                try:
                    return date.fromisoformat(value[:10]).isoformat()
                except ValueError:
                    pass

            try:
                normalized = value.replace("Z", "+00:00")
                return datetime.fromisoformat(normalized).date().isoformat()
            except ValueError:
                pass

        # Timestamps em segundos ou milissegundos.
        if isinstance(value, (int, float)):
            try:
                timestamp = value / 1000 if value > 100_000_000_000 else value
                return datetime.fromtimestamp(timestamp).date().isoformat()
            except (OverflowError, OSError, ValueError):
                pass

    return date.today().isoformat()


def format_data(data):
    """Formata dados para enviar ao modelo."""
    if isinstance(data, str):
        return data

    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def get_tool_schemas(tools_result):
    """Cria um mapa com o esquema de argumentos de cada ferramenta MCP."""
    schemas = {}

    for tool in getattr(tools_result, "tools", []):
        schemas[tool.name] = getattr(tool, "inputSchema", {}) or {}

    return schemas


def get_scheduled_items(data):
    """Tenta localizar a lista de treinos na resposta do Garmin."""
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in (
            "scheduledWorkouts",
            "scheduled_workouts",
            "workouts",
            "data",
            "results",
        ):
            value = data.get(key)
            if isinstance(value, list):
                return value

    return []


def find_workout_identifier(workout, argument_name):
    """
    Localiza o identificador do treino.

    Alguns treinos usam workout_id; treinos de planos Garmin podem usar
    workout_uuid. As duas formas são consideradas.
    """
    aliases = {
        "workout_id": (
            "workout_id",
            "workoutId",
            "workout_uuid",
            "workoutUuid",
            "id",
        ),
        "workout_uuid": (
            "workout_uuid",
            "workoutUuid",
            "workout_id",
            "workoutId",
            "uuid",
            "id",
        ),
        "id": (
            "id",
            "workout_id",
            "workoutId",
            "workout_uuid",
            "workoutUuid",
            "uuid",
        ),
    }

    normalized_name = argument_name.replace("-", "_").lower()
    keys = aliases.get(
        normalized_name,
        (
            argument_name,
            "workout_id",
            "workoutId",
            "workout_uuid",
            "workoutUuid",
            "uuid",
            "id",
        ),
    )

    for key in keys:
        value = workout.get(key)
        if value is not None:
            return value

    return None


async def get_workout_details(session, schemas, workout):
    """Busca os detalhes de um treino planejado, se houver identificador."""
    schema = schemas.get("get_workout_by_id")

    if not schema:
        return None

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # Usa os argumentos obrigatórios definidos pelo servidor.
    argument_names = required or list(properties.keys())

    arguments = {}

    for argument_name in argument_names:
        value = find_workout_identifier(workout, argument_name)

        if value is None:
            return None

        arguments[argument_name] = value

    if not arguments:
        return None

    try:
        result = await session.call_tool(
            "get_workout_by_id",
            arguments=arguments,
        )
        return result_text(result)
    except Exception as error:
        print(f"Não foi possível buscar os detalhes do treino planejado: {error}")
        return None


async def append_workout_details(session, schemas, data, context_parts, label):
    """Acrescenta detalhes de até três treinos encontrados numa resposta."""
    items = get_scheduled_items(data)

    for workout in items[:3]:
        if not isinstance(workout, dict):
            continue

        details = await get_workout_details(session, schemas, workout)

        if details:
            context_parts.append(f"{label}:\n{details}")


async def get_planned_workout_context(session, schemas, target_date):
    """
    Consulta os treinos agendados para a data da atividade e, se possível,
    busca os detalhes dos treinos.
    """
    context_parts = []

    # Treinos agendados no calendário do Garmin Connect.
    try:
        scheduled_result = await session.call_tool(
            "get_scheduled_workouts",
            arguments={
                "start_date": target_date,
                "end_date": target_date,
            },
        )

        scheduled_text = result_text(scheduled_result)
        scheduled_data = parse_json_or_text(scheduled_text)

        context_parts.append(
            f"Treinos agendados no calendário Garmin para {target_date}:\n"
            f"{format_data(scheduled_data)}"
        )

        await append_workout_details(
            session,
            schemas,
            scheduled_data,
            context_parts,
            "Detalhes de um treino agendado",
        )

    except Exception as error:
        context_parts.append(
            f"Não foi possível consultar os treinos agendados para "
            f"{target_date}: {error}"
        )

    # Consulta também o plano de treinamento ativo, se existir.
    try:
        plan_result = await session.call_tool(
            "get_training_plan_workouts",
            arguments={"calendar_date": target_date},
        )

        plan_text = result_text(plan_result)
        plan_data = parse_json_or_text(plan_text)

        context_parts.append(
            f"Treinos do plano de treinamento para a semana de {target_date}:\n"
            f"{format_data(plan_data)}"
        )

        await append_workout_details(
            session,
            schemas,
            plan_data,
            context_parts,
            "Detalhes de um treino do plano",
        )

    except Exception as error:
        context_parts.append(
            f"Não foi possível consultar o plano de treinamento: {error}"
        )

    if not context_parts:
        return "Não foram encontrados dados de treino planejado."

    return "\n\n".join(context_parts)


def analyze_with_ollama(activity_name, activity_details, planned_workout):
    """Compara a atividade com o treino planejado usando o Ollama."""

    # Evita enviar respostas enormes do Garmin, que podem deixar a análise
    # muito lenta no modelo local.
    activity_details = str(activity_details)[:10000]
    planned_workout = str(planned_workout)[:6000]

    prompt = (
        "Compare o treino realizado com o treino planejado para o dia. "
        "Use somente os dados fornecidos; não invente informações. "
        "Responda em português, de forma objetiva, com estes tópicos: "
        "comparação com o planejado, ponto positivo, algo a observar e "
        "uma recomendação prática de recuperação. Se faltarem dados, "
        "diga isso claramente.\n\n"
        f"Atividade: {activity_name}\n\n"
        f"Dados da atividade:\n{activity_details}\n\n"
        f"Treino planejado:\n{planned_workout}"
    )

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_predict": 350,
            },
        },
        timeout=(10, 900),
    )
    response.raise_for_status()

    data = response.json()
    analysis = data.get("message", {}).get("content", "").strip()

    if not analysis:
        raise RuntimeError("O Ollama respondeu, mas não retornou uma análise.")

    return analysis



def send_telegram_message(message):
    """Envia uma mensagem para o chat configurado no Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Configure as variáveis TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID."
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message[:4000],
        },
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("O Telegram não confirmou o envio da mensagem.")


async def main():
    params = StdioServerParameters(
        command=GARMIN_MCP,
        args=[],
        env={
            **os.environ,
            "GARMIN_COOKIES_FILE": COOKIES,
        },
    )

    print(f"Monitorando atividades do Garmin. Modelo local: {OLLAMA_MODEL}")
    print("O programa verifica se há uma atividade nova a cada 5 minutos.")
    print("Ao detectar uma atividade, consulta o treino planejado para a data dela.")
    print("Para encerrar, pressione Ctrl+C.")

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools_result = await session.list_tools()
                schemas = get_tool_schemas(tools_result)

                while True:
                    try:
                        result = await session.call_tool(
                            "get_activities",
                            arguments={},
                        )

                        activities = parse_activities(result_text(result))

                        if activities:
                            newest = activities[0]
                            activity_id = get_activity_id(newest)
                            activity_name = get_activity_name(newest)
                            activity_date = get_activity_date(newest)

                            previous_id = (
                                STATE_FILE.read_text(encoding="utf-8").strip()
                                if STATE_FILE.exists()
                                else ""
                            )

                            if activity_id and activity_id != previous_id:
                                print(f"\nAtividade nova: {activity_name}")
                                print(f"Data usada na comparação: {activity_date}")
                                print("Buscando detalhes no Garmin Connect...")

                                details_result = await session.call_tool(
                                    "get_activity",
                                    arguments={"activity_id": activity_id},
                                )
                                activity_details = result_text(details_result)

                                print(
                                    f"Consultando o treino planejado para "
                                    f"{activity_date}..."
                                )

                                planned_workout = await get_planned_workout_context(
                                    session,
                                    schemas,
                                    activity_date,
                                )

                                print("Enviando os dados ao Ollama para análise...")
                                analysis = await asyncio.to_thread(
                                    analyze_with_ollama,
                                    activity_name,
                                    activity_details,
                                    planned_workout,
                                )

                                telegram_message = (
                                    f"🏃 Análise de: {activity_name}\n"
                                    f"📅 Data: {activity_date}\n\n"
                                    f"{analysis}"
                                )

                                print("Enviando a análise pelo Telegram...")
                                await asyncio.to_thread(
                                    send_telegram_message,
                                    telegram_message,
                                )

                                # Registra a atividade somente depois do envio.
                                STATE_FILE.write_text(
                                    activity_id,
                                    encoding="utf-8",
                                )

                                print("Análise enviada pelo Telegram.")
                            else:
                                print("Nenhuma atividade nova.")
                        else:
                            print("Nenhuma atividade encontrada.")

                    except Exception as error:
                        print(f"Erro nesta consulta: {error}")

                    await asyncio.sleep(300)

    except KeyboardInterrupt:
        print("\nMonitoramento encerrado.")


if __name__ == "__main__":
    asyncio.run(main())
