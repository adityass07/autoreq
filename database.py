import logging
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_URI, DB_NAME, DEFAULT_MESSAGE

logger = logging.getLogger("Database")

# Initialize MongoDB Async Motor Client
client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]

users_col = db["users"]
logs_col = db["dm_logs"]

async def init_db():
    """Initialize MongoDB indexes and verify connection."""
    try:
        # Ping the server to test connection
        await client.admin.command('ping')
        logger.info("🍃 Successfully connected to MongoDB Atlas!")
        
        # Ensure unique index on user_id and compound index on dm logs
        await users_col.create_index("user_id", unique=True)
        await logs_col.create_index([("owner_id", 1), ("recipient_id", 1)])
    except Exception as e:
        logger.error(f"❌ MongoDB Connection Error: {e}", exc_info=True)
        raise e

async def get_user(user_id: int):
    """Retrieve user document from MongoDB."""
    return await users_col.find_one({"user_id": user_id})

async def save_or_update_session(user_id: int, phone_number: str, session_string: str):
    """Save newly logged in user or update their session in MongoDB."""
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "phone_number": phone_number,
                "session_string": session_string,
                "is_active": 1
            },
            "$setOnInsert": {
                "custom_message": DEFAULT_MESSAGE.strip(),
                "msg_type": "text",
                "media_path": None,
                "msg_1_type": "text",
                "msg_1_content": DEFAULT_MESSAGE.strip(),
                "msg_1_media_path": None,
                "msg_2_enabled": False,
                "msg_2_type": "text",
                "msg_2_content": None,
                "msg_2_media_path": None,
                "auto_approve": 0,
                "min_delay": 0,
                "max_delay": 0,
                "total_dms_sent": 0,
                "target_channels": [],
                "disabled_channels": [],
                "saved_broadcast_channels": [],
                "quick_1_type": "text",
                "quick_1_content": None,
                "quick_1_media_path": None,
                "quick_2_type": "text",
                "quick_2_content": None,
                "quick_2_media_path": None,
                "custom_triggers": {},
                "created_at": datetime.utcnow()
            }
        },
        upsert=True
    )
    logger.info(f"🍃 User {user_id} session saved to MongoDB.")

def normalize_channel_id(ch_id) -> tuple[str, str]:
    """Returns (clean_raw_id, clean_full_id) e.g. ('2148272343', '-1002148272343')."""
    s = str(ch_id).strip()
    if s.startswith("-100"):
        raw = s[4:]
        full = s
    elif s.startswith("-"):
        raw = s[1:]
        full = f"-100{raw}"
    else:
        raw = s
        full = f"-100{s}"
    return raw, full

async def toggle_channel_dm(user_id: int, channel_id: str) -> bool:
    """Toggle a specific channel's DM state. Returns True if now ENABLED (ON), False if DISABLED (OFF)."""
    user = await get_user(user_id)
    disabled = user.get("disabled_channels", []) if user else []
    raw_id, full_id = normalize_channel_id(channel_id)

    # Check if either raw_id or full_id is in disabled
    is_disabled = (raw_id in disabled) or (full_id in disabled)

    if is_disabled:
        # Enable it -> pull both forms
        await users_col.update_one(
            {"user_id": user_id},
            {"$pull": {"disabled_channels": {"$in": [raw_id, full_id, str(channel_id)]}}}
        )
        return True
    else:
        # Disable it -> add both forms
        await users_col.update_one(
            {"user_id": user_id},
            {"$addToSet": {"disabled_channels": {"$each": [raw_id, full_id]}}}
        )
        return False

async def set_all_channels_dm_state(user_id: int, enable_all: bool, all_channel_ids: list[str] | None = None):
    """Enable or disable all channels at once."""
    if enable_all:
        await users_col.update_one(
            {"user_id": user_id},
            {"$set": {"disabled_channels": []}}
        )
    else:
        ids_to_disable = [str(x) for x in (all_channel_ids or [])]
        await users_col.update_one(
            {"user_id": user_id},
            {"$set": {"disabled_channels": ids_to_disable}}
        )

