import os
import json
import time
import threading

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

# --------------------------------------------------
# LOAD ENVIRONMENT VARIABLES
# --------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# When deployed, we will put the real public URL here.
# Example:
# PUBLIC_BASE_URL=https://your-bot-name.onrender.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from .env")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing from .env")


# --------------------------------------------------
# OPENAI
# --------------------------------------------------

client = OpenAI(api_key=OPENAI_API_KEY)


# --------------------------------------------------
# LOG FILE
# --------------------------------------------------

LOG_FILE = "run.jsonl"


def write_log(event, **data):
    record = {
        "timestamp": time.time(),
        "event": event,
        **data,
    }

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                record,
                ensure_ascii=False
            ) + "\n"
        )


# --------------------------------------------------
# FLASK WEB SERVER
# --------------------------------------------------

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Data Analyst Telegram Bot is running."


@web_app.route("/run.jsonl")
def get_log():
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "a", encoding="utf-8").close()

    return send_file(
        LOG_FILE,
        mimetype="application/jsonl",
        as_attachment=False
    )


def start_web_server():
    port = int(os.environ.get("PORT", 5000))

    web_app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


# --------------------------------------------------
# TELEGRAM MESSAGE HANDLER
# --------------------------------------------------

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    question = update.message.text

    write_log(
        "received_question",
        question=question
    )

    try:

        # Ask OpenAI to solve the question.
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {
                    "role": "system",
                    "content": """
You are a data-analysis agent.

Answer the user's data-analysis question accurately.

The user's message will normally specify the exact JSON
structure required for the answer.

IMPORTANT RULES:

1. Return ONLY one valid JSON object.
2. Do not use Markdown.
3. Do not add explanations outside the JSON.
4. The top-level JSON object must contain exactly:
   "answer"
   "log_url"

5. The "answer" value must follow the exact structure requested
   by the user.

6. Do NOT invent a log URL.
   The Python program will add the real log URL after you answer.

Example:

User:
Which state has the highest value?
Reply with ONLY this JSON:
{"answer":{"state":"<state name>"},"log_url":"<url>"}

Your internal response should therefore contain only the
answer information in valid JSON format.
""",
                },
                {
                    "role": "user",
                    "content": question
                }
            ]
        )

        raw_answer = response.output_text.strip()

        write_log(
            "openai_response",
            response=raw_answer
        )

        # Remove Markdown code fences if the model accidentally
        # uses them.
        if raw_answer.startswith("```"):
            raw_answer = raw_answer.replace("```json", "")
            raw_answer = raw_answer.replace("```", "")
            raw_answer = raw_answer.strip()

        result = json.loads(raw_answer)

        if "answer" not in result:
            raise ValueError(
                "OpenAI response does not contain 'answer'"
            )

        # --------------------------------------------------
        # CREATE THE REAL LOG URL
        # --------------------------------------------------

        if PUBLIC_BASE_URL:
            log_url = PUBLIC_BASE_URL + "/run.jsonl"
        else:
            # Local development only.
            log_url = ""

        final_result = {
            "answer": result["answer"],
            "log_url": log_url
        }

        write_log(
            "final_answer",
            answer=result["answer"],
            log_url=log_url
        )

        final_text = json.dumps(
            final_result,
            ensure_ascii=False,
            separators=(",", ":")
        )

        await update.message.reply_text(final_text)

    except Exception as e:

        print("ERROR:", e)

        write_log(
            "error",
            error=str(e)
        )

        error_result = {
            "answer": "Unable to process the question.",
            "log_url": (
                PUBLIC_BASE_URL + "/run.jsonl"
                if PUBLIC_BASE_URL
                else ""
            )
        }

        await update.message.reply_text(
            json.dumps(
                error_result,
                separators=(",", ":")
            )
        )


# --------------------------------------------------
# START TELEGRAM BOT
# --------------------------------------------------

def main():

    # Start Flask in a separate thread.
    web_thread = threading.Thread(
        target=start_web_server,
        daemon=True
    )

    web_thread.start()

    print("Web server started.")
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


if __name__ == "__main__":
    main()