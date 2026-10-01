import asyncio
import json
import os
from datetime import date, datetime
from pathlib import Path

import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


GARMIN_MCP = os.environ.get("GARMIN_MCP", "garmin-mcp")
GARMINTOKENS = os.environ.get(
    "GARMINTOKENS",
    os.path.expanduser("~/.garminconnect"),
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL")
GEMINI_URL_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

STATE_FILE = Path(
    os.environ.get(
        "GARMIN_STATE_FILE",
        str(Path(__file__).resolve().parent / "ultima_atividade.txt"),
    )
)


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

    if isinstance(data, str):
        if data.startswith("Error"):
            raise RuntimeError(f"Erro ao consultar atividades Garmin: {data}")
        if data.startswith("No activities found"):
            return []
        raise RuntimeError(
            f"Resposta inesperada da ferramenta de atividades: {data[:300]}"
        )

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("activities", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value

    raise RuntimeError("A resposta do Garmin não continha uma lista de atividades.")



def get_activity_id(activity):
    value = (
        activity.get("activityId")
        or activity.get("activity_id")
        or activity.get("id")
    )
    return str(value) if value is not None else ""


async def fetch_activities_until_state(session, previous_id, max_pages=100):
    """Busca atividades paginadas até encontrar o ID salvo."""
    page_size = 20
    activities = []

    for page in range(max_pages):
        start = page * page_size
        print(f"Consultando página {page + 1}, início {start}.")

        result = await session.call_tool(
            "get_activities",
            arguments={
                "start": start,
                "limit": page_size,
            },
        )

        page_activities = parse_activities(result_text(result))

        if not page_activities:
            break

        activities.extend(page_activities)

        if previous_id and any(
            get_activity_id(activity) == previous_id
            for activity in activities
        ):
            return activities

        if len(page_activities) < page_size:
            break

        if not previous_id:
            return activities

    if previous_id and not any(
        get_activity_id(activity) == previous_id
        for activity in activities
    ):
        raise RuntimeError(
            "O ID de estado não foi encontrado nas páginas consultadas. "
            "O estado foi preservado; nenhuma atividade foi marcada "
            "como processada."
        )

    return activities



def get_activity_name(activity):
    return (
        activity.get("activityName")
        or activity.get("activity_name")
        or activity.get("name")
        or "Treino Garmin"
    )


def get_activity_date(activity):
    """Tenta obter a data em que a atividade foi realizada."""
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
    """Localiza o identificador do treino planejado."""
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
    """Busca detalhes de um treino planejado, se houver identificador."""
    schema = schemas.get("get_workout_by_id")

    if not schema:
        return None

    properties = schema.get("properties", {})
    required = schema.get("required", [])
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
        print(f"Não foi possível buscar detalhes do treino planejado: {error}")
        return None


async def append_workout_details(session, schemas, data, context_parts, label):
    """Acrescenta detalhes de até três treinos encontrados na resposta."""
    items = get_scheduled_items(data)

    for workout in items[:3]:
        if not isinstance(workout, dict):
            continue

        details = await get_workout_details(session, schemas, workout)

        if details:
            context_parts.append(f"{label}:\n{details}")


async def get_planned_workout_context(session, schemas, target_date):
    """Consulta treinos agendados para a data da atividade."""
    context_parts = []

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


def analyze_with_gemini(activity_name, activity_details, planned_workout):
    """Compara a atividade realizada com o treino planejado."""
    if not GEMINI_API_KEY:
        raise RuntimeError("A variável GEMINI_API_KEY não foi configurada.")

    if not GEMINI_MODEL:
        raise RuntimeError("A variável GEMINI_MODEL não foi configurada.")

    prompt = (
        "Compare o treino realizado com o treino planejado para o dia. "
        "Use somente os dados fornecidos; não invente informações. "
        "Responda em português, de forma objetiva, com estes tópicos: "
        "comparação com o planejado, ponto positivo, algo a observar e "
        "uma recomendação prática de recuperação. Se faltarem dados, "
        "diga isso claramente.\n\n"
        f"Atividade: {activity_name}\n\n"
        f"Dados da atividade:\n{str(activity_details)[:10000]}\n\n"
        f"Treino planejado:\n{str(planned_workout)[:6000]}"
    )

    response = requests.post(
        f"{GEMINI_URL_BASE}/{GEMINI_MODEL}:generateContent",
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 500,
            },
        },
        timeout=(10, 120),
    )
    response.raise_for_status()

    data = response.json()
    candidates = data.get("candidates", [])

    if not candidates:
        raise RuntimeError("A Gemini API não retornou uma análise.")

    parts = candidates[0].get("content", {}).get("parts", [])
    analysis = "\n".join(
        part.get("text", "")
        for part in parts
        if part.get("text")
    ).strip()

    if not analysis:
        raise RuntimeError("A Gemini API não retornou texto para a análise.")

    return analysis