async def add_target_channel(user_id: int, channel_data: dict):
    """Add a channel dict {'id': int/str, 'title': str, 'username': str} to user's target_channels filter."""
    await users_col.update_one(
        {"user_id": user_id},
        {"$pull": {"target_channels": {"id": channel_data["id"]}}}
    )
    await users_col.update_one(
        {"user_id": user_id},
        {"$push": {"target_channels": channel_data}}
    )

async def remove_target_channel(user_id: int, channel_id):
    """Remove a channel from user's target_channels list."""
    await users_col.update_one(
        {"user_id": user_id},
        {"$pull": {"target_channels": {"id": channel_id}}}
    )

async def clear_target_channels(user_id: int):
    """Reset target channels filter to empty list (All Channels mode)."""
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"target_channels": []}}
    )

async def save_default_broadcast_channels(user_id: int, channel_ids: list[str]):
    """Save user's default broadcast channels preset in MongoDB."""
    ids = [str(x).replace("-100", "") for x in channel_ids]
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"saved_broadcast_channels": ids}}
    )

async def get_default_broadcast_channels(user_id: int) -> list[str]:
    """Get user's default saved broadcast channels preset."""
    user = await get_user(user_id)
    return user.get("saved_broadcast_channels", []) if user else []

async def update_custom_message(user_id: int, message: str):
    """Update custom welcome text message for a user in MongoDB."""
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "msg_type": "text",
                "custom_message": message,
                "media_path": None,
                "msg_1_type": "text",
                "msg_1_content": message,
                "msg_1_media_path": None
            }
        }
    )

async def update_custom_message_data(user_id: int, msg_type: str, content: str, media_path: str | None = None):
    """Update custom message type, text/caption, and media path in MongoDB."""
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "msg_type": msg_type,
                "custom_message": content,
                "media_path": media_path,
                "msg_1_type": msg_type,
                "msg_1_content": content,
                "msg_1_media_path": media_path
            }
        }
    )

async def update_custom_message_slot(user_id: int, slot: int, msg_type: str, content: str, media_path: str | None = None):
    """Update custom message slot (1 or 2) in MongoDB."""
    if slot == 1:
        await users_col.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "msg_type": msg_type,
                    "custom_message": content,
                    "media_path": media_path,
                    "msg_1_type": msg_type,
                    "msg_1_content": content,
                    "msg_1_media_path": media_path
                }
            }
        )
    elif slot == 2:
        await users_col.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "msg_2_enabled": True,
                    "msg_2_type": msg_type,
                    "msg_2_content": content,
                    "msg_2_media_path": media_path
                }
            }
        )

async def clear_custom_message_slot_2(user_id: int):
    """Disable and clear Message Slot 2."""
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "msg_2_enabled": False,
                "msg_2_type": "text",
                "msg_2_content": None,
                "msg_2_media_path": None
            }
        }
    )

async def update_quick_shortcut_slot(user_id: int, slot: int, msg_type: str, content: str, media_path: str | None = None):
    """Update dedicated DM quick shortcut slot 1 or 2 in MongoDB."""
    field_prefix = f"quick_{slot}"
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                f"{field_prefix}_type": msg_type,
                f"{field_prefix}_content": content,
                f"{field_prefix}_media_path": media_path
            }
        }
    )

async def clear_quick_shortcut_slot(user_id: int, slot: int):
    """Clear dedicated DM quick shortcut slot."""
    field_prefix = f"quick_{slot}"
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                f"{field_prefix}_type": "text",
                f"{field_prefix}_content": None,
                f"{field_prefix}_media_path": None
            }
        }
    )

