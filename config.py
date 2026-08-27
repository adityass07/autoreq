import os

# ================= MASTER CONFIGURATION =================
API_ID = int(os.getenv("API_ID", "30929822"))
API_HASH = os.getenv("API_HASH", "8586e9580c6480b65d23150cec959506")

# Master Bot Token from @BotFather on Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "8696953804:AAHjufDAPI9QqeI6eMdZO4DEgadb1iE8FoI")

# MongoDB Cloud Database Configuration
MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb+srv://yfree1232_db_user:NtdjPUmAgl7iEuKE@uploder.6fhrdxh.mongodb.net/?appName=Uploder"
)
DB_NAME = os.getenv("DB_NAME", "telegram_join_bot")

# Default Welcome Message template
DEFAULT_MESSAGE = """
Hey **{name}**! 👋

Aapne **{channel}** join karne ki request bheji thi.
Aapka request process ho raha hai! 

Koi question ya help chahiye ho toh aap directly yahan reply kar sakte hain. 😊
"""