def send_telegram_message(message):
    """Envia uma mensagem para o chat configurado no Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError(
            "Configure TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID."
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
    if not os.path.isdir(GARMINTOKENS):
        raise RuntimeError(
            f"O diretório de tokens Garmin não existe: {GARMINTOKENS}"
        )

    params = StdioServerParameters(
        command=GARMIN_MCP,
        args=[],
        env={
            **os.environ,
            "GARMINTOKENS": GARMINTOKENS,
        },
    )

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    print("Consultando atividades do Garmin.")
    print(f"Servidor MCP: {GARMIN_MCP}")
    print(f"Arquivo de estado: {STATE_FILE}")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            schemas = get_tool_schemas(tools_result)

            previous_id = (
                STATE_FILE.read_text(encoding="utf-8").strip()
                if STATE_FILE.exists()
                else ""
            )

            activities = await fetch_activities_until_state(
                session,
                previous_id,
            )

            if not activities:
                print("Nenhuma atividade encontrada.")
                return

            newest = activities[0]
            newest_id = get_activity_id(newest)

            if not newest_id:
                raise RuntimeError(
                    "Não foi possível encontrar o ID da atividade mais recente."
                )

            # Primeira execução: registra a atividade mais recente como ponto
            # inicial e não envia análises retroativas.
            if not previous_id:
                STATE_FILE.write_text(newest_id, encoding="utf-8")
                print(
                    "Estado inicial criado na atividade mais recente; "
                    "nenhuma análise antiga foi enviada."
                )
                return

            previous_index = next(
                (
                    index
                    for index, activity in enumerate(activities)
                    if get_activity_id(activity) == previous_id
                ),
                None,
            )

            if previous_index is None:
                raise RuntimeError(
                    "O ID salvo não foi encontrado. "
                    "O estado foi preservado; nenhuma atividade foi "
                    "marcada como processada."
                )

            pending = activities[:previous_index]

            if not pending:
                print("Nenhuma atividade nova.")
                return

            # Processa da atividade nova mais antiga até a mais recente.
            for activity in reversed(pending):
                activity_id = get_activity_id(activity)
                activity_name = get_activity_name(activity)
                activity_date = get_activity_date(activity)

                if not activity_id:
                    raise RuntimeError(
                        "Uma atividade retornada pelo Garmin não tem ID."
                    )

                print(f"Atividade nova: {activity_name}")
                print(f"Data usada na comparação: {activity_date}")

                details_result = await session.call_tool(
                    "get_activity",
                    arguments={"activity_id": activity_id},
                )
                activity_details = result_text(details_result)

                planned_workout = await get_planned_workout_context(
                    session,
                    schemas,
                    activity_date,
                )

                analysis = await asyncio.to_thread(
                    analyze_with_gemini,
                    activity_name,
                    activity_details,
                    planned_workout,
                )

                telegram_message = (
                    f"🏃 Análise de: {activity_name}\n"
                    f"📅 Data: {activity_date}\n\n"
                    f"{analysis}"
                )

                await asyncio.to_thread(
                    send_telegram_message,
                    telegram_message,
                )

                # Atualiza o estado somente após o envio confirmado.
                STATE_FILE.write_text(activity_id, encoding="utf-8")
                print(f"Análise enviada pelo Telegram: {activity_name}")


if __name__ == "__main__":
    asyncio.run(main())