def get_quick_shortcut_data(user_data: dict | None, slot: int) -> dict:
    """Extract quick shortcut data for a slot (1-10) with backward compatibility."""
    if not user_data:
        return {"type": "text", "content": None, "media_path": None, "is_set": False}
    
    q_type = user_data.get(f"quick_{slot}_type") or "text"
    q_content = user_data.get(f"quick_{slot}_content")
    q_media = user_data.get(f"quick_{slot}_media_path")
    
    # Graceful fallback for slot 1 if user never set quick_1 specifically
    if slot == 1 and not q_content and not q_media:
        q_type = user_data.get("msg_1_type") or user_data.get("msg_type", "text")
        q_content = user_data.get("msg_1_content") or user_data.get("custom_message")
        q_media = user_data.get("msg_1_media_path") or user_data.get("media_path")
        
    is_set = bool(q_content or q_media)
    return {
        "type": q_type,
        "content": q_content,
        "media_path": q_media,
        "is_set": is_set
    }

async def add_or_update_custom_trigger(user_id: int, trigger_key: str, msg_type: str, content: str, media_path: str | None = None):
    """Add or update an A-to-Z / custom keyword trigger in MongoDB."""
    clean_key = str(trigger_key).lower().strip().lstrip(".")
    await users_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                f"custom_triggers.{clean_key}": {
                    "type": msg_type,
                    "content": content,
                    "media_path": media_path
                }
            }
        }
    )

async def delete_custom_trigger(user_id: int, trigger_key: str):
    """Delete a custom trigger from MongoDB."""
    clean_key = str(trigger_key).lower().strip().lstrip(".")
    await users_col.update_one(
        {"user_id": user_id},
        {"$unset": {f"custom_triggers.{clean_key}": ""}}
    )

async def get_all_custom_triggers(user_id: int) -> dict:
    """Get all custom A-Z/keyword triggers for a user."""
    user = await get_user(user_id)
    if not user:
        return {}
    return user.get("custom_triggers", {})

async def toggle_auto_approve(user_id: int) -> bool:
    """Toggle Auto-Approve on/off for a user."""
    user = await get_user(user_id)
    current_state = user.get("auto_approve", 0) if user else 0
    new_state = 0 if current_state == 1 else 1

    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"auto_approve": new_state}}
    )
    return bool(new_state)

async def toggle_active(user_id: int) -> bool:
    """Toggle automation Active / Paused state for a user."""
    user = await get_user(user_id)
    current_state = user.get("is_active", 1) if user else 1
    new_state = 0 if current_state == 1 else 1

    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"is_active": new_state}}
    )
    return bool(new_state)

async def update_delay(user_id: int, min_delay: int, max_delay: int):
    """Update anti-flood delay range for a user."""
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"min_delay": min_delay, "max_delay": max_delay}}
    )

async def log_dm_sent(owner_id: int, recipient_id: int, recipient_name: str, chat_title: str):
    """Record a sent DM log in MongoDB and increment counter."""
    await logs_col.insert_one({
        "owner_id": owner_id,
        "recipient_id": recipient_id,
        "recipient_name": recipient_name,
        "chat_title": chat_title,
        "timestamp": datetime.utcnow()
    })
    await users_col.update_one(
        {"user_id": owner_id},
        {"$inc": {"total_dms_sent": 1}}
    )

async def has_already_received_dm(owner_id: int, recipient_id: int) -> bool:
    """Check if recipient has already received a DM from this account to strictly prevent duplicate spam."""
    doc = await logs_col.find_one({
        "owner_id": owner_id,
        "recipient_id": recipient_id
    })
    return doc is not None

async def delete_user_session(user_id: int):
    """Delete user session on logout from MongoDB."""
    await users_col.delete_one({"user_id": user_id})
    logger.info(f"🍃 User {user_id} removed from MongoDB.")

async def get_all_active_users():
    """Retrieve all active users on bot startup from MongoDB."""
    cursor = users_col.find({"is_active": 1, "session_string": {"$ne": None}})
    return await cursor.to_list(length=None)
