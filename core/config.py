import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]

OWNER_IDS = {int(userId) for userId in os.getenv("OWNER_IDS", "").split(",") if userId.strip()}

DEFAULT_MANAGER_ROLE_IDS = {int(role_id) for role_id in os.getenv("MANAGER_ROLE_IDS", "").split(",") if role_id.strip()}

CATEGORY_NAME = "Events"
FORUM_NAME = "event-organisation"
SOCIAL_FORUM_NAME = "socials-organisation"

TIMEZONE = "Europe/London"
DB_PATH = os.getenv("DB_PATH", "data/logibot.db")

SIGNUP_CLOSE_HOUR = 9
QUESTION_TIMEOUT = 600

ACTION_LOG_CHANNEL_NAME = "action-log"
LOG_CHANNEL_NAME = "event-officers"

EVENT_RETENTION_DAYS = 14
PROFILE_RETENTION_DAYS = 365
BACKUP_DIR = os.getenv("BACKUP_DIR", "data/backups")
BACKUP_KEEP = 14
BACKUP_EVERY_HOURS = 6
DRAFT_TIMEOUT_HOURS = int(os.getenv("DRAFT_TIMEOUT_HOURS", "48"))
PRESET_DIR = os.getenv("PRESET_DIR", "preset_files")
