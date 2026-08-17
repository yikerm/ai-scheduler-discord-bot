"""Environment-backed application configuration."""

import os

from dotenv import load_dotenv


load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CALENDAR_ID = os.getenv("CALENDAR_ID")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")

BUSY_CALENDAR_IDS = tuple(
    value.strip()
    for value in os.getenv("BUSY_CALENDAR_IDS", "").split(",")
    if value.strip()
)
EXTERNAL_CALENDAR_URLS = tuple(
    value.strip()
    for value in os.getenv("EXTERNAL_CALENDAR_URLS", "").split(",")
    if value.strip()
)

PLANNING_HORIZON_DAYS = int(os.getenv("PLANNING_HORIZON_DAYS", "7"))
REPEAT_HORIZON_DAYS = int(os.getenv("REPEAT_HORIZON_DAYS", "14"))
NLP_PENDING_MINUTES = int(os.getenv("NLP_PENDING_MINUTES", "10"))
MAX_REPLAN_MOVES = int(os.getenv("MAX_REPLAN_MOVES", "3"))
ML_PROGRESSIVE_MIN_FEEDBACK = int(os.getenv("ML_PROGRESSIVE_MIN_FEEDBACK", "20"))
ML_FULL_FEEDBACK = int(os.getenv("ML_FULL_FEEDBACK", "50"))
SCHEDULE_NOTIFICATION_COOLDOWN_HOURS = int(
    os.getenv("SCHEDULE_NOTIFICATION_COOLDOWN_HOURS", "24")
)
