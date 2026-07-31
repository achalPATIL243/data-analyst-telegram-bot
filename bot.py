import os
import csv
import json
import re
import time
import threading
import io

import requests
from dotenv import load_dotenv
from openai import OpenAI
from flask import Flask, send_file

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    ""
).rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is missing"
    )

if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY is missing"
    )

client = OpenAI(
    api_key=OPENAI_API_KEY
)

LOG_FILE = "run.jsonl"


# ============================================================
# LOGGING
# ============================================================

def write_log(event, **data):

    record = {
        "timestamp": time.time(),
        "event": event,
        **data
    }

    with open(
        LOG_FILE,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(
            json.dumps(
                record,
                ensure_ascii=False
            ) + "\n"
        )


def get_log_url():

    if PUBLIC_BASE_URL:

        return (
            PUBLIC_BASE_URL
            + "/run.jsonl"
        )

    return ""


# ============================================================
# WEB SERVER
# ============================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():

    return (
        "Data Analyst Telegram Bot "
        "is running."
    )


@web_app.route("/run.jsonl")
def get_log():

    if not os.path.exists(
        LOG_FILE
    ):

        open(
            LOG_FILE,
            "a",
            encoding="utf-8"
        ).close()

    return send_file(
        LOG_FILE,
        mimetype="application/jsonl"
    )


def start_web_server():

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    web_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


# ============================================================
# NUMBER ANALYSIS
# ============================================================

def extract_numbers(text):

    matches = re.findall(
        r"(?<![\w.])-?\d+(?:\.\d+)?",
        text
    )

    return [
        float(x)
        for x in matches
    ]


def simple_number_analysis(question):

    numbers = extract_numbers(
        question
    )

    if not numbers:
        return None

    q = question.lower()

    if (
        "average" in q
        or "mean" in q
    ):

        return {
            "average":
                sum(numbers)
                / len(numbers)
        }

    if (
        "sum" in q
        or "total" in q
    ):

        return {
            "sum": sum(numbers)
        }

    if (
        "maximum" in q
        or "highest" in q
        or "largest" in q
        or "max" in q
    ):

        return {
            "maximum": max(numbers)
        }

    if (
        "minimum" in q
        or "lowest" in q
        or "smallest" in q
        or "min" in q
    ):

        return {
            "minimum": min(numbers)
        }

    return None


# ============================================================
# URL EXTRACTION
# ============================================================

def extract_urls(text):

    urls = re.findall(
        r'https?://[^\s<>"\']+',
        text
    )

    cleaned = []

    for url in urls:

        url = url.rstrip(
            ".,);]}"
        )

        if url not in cleaned:

            cleaned.append(url)

    return cleaned


# ============================================================
# CSV ANALYSIS WITHOUT PANDAS
# ============================================================

def csv_to_context(content):

    text = content.decode(
        "utf-8-sig",
        errors="replace"
    )

    reader = csv.DictReader(
        io.StringIO(text)
    )

    rows = []

    for i, row in enumerate(reader):

        if i >= 100:

            break

        rows.append(row)

    columns = (
        reader.fieldnames
        or []
    )

    return {
        "type": "csv",
        "columns": columns,
        "sample_rows": rows,
        "sample_row_count": len(rows)
    }


# ============================================================
# JSON DATA
# ============================================================

def json_to_context(data):

    if isinstance(
        data,
        list
    ):

        return {
            "type": "json",
            "sample": data[:100],
            "count": len(data)
        }

    if isinstance(
        data,
        dict
    ):

        return {
            "type": "json",
            "data": data
        }

    return {
        "type": "json",
        "data": str(data)
    }


# ============================================================
# DOWNLOAD PUBLIC DATA
# ============================================================

def download_url(url):

    write_log(
        "download_started",
        url=url
    )

    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent":
                "Mozilla/5.0"
        }
    )

    response.raise_for_status()

    content = response.content

    content_type = response.headers.get(
        "content-type",
        ""
    ).lower()

    lower_url = url.lower()

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    if (
        lower_url.endswith(".csv")
        or "text/csv" in content_type
    ):

        context = csv_to_context(
            content
        )

        write_log(
            "csv_loaded",
            url=url,
            columns=context[
                "columns"
            ]
        )

        return context

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    if (
        lower_url.endswith(".json")
        or "application/json"
        in content_type
    ):

        data = response.json()

        context = json_to_context(
            data
        )

        write_log(
            "json_loaded",
            url=url
        )

        return context

    # --------------------------------------------------------
    # Try CSV even if extension isn't .csv
    # --------------------------------------------------------

    text = content.decode(
        "utf-8-sig",
        errors="replace"
    )

    if "," in text[:2000]:

        try:

            context = csv_to_context(
                content
            )

            if context[
                "columns"
            ]:

                write_log(
                    "csv_detected",
                    url=url
                )

                return context

        except Exception:

            pass

    # --------------------------------------------------------
    # Web page
    # --------------------------------------------------------

    return {
        "type": "webpage",
        "url": url,
        "text": text[:30000]
    }


# ============================================================
# OPENAI
# ============================================================

def ask_openai(
    question,
    context
):

    prompt = f"""
You are a data-analysis agent.

USER QUESTION:
{question}

DATA / CONTEXT:
{context}

Instructions:

1. Answer the user's actual question.
2. Use the supplied data when available.
3. Do not invent data.
4. Perform calculations accurately.
5. Return ONLY valid JSON.
6. Do not use Markdown.
7. Do not include explanations.
8. Do not include log_url.
9. Follow the JSON structure requested by the user.
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt
    )

    answer = (
        response.output_text
        .strip()
    )

    # Remove accidental Markdown fences
    answer = re.sub(
        r"^```(?:json)?",
        "",
        answer,
        flags=re.IGNORECASE
    )

    answer = re.sub(
        r"```$",
        "",
        answer
    ).strip()

    return json.loads(
        answer
    )


# ============================================================
# MAIN AGENT
# ============================================================

def run_agent(question):

    write_log(
        "agent_started",
        question=question
    )

    # --------------------------------------------------------
    # Simple calculations
    # --------------------------------------------------------

    simple_result = (
        simple_number_analysis(
            question
        )
    )

    if simple_result is not None:

        write_log(
            "simple_analysis",
            result=simple_result
        )

        return simple_result

    # --------------------------------------------------------
    # URLs
    # --------------------------------------------------------

    urls = extract_urls(
        question
    )

    contexts = []

    for url in urls:

        try:

            context = download_url(
                url
            )

            contexts.append(
                context
            )

        except Exception as e:

            write_log(
                "download_error",
                url=url,
                error=str(e)
            )

    # --------------------------------------------------------
    # Build context
    # --------------------------------------------------------

    if contexts:

        context_text = json.dumps(
            contexts,
            ensure_ascii=False,
            default=str
        )

    else:

        context_text = (
            "No external dataset "
            "was supplied."
        )

    # --------------------------------------------------------
    # OpenAI
    # --------------------------------------------------------

    answer = ask_openai(
        question,
        context_text
    )

    write_log(
        "agent_finished",
        answer=answer
    )

    return answer


# ============================================================
# TELEGRAM
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    question = (
        update.message.text
    )

    write_log(
        "received_question",
        question=question
    )

    try:

        answer = run_agent(
            question
        )

        result = {
            "answer": answer,
            "log_url":
                get_log_url()
        }

        write_log(
            "final_answer",
            answer=answer,
            log_url=get_log_url()
        )

        await update.message.reply_text(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(
                    ",",
                    ":"
                )
            )
        )

    except Exception as e:

        print(
            "ERROR:",
            repr(e)
        )

        write_log(
            "error",
            error=str(e)
        )

        result = {
            "answer":
                "Unable to process the question.",
            "log_url":
                get_log_url()
        }

        await update.message.reply_text(
            json.dumps(
                result,
                separators=(
                    ",",
                    ":"
                )
            )
        )


# ============================================================
# START
# ============================================================

def start_telegram_bot():

    print("Telegram bot is starting...")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print("Data analyst bot is running!")

    application.run_polling()


def main():

    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True
    )

    web_thread.start()

    print("Web server started.")

    start_telegram_bot()


# Local execution
if __name__ == "__main__":
    main()

# Render/Gunicorn execution
else:
    telegram_thread = threading.Thread(
        target=start_telegram_bot,
        daemon=True
    )

    telegram_thread.start()

    print("Telegram bot background thread started.")
