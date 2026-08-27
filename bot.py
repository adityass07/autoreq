import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    FloodWaitError
)

from config import BOT_TOKEN, API_ID, API_HASH, DEFAULT_MESSAGE
import database
import client_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MasterBot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()

# User state tracker: {user_id: {"step": "WAITING_PHONE" | "WAITING_OTP" | "WAITING_2FA" | "WAITING_CUSTOM_MSG" | "WAITING_SESSION_STRING", "client": TelegramClient, ...}}
user_states: dict[int, dict] = {}

def create_telethon_auth_client() -> TelegramClient:
    """Create Telethon client with official Desktop fingerprint."""
    return TelegramClient(
        session=StringSession(),
        api_id=API_ID,
        api_hash=API_HASH,
        device_model="Telegram Desktop",
        system_version="Windows 11 x64",
        app_version="5.4.1 x64",
        lang_code="en",
        system_lang_code="en-US"
    )

async def cleanup_auth_state(user_id: int):
    """Safely disconnect and cleanup temporary login client."""
    state = user_states.pop(user_id, None)
    if state and state.get("client"):
        try:
            client = state["client"]
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass

def build_channels_hub_keyboard(channels: list[dict], disabled: list[str]) -> InlineKeyboardMarkup:
    """Generate dynamic colorful channel toggle grid with Bot API 9.4 styles."""
    buttons = []
    
    # Bulk actions on top
    buttons.append([
        InlineKeyboardButton(text="⚡ Turn ALL ON", callback_data="ch_all_on", style="success"),
        InlineKeyboardButton(text="⏸️ Turn ALL OFF", callback_data="ch_all_off", style="danger")
    ])

    if not channels:
        buttons.append([InlineKeyboardButton(text="⚠️ Koi Channel nahi mila (Check Admin/Owner)", callback_data="btn_channels_hub", style="default")])
    else:
        for ch in channels:
            raw_id_str = str(ch.get("raw_id", ch.get("id")))
            full_id_str = str(ch.get("id"))
            is_off = (raw_id_str in disabled) or (full_id_str in disabled)
            role_icon = "👑" if ch.get("is_owner") else "🛡️"
            
            ch_title = ch.get("title", "Channel")
            if len(ch_title) > 18:
                ch_title = ch_title[:18] + ".."

            if is_off:
                btn_text = f"🔴 {role_icon} {ch_title} [OFF]"
                btn_style = "danger"
            else:
                btn_text = f"🟢 {role_icon} {ch_title} [ON]"
                btn_style = "success"

            buttons.append([
                InlineKeyboardButton(text=btn_text, callback_data=f"tog_ch_{raw_id_str}", style=btn_style)
            ])

    buttons.append([
        InlineKeyboardButton(text="🔄 Refresh List", callback_data="btn_channels_hub", style="primary"),
        InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="btn_menu", style="default")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_broadcast_channels_keyboard(channels: list[dict], selected_ids: list[str], has_saved_preset: bool = True) -> InlineKeyboardMarkup:
    """Build multi-select channel toggle grid for Broadcast with Preset controls and Bot API 9.4 styles."""
    buttons = []
    
    # Bulk Select / Deselect All
    buttons.append([
        InlineKeyboardButton(text="⚡ Select ALL", callback_data="bc_all_sel", style="success"),
        InlineKeyboardButton(text="⏸️ Deselect ALL", callback_data="bc_all_desel", style="default")
    ])

    # Default Preset Controls
    preset_row = [
        InlineKeyboardButton(text="💾 Save Preset", callback_data="bc_save_preset", style="primary")
    ]
    if has_saved_preset:
        preset_row.append(InlineKeyboardButton(text="🔁 Use Saved Preset", callback_data="bc_load_preset", style="success"))
    buttons.append(preset_row)

    if not channels:
        buttons.append([
            InlineKeyboardButton(text="⚠️ Koi Admin/Owner Channel nahi mila", callback_data="noop", style="default")
        ])
    else:
        for ch in channels:
            role_icon = "👑" if ch.get("is_owner") else "🛡️"
            ch_title = ch.get("title", "Channel")
            raw_id_str = str(ch.get("raw_id", ch.get("id"))).replace("-100", "")
            full_id_str = f"-100{raw_id_str}"
            
            # Check if this channel is selected
            is_selected = (raw_id_str in selected_ids) or (full_id_str in selected_ids)
            
            if len(ch_title) > 18:
                ch_title = ch_title[:18] + ".."

            if is_selected:
                btn_text = f"✅ {role_icon} {ch_title}"
                btn_style = "success"
            else:
                btn_text = f"⬜ {role_icon} {ch_title}"
                btn_style = "default"

            buttons.append([
                InlineKeyboardButton(text=btn_text, callback_data=f"bc_tog_{raw_id_str}", style=btn_style)
            ])

    # Send button with live counter
    sel_count = len(selected_ids)
    send_text = f"🚀 Send Broadcast Now ({sel_count} Selected)"
    send_style = "primary" if sel_count > 0 else "default"
    
    buttons.append([
        InlineKeyboardButton(text=send_text, callback_data="bc_send_now", style=send_style)
    ])
    buttons.append([
        InlineKeyboardButton(text="🔄 New Message", callback_data="btn_broadcast_menu", style="default"),
        InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="btn_menu", style="default")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_quick_triggers_dashboard(user_data: dict | None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate Dashboard text and interactive keyboard for A-Z & 1-10 shortcuts."""
    custom_triggers = (user_data.get("custom_triggers") or {}) if user_data else {}
    
    # 1. Custom A-Z & Word Triggers
    trigger_lines = []
    if custom_triggers:
        for k, v in sorted(custom_triggers.items()):
            t_type = v.get("type", "text").upper()
            c_prev = str(v.get("content") or "")[:22]
            trigger_lines.append(f"• 🔹 `{k}` *(or `.{k}`)* ➔ 🟢 `{t_type}` ({c_prev}..)")
    
    # 2. Numbered 1-10 Slots
    slots_summary = []
    for s in range(1, 11):
        s_data = database.get_quick_shortcut_data(user_data, s)
        if s_data["is_set"]:
            c_preview = str(s_data["content"] or "")[:20]
            slots_summary.append(f"• `.{s}` *(or `{s}`)* ➔ 🟢 `{s_data['type'].upper()}` ({c_preview}..)")
        else:
            slots_summary.append(f"• `.{s}` ➔ ⚪ *[Not Set]*")

    cust_section = "\n".join(trigger_lines) if trigger_lines else "*(Abhi koi A-Z custom shortcut nahi bana. Niche ➕ button dabayein)*"
    num_section = "\n".join(slots_summary[:6])

    text = (
        "⚡ **A-to-Z & Custom DM Shortcuts Dashboard**\n\n"
        "*(💡 Ye shortcuts **Channel Join Auto-DM se bilkul ALAG** hain. DM me typing karte waqt **'.' lagane ki zaroorat nahi hai** — jaise direct `a` ya `loss` ya `.a` likhne par bhi turant message chala jayega!)*\n\n"
        "🔤 **A to Z & Custom Keyword Shortcuts:**\n"
        f"{cust_section}\n\n"
        "🔢 **Numbered Quick Slots (.1 to .10):**\n"
        f"{num_section}\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "👇 **Neeche diye gaye buttons se naya A-Z shortcut banayein ya number slot edit karein:**"
    )

    buttons = [
        [
            InlineKeyboardButton(text="➕ Add Custom A-Z / Keyword Shortcut", callback_data="btn_add_custom_trigger", style="success")
        ],
        [
            InlineKeyboardButton(text="1️⃣ Set .1", callback_data="btn_edit_quick_1", style="primary"),
            InlineKeyboardButton(text="2️⃣ Set .2", callback_data="btn_edit_quick_2", style="primary"),
            InlineKeyboardButton(text="3️⃣ Set .3", callback_data="btn_edit_quick_3", style="primary")
        ],
        [
            InlineKeyboardButton(text="4️⃣ Set .4", callback_data="btn_edit_quick_4", style="primary"),
            InlineKeyboardButton(text="5️⃣ Set .5", callback_data="btn_edit_quick_5", style="primary"),
            InlineKeyboardButton(text="6️⃣ Set .6", callback_data="btn_edit_quick_6", style="primary")
        ],
        [
            InlineKeyboardButton(text="7️⃣ Set .7", callback_data="btn_edit_quick_7", style="primary"),
            InlineKeyboardButton(text="8️⃣ Set .8", callback_data="btn_edit_quick_8", style="primary"),
            InlineKeyboardButton(text="9️⃣ Set .9", callback_data="btn_edit_quick_9", style="primary")
        ],
        [
            InlineKeyboardButton(text="🔟 Set .10", callback_data="btn_edit_quick_10", style="primary"),
            InlineKeyboardButton(text="🗑️ Delete a Shortcut", callback_data="btn_clear_quick_menu", style="danger")
        ],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="btn_menu", style="default")]
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)

def get_main_keyboard(user_data: dict | None) -> InlineKeyboardMarkup:
    """Generate dynamic interactive keyboard based on user's login & active status with Bot API 9.4 styles."""
    if not user_data or not user_data.get("session_string"):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Phone Login (Fast & Safe OTP)", callback_data="btn_login", style="primary")],
            [InlineKeyboardButton(text="🔑 Paste Session String (Direct)", callback_data="btn_session_login", style="success")],
            [InlineKeyboardButton(text="ℹ️ How it Works & Help", callback_data="btn_help", style="default")]
        ])

    is_active = bool(user_data.get("is_active", 1))
    auto_approve = bool(user_data.get("auto_approve", 0))

    active_btn = "⏸️ Pause Automation" if is_active else "▶️ Start Automation"
    active_style = "danger" if is_active else "success"

    approve_btn = "⚡ Auto-Approve: ON ✅" if auto_approve else "⚡ Auto-Approve: OFF ❌"
    approve_style = "success" if auto_approve else "default"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=active_btn, callback_data="btn_toggle_active", style=active_style)],
        [
            InlineKeyboardButton(text="✍️ Edit Custom Message", callback_data="btn_custom_msg", style="primary"),
            InlineKeyboardButton(text=approve_btn, callback_data="btn_toggle_approve", style=approve_style)
        ],
        [
            InlineKeyboardButton(text="📢 Channels Auto-DM (ON/OFF 🔘)", callback_data="btn_channels_hub", style="primary"),
            InlineKeyboardButton(text="⏱️ Anti-Ban Delay", callback_data="btn_delay_menu", style="primary")
        ],
        [
            InlineKeyboardButton(text="📡 Broadcast to Channels (Multi-Post 🚀)", callback_data="btn_broadcast_menu", style="success")
        ],
        [
            InlineKeyboardButton(text="⚡ A-Z & Custom DM Shortcuts 🚀", callback_data="btn_quick_triggers", style="primary"),
            InlineKeyboardButton(text="🔗 Generate Join Link", callback_data="btn_gen_link", style="primary")
        ],
        [
            InlineKeyboardButton(text="📊 Stats & Info", callback_data="btn_stats", style="primary"),
            InlineKeyboardButton(text="ℹ️ Tags Guide", callback_data="btn_tags_guide", style="default")
        ],
        [
            InlineKeyboardButton(text="🚪 Logout Account", callback_data="btn_logout_confirm", style="danger")
        ]
    ])

@dp.message(F.text.startswith("/start") | F.text.startswith("/menu") | F.text.startswith("/help"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    logger.info(f"📩 Received /start from {user_id} ({message.from_user.first_name})")
    await cleanup_auth_state(user_id)

    user_data = await database.get_user(user_id)

    if user_data and user_data.get("session_string"):
        phone = user_data.get("phone_number", "Logged In")
        status = "🟢 Active (Sending Auto-DMs)" if user_data.get("is_active", 1) else "🟡 Paused"
        total_dms = user_data.get("total_dms_sent", 0)

        text = (
            f"👋 **Welcome back, {message.from_user.first_name}!**\n\n"
            f"📱 **Connected ID:** `{phone}`\n"
            f"📡 **Status:** {status}\n"
            f"✉️ **Total DMs Sent:** `{total_dms}`\n\n"
            f"Neeche diye gaye buttons se apni settings control karein 👇"
        )
    else:
        text = (
            f"👋 **Namaste {message.from_user.first_name}!**\n\n"
            f"Ye **Join Request Auto-DM Bot** hai.\n"
            f"Jab bhi koi aapke channel/group me join request bhejega, ye automatically aapki personal ID se unko DM bhej dega!\n\n"
            f"🚀 **Shuru karne ke liye apna account connect karein:**"
        )

    await message.answer(text, reply_markup=get_main_keyboard(user_data))

@dp.callback_query()
async def callback_handler(query: types.CallbackQuery):
    user_id = query.from_user.id
    data = query.data
    logger.info(f"🔘 Button clicked: '{data}' by user {user_id}")

    user_data = await database.get_user(user_id)

    try:
        # 1. Main Menu
        if data == "btn_menu":
            await cleanup_auth_state(user_id)
            user_data = await database.get_user(user_id)
            try:
                await query.message.edit_text(
                    "🏠 **Main Control Dashboard**\n\nNeeche diye gaye buttons se control karein:",
                    reply_markup=get_main_keyboard(user_data)
                )
            except TelegramBadRequest:
                pass
            await query.answer()

        # 2. Start Phone Login Flow
        elif data == "btn_login":
            if user_data and user_data.get("session_string"):
                await query.answer("Aapka account pehle se connected hai!", show_alert=True)
                return

            await cleanup_auth_state(user_id)
            user_states[user_id] = {"step": "WAITING_PHONE", "client": None}
            text = (
                "📲 **Telegram Phone (OTP) Login**\n\n"
                "Apna Telegram Phone Number country code ke sath yahan bhejein:\n"
                "*(Example: `+919876543210` ya `+19127418551`)*"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Paste Session String Instead", callback_data="btn_session_login", style="success")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="btn_menu", style="danger")]
            ])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 3. Direct Session String Login
        elif data == "btn_session_login":
            if user_data and user_data.get("session_string"):
                await query.answer("Aapka account pehle se connected hai!", show_alert=True)
                return

            await cleanup_auth_state(user_id)
            user_states[user_id] = {"step": "WAITING_SESSION_STRING"}
            text = (
                "🔑 **Direct Session String Login (1-Click & Safe)**\n\n"
                "Apna **Telethon ya Pyrogram Session String** yahan chat me paste karke bhejein.\n\n"
                "*(💡 Is method se koi OTP request nahi hoti aur Telegram ka koi delay warning nahi aata!)*"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="btn_menu", style="danger")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 4. Toggle Active / Pause
        elif data == "btn_toggle_active":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return
            
            new_active = await database.toggle_active(user_id)
            if new_active:
                session = user_data.get("session_string")
                if session:
                    asyncio.create_task(client_manager.start_client(user_id, session))
                await query.answer("▶️ Automation Start ho gaya!", show_alert=False)
            else:
                await client_manager.stop_client(user_id)
                await query.answer("⏸️ Automation Pause ho gaya!", show_alert=False)

            user_data = await database.get_user(user_id)
            try:
                await query.message.edit_reply_markup(reply_markup=get_main_keyboard(user_data))
            except TelegramBadRequest:
                pass

        # 5. Toggle Auto-Approve
        elif data == "btn_toggle_approve":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            new_approve = await database.toggle_auto_approve(user_id)
            status_txt = "ON ✅ (DM bhejte hi request accept ho jayegi)" if new_approve else "OFF ❌ (Request pending rahegi)"
            await query.answer(f"Auto-Approve: {status_txt}", show_alert=True)

            user_data = await database.get_user(user_id)
            try:
                await query.message.edit_reply_markup(reply_markup=get_main_keyboard(user_data))
            except TelegramBadRequest:
                pass

        # 6. Channels Auto-DM Toggle Hub (Colorful Buttons)
        elif data == "btn_channels_hub":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            channels = await client_manager.get_user_admin_channels(user_id)
            disabled = user_data.get("disabled_channels", [])
            
            active_count = len(channels) - sum(1 for ch in channels if str(ch.get("raw_id")) in disabled or str(ch.get("id")) in disabled)
            owner_count = sum(1 for ch in channels if ch.get("is_owner"))
            admin_count = len(channels) - owner_count

            text = (
                "📢 **Channels Auto-DM Toggle Dashboard**\n\n"
                f"👑 **Owner Channels:** `{owner_count}` | 🛡️ **Admin Channels:** `{admin_count}`\n"
                f"📊 **Active Status:** `{active_count} / {len(channels)} ON`\n\n"
                "👇 **Kripya kisi bhi channel par tap karke uska Auto-DM ON/OFF karein:**\n"
                "• 🟢 **GREEN [ON]:** Is channel ki request par instant DM jayega.\n"
                "• 🔴 **RED [OFF]:** Is channel ki request ko bot ignore karega."
            )
            keyboard = build_channels_hub_keyboard(channels, disabled)
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Toggle individual channel ON / OFF
        elif data.startswith("tog_ch_"):
            raw_id = data.replace("tog_ch_", "")
            is_now_on = await database.toggle_channel_dm(user_id, raw_id)
            status_txt = "🟢 Auto-DM ON ho gaya!" if is_now_on else "🔴 Auto-DM OFF ho gaya!"
            await query.answer(status_txt, show_alert=False)

            user_data = await database.get_user(user_id)
            channels = await client_manager.get_user_admin_channels(user_id)
            disabled = user_data.get("disabled_channels", [])
            active_count = len(channels) - sum(1 for ch in channels if str(ch.get("raw_id")) in disabled or str(ch.get("id")) in disabled)
            owner_count = sum(1 for ch in channels if ch.get("is_owner"))
            admin_count = len(channels) - owner_count

            text = (
                "📢 **Channels Auto-DM Toggle Dashboard**\n\n"
                f"👑 **Owner Channels:** `{owner_count}` | 🛡️ **Admin Channels:** `{admin_count}`\n"
                f"📊 **Active Status:** `{active_count} / {len(channels)} ON`\n\n"
                "👇 **Kripya kisi bhi channel par tap karke uska Auto-DM ON/OFF karein:**\n"
                "• 🟢 **GREEN [ON]:** Is channel ki request par instant DM jayega.\n"
                "• 🔴 **RED [OFF]:** Is channel ki request ko bot ignore karega."
            )
            keyboard = build_channels_hub_keyboard(channels, disabled)
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass

        # Turn all channels ON
        elif data == "ch_all_on":
            await database.set_all_channels_dm_state(user_id, enable_all=True)
            await query.answer("⚡ Sabhi Channels ka Auto-DM ON ho gaya!", show_alert=True)
            user_data = await database.get_user(user_id)
            channels = await client_manager.get_user_admin_channels(user_id)
            keyboard = build_channels_hub_keyboard(channels, [])
            try:
                await query.message.edit_text(
                    "📢 **Channels Auto-DM Dashboard**\n\n⚡ Sabhi Channels **🟢 [ON]** ho chuke hain!",
                    reply_markup=keyboard
                )
            except TelegramBadRequest:
                pass

        # Turn all channels OFF
        elif data == "ch_all_off":
            channels = await client_manager.get_user_admin_channels(user_id)
            all_ids = [str(ch.get("raw_id", ch.get("id"))) for ch in channels]
            await database.set_all_channels_dm_state(user_id, enable_all=False, all_channel_ids=all_ids)
            await query.answer("⏸️ Sabhi Channels ka Auto-DM OFF ho gaya!", show_alert=True)
            user_data = await database.get_user(user_id)
            disabled = user_data.get("disabled_channels", [])
            keyboard = build_channels_hub_keyboard(channels, disabled)
            try:
                await query.message.edit_text(
                    "📢 **Channels Auto-DM Dashboard**\n\n⏸️ Sabhi Channels **🔴 [OFF]** ho chuke hain!",
                    reply_markup=keyboard
                )
            except TelegramBadRequest:
                pass

        # 7. Target Channels Filter Menu
        elif data == "btn_target_channels":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            targets = user_data.get("target_channels") or []
            if targets:
                mode_str = f"🎯 **Specific Channels Mode ({len(targets)} Channels Active)**"
                lines = []
                for idx, t in enumerate(targets, 1):
                    t_title = t.get("title", "Channel")
                    t_user = f" ({t.get('username')})" if t.get("username") else ""
                    lines.append(f"{idx}. **{t_title}**{t_user} `[{t.get('id')}]`")
                channels_list_str = "\n".join(lines)
            else:
                mode_str = "🌐 **All Channels Mode (Default)**"
                channels_list_str = "• *Aapki ID jis bhi channel me Admin hai, un sabhi channels par Auto-DM kaam kar raha hai.*"

            text = (
                "🎯 **Target Channels Filter Settings**\n\n"
                f"📊 **Current Mode:** {mode_str}\n\n"
                f"📋 **Active Channels List:**\n{channels_list_str}\n\n"
                "💡 **Kaise Kaam Karta Hai:**\n"
                "Agar aap yahan koi channel add karte hain, toh bot **sirf usi channel ki join requests par DM bhejega**, baki channels ko ignore karega!"
            )

            buttons = [
                [InlineKeyboardButton(text="➕ Add Target Channel", callback_data="btn_add_target_channel", style="success")]
            ]
            if targets:
                for t in targets:
                    t_name = t.get("title", "Channel")[:18]
                    buttons.append([InlineKeyboardButton(text=f"🗑️ Remove: {t_name}", callback_data=f"del_channel_{t.get('id')}", style="danger")])
                buttons.append([InlineKeyboardButton(text="🌐 Reset to All Channels", callback_data="btn_reset_target_channels", style="primary")])

            buttons.append([InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="btn_menu", style="default")])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 7. Add Target Channel Prompt
        elif data == "btn_add_target_channel":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            user_states[user_id] = {"step": "WAITING_ADD_TARGET_CHANNEL"}
            text = (
                "➕ **Add Target Channel to Filter**\n\n"
                "Apne Channel ka **@username** (jaise `@mychannel`) ya **Channel ID** (`-100...`) yahan chat me bhejein:\n\n"
                "*(💡 Aapki connected ID us channel me Admin honi chahiye)*"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="btn_target_channels", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 8. Reset Target Channels Filter
        elif data == "btn_reset_target_channels":
            await database.clear_target_channels(user_id)
            await query.answer("Filter reset! Ab sabhi channels par Auto-DM chalega.", show_alert=True)
            user_data = await database.get_user(user_id)
            try:
                await query.message.edit_text("✅ Filter reset ho gaya! Ab sabhi channels ki requests par DM jayega.", reply_markup=get_main_keyboard(user_data))
            except TelegramBadRequest:
                pass

        # 9. Delete Specific Target Channel
        elif data.startswith("del_channel_"):
            ch_id = data.replace("del_channel_", "")
            await database.remove_target_channel(user_id, ch_id)
            await query.answer("Channel filter se remove ho gaya!", show_alert=True)
            user_data = await database.get_user(user_id)
            # Re-trigger target channels menu
            targets = user_data.get("target_channels") or []
            if targets:
                mode_str = f"🎯 **Specific Channels Mode ({len(targets)} Channels Active)**"
                lines = [f"{idx}. **{t.get('title', 'Channel')}** `[{t.get('id')}]`" for idx, t in enumerate(targets, 1)]
                channels_list_str = "\n".join(lines)
            else:
                mode_str = "🌐 **All Channels Mode (Default)**"
                channels_list_str = "• *Aapki ID jis bhi channel me Admin hai, un sabhi channels par Auto-DM kaam kar raha hai.*"

            text = (
                "🎯 **Target Channels Filter Settings**\n\n"
                f"📊 **Current Mode:** {mode_str}\n\n"
                f"📋 **Active Channels List:**\n{channels_list_str}"
            )
            buttons = [[InlineKeyboardButton(text="➕ Add Target Channel", callback_data="btn_add_target_channel", style="success")]]
            if targets:
                for t in targets:
                    t_name = t.get("title", "Channel")[:18]
                    buttons.append([InlineKeyboardButton(text=f"🗑️ Remove: {t_name}", callback_data=f"del_channel_{t.get('id')}", style="danger")])
                buttons.append([InlineKeyboardButton(text="🌐 Reset to All Channels", callback_data="btn_reset_target_channels", style="primary")])
            buttons.append([InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="btn_menu", style="default")])
            try:
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            except TelegramBadRequest:
                pass

        # 10. Generate Join Link
        elif data == "btn_gen_link":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            user_states[user_id] = {"step": "WAITING_CHANNEL_LINK"}
            text = (
                "🔗 **Generate Join Request Invite Link**\n\n"
                "Apne Channel/Group ka **Username** ya **Link** ya **ID** yahan chat me bhejein:\n"
                "*(Example: `@mychannel` ya `https://t.me/mychannel` ya `-1001234567890`)*\n\n"
                "💡 *Note: Aapki connected ID us channel me Admin honi chahiye.*"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="btn_menu", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 11. Dual-Message Custom DM Dashboard
        elif data == "btn_custom_msg":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            msg_1_type = (user_data.get("msg_1_type") or user_data.get("msg_type", "text")).upper()
            msg_1_content = user_data.get("msg_1_content") or user_data.get("custom_message") or DEFAULT_MESSAGE
            
            msg_2_enabled = user_data.get("msg_2_enabled", False)
            msg_2_type = (user_data.get("msg_2_type") or "text").upper()
            msg_2_content = user_data.get("msg_2_content") or user_data.get("msg_2_media_path") or "*(Message 2 Set nahi hai - Sirf 1 message jayega)*"
            
            is_msg_2_active = bool(msg_2_enabled and (user_data.get("msg_2_content") or user_data.get("msg_2_media_path")))
            status_2 = "🟢 Active (1st ke turant baad jayega)" if is_msg_2_active else "⚪ Disabled / Not Set"

            text = (
                "✍️ **Multi-Message Auto-DM Dashboard**\n\n"
                "Aap yahan **2 Alag-Alag Messages** set kar sakte hain jo user ko sequence me jayenge (First Msg 1, then Msg 2):\n\n"
                "1️⃣ **Message 1 (First DM):**\n"
                f"• Type: `{msg_1_type}` | Status: `🟢 Active`\n"
                f"• Content: {str(msg_1_content)[:70]}...\n\n"
                "2️⃣ **Message 2 (Follow-up DM - Turant Baad):**\n"
                f"• Type: `{msg_2_type}` | Status: `{status_2}`\n"
                f"• Content: {str(msg_2_content)[:70]}...\n\n"
                "📌 **Supported Formats:**\n"
                "• 🎤 Voice Notes & 📹 Round Video Notes\n"
                "• 📝 Text with Custom Premium Emojis & Links\n"
                "• 🎭 Telegram Premium Animated Stickers\n"
                "• 🖼️ Photos / Videos with Formatted Captions\n"
                "• Tags: `{name}`, `{channel}`, `{username}`, `{full_name}`"
            )

            buttons = [
                [InlineKeyboardButton(text="1️⃣ Edit Message 1 (First Msg)", callback_data="btn_edit_slot_1", style="primary")],
                [InlineKeyboardButton(text="2️⃣ Edit Message 2 (Second Msg)", callback_data="btn_edit_slot_2", style="primary")]
            ]
            if is_msg_2_active:
                buttons.append([InlineKeyboardButton(text="🗑️ Remove Message 2 (Send Only 1 Msg)", callback_data="btn_clear_slot_2", style="danger")])
            
            buttons.append([InlineKeyboardButton(text="🔄 Reset to Default 1 Message", callback_data="btn_reset_msg", style="default")])
            buttons.append([InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="btn_menu", style="default")])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Edit Slot 1
        elif data == "btn_edit_slot_1":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            user_states[user_id] = {"step": "WAITING_CUSTOM_MSG", "slot": 1}
            text = (
                "1️⃣ **Editing Message 1 (First DM)**\n\n"
                "Apna **1st Message** yahan chat me bhejein:\n"
                "• 📝 Text Message (Custom TG Emojis/Links supported)\n"
                "• 🎭 Telegram Premium Animated Sticker\n"
                "• 🖼️ Photo with Caption\n"
                "• 🎬 Video with Caption\n\n"
                "📌 Tags: `{name}`, `{channel}`, `{username}`, `{full_name}`"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="btn_custom_msg", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Edit Slot 2
        elif data == "btn_edit_slot_2":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            user_states[user_id] = {"step": "WAITING_CUSTOM_MSG", "slot": 2}
            text = (
                "2️⃣ **Editing Message 2 (Follow-up DM)**\n\n"
                "Apna **2nd Message** yahan chat me bhejein *(ye Message 1 ke turant baad jayega)*:\n"
                "• 📝 Text Message (Custom TG Emojis/Links supported)\n"
                "• 🎭 Telegram Premium Animated Sticker\n"
                "• 🖼️ Photo with Caption\n"
                "• 🎬 Video with Caption\n\n"
                "📌 Tags: `{name}`, `{channel}`, `{username}`, `{full_name}`"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="btn_custom_msg", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Clear Slot 2
        elif data == "btn_clear_slot_2":
            await database.clear_custom_message_slot_2(user_id)
            await query.answer("Message 2 remove ho gaya! Ab sirf 1st message jayega.", show_alert=True)
            user_data = await database.get_user(user_id)
            msg_1_type = (user_data.get("msg_1_type") or user_data.get("msg_type", "text")).upper()
            msg_1_content = user_data.get("msg_1_content") or user_data.get("custom_message") or DEFAULT_MESSAGE
            text = (
                "✍️ **Multi-Message Auto-DM Dashboard**\n\n"
                "1️⃣ **Message 1 (First DM):**\n"
                f"• Type: `{msg_1_type}` | Status: `🟢 Active`\n"
                f"• Content: {msg_1_content[:70]}...\n\n"
                "2️⃣ **Message 2 (Follow-up DM):**\n"
                "• Status: `⚪ Disabled / Cleared`\n\n"
                "Ab user ko join request par **sirf 1st Message** hi jayega!"
            )
            buttons = [
                [InlineKeyboardButton(text="1️⃣ Edit Message 1 (First Msg)", callback_data="btn_edit_slot_1", style="primary")],
                [InlineKeyboardButton(text="2️⃣ Add Message 2 (Second Msg)", callback_data="btn_edit_slot_2", style="success")],
                [InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="btn_menu", style="default")]
            ]
            try:
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            except TelegramBadRequest:
                pass

        # Reset message to default
        elif data == "btn_reset_msg":
            await database.update_custom_message(user_id, DEFAULT_MESSAGE.strip())
            await database.clear_custom_message_slot_2(user_id)
            await query.answer("Default message restore ho gaya!", show_alert=True)
            await cleanup_auth_state(user_id)
            user_data = await database.get_user(user_id)
            try:
                await query.message.edit_text("✅ Message 1 default par set ho gaya aur Message 2 clear ho gaya!", reply_markup=get_main_keyboard(user_data))
            except TelegramBadRequest:
                pass

        # 8. Delay Settings Menu
        elif data == "btn_delay_menu":
            min_d = user_data.get("min_delay", 0) if user_data else 0
            max_d = user_data.get("max_delay", 0) if user_data else 0

            current_str = "⚡ Instant (0s Delay)" if max_d == 0 else f"{min_d}s - {max_d}s"

            text = (
                "⏱️ **Anti-Ban / Speed Delay Settings**\n\n"
                f"Current Speed: **{current_str}**\n\n"
                "Apni requirement ke anusaar speed select karein:"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Super Instant (0s - Direct DM) [Recommended]", callback_data="set_delay_0_0", style="success")],
                [InlineKeyboardButton(text="⚡ Fast (1s - 3s)", callback_data="set_delay_1_3", style="primary")],
                [InlineKeyboardButton(text="🛡️ Safe (3s - 6s)", callback_data="set_delay_3_6", style="primary")],
                [InlineKeyboardButton(text="🐢 Ultra Safe (6s - 12s)", callback_data="set_delay_6_12", style="default")],
                [InlineKeyboardButton(text="🔙 Back", callback_data="btn_menu", style="default")]
            ])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 9. Set Delay Options
        elif data.startswith("set_delay_"):
            parts = data.split("_")
            min_val, max_val = int(parts[2]), int(parts[3])
            await database.update_delay(user_id, min_val, max_val)
            speed_txt = "🚀 Super Instant (0s)" if max_val == 0 else f"{min_val}s - {max_val}s"
            await query.answer(f"Speed updated to {speed_txt}!", show_alert=True)
            user_data = await database.get_user(user_id)
            try:
                await query.message.edit_text(f"✅ Speed successfully updated to **{speed_txt}**!", reply_markup=get_main_keyboard(user_data))
            except TelegramBadRequest:
                pass

        # 10. Stats & Info
        elif data == "btn_stats":
            if not user_data:
                await query.answer("Account not connected!", show_alert=True)
                return

            phone = user_data.get("phone_number", "N/A")
            total_dms = user_data.get("total_dms_sent", 0)
            auto_app = "ON ✅" if user_data.get("auto_approve") == 1 else "OFF ❌"
            status = "Active 🟢" if user_data.get("is_active") == 1 else "Paused ⏸️"
            created = user_data.get("created_at", "N/A")

            text = (
                "📊 **Aapke Account ke Statistics**\n\n"
                f"👤 **Phone / ID:** `{phone}`\n"
                f"🚀 **Automation:** {status}\n"
                f"✉️ **Total DMs Sent:** `{total_dms}`\n"
                f"⚡ **Auto-Approve:** {auto_app}\n"
                f"⏱️ **Delay:** {user_data.get('min_delay', 3)}s - {user_data.get('max_delay', 7)}s\n"
                f"📅 **Joined Date:** `{created}`"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="btn_menu", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 11. Tags Guide
        elif data in ["btn_tags_guide", "btn_help"]:
            text = (
                "ℹ️ **Help & Guide**\n\n"
                "**1. Connect Kaise Karein?**\n"
                "• **Phone (OTP):** Apna number daalein aur Telegram ka OTP enter karein.\n"
                "• **Session String:** 1-Click me apna Pyrogram/Telethon session string paste karein.\n\n"
                "**2. Message Tags:**\n"
                "• `{name}` ➔ User ka first name\n"
                "• `{full_name}` ➔ Pura naam\n"
                "• `{username}` ➔ @username\n"
                "• `{channel}` ➔ Channel ka title\n\n"
                "💡 **Note:** Bas aapki **Personal ID us channel me Admin** honi chahiye jisme 'Invite Links / Approve Members' permission ho!"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="btn_menu", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 12. Broadcast Menu Prompt
        elif data == "btn_broadcast_menu":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            user_states[user_id] = {"step": "WAITING_BROADCAST_MSG"}
            text = (
                "📡 **Multi-Channel Broadcast (Ek Sath Multiple Channels me Post Karein 🚀)**\n\n"
                "👉 **Apna message yahan chat me bhejein:**\n"
                "• 📝 **Text Message:** (Links, Custom TG Premium Emojis)\n"
                "• 🎤 **Voice Note:** (Audio Message)\n"
                "• 🖼️ **Photo:** (with caption)\n"
                "• 🎬 **Video / GIF:** (with caption)\n"
                "• 🎭 **Telegram Sticker**\n"
                "• 📹 **Round Video Note**\n"
                "• 📁 **Document / File**\n\n"
                "💡 *Message bhejne ke baad aap interactive menu se select kar sakenge ki kin-kin Admin/Owner channels me ye message post karna hai!*"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Cancel", callback_data="btn_menu", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Broadcast Toggle Channel Selection
        elif data.startswith("bc_tog_"):
            raw_id = data.replace("bc_tog_", "")
            state = user_states.get(user_id) or {}
            if state.get("step") != "WAITING_BROADCAST_CHANNELS":
                await query.answer("Session expire ho gaya. Dobara message bhejein.", show_alert=True)
                return

            selected = state.get("selected_channels", [])
            raw_clean = raw_id.replace("-100", "")
            full_clean = f"-100{raw_clean}"

            if (raw_clean in selected) or (full_clean in selected):
                selected = [x for x in selected if x not in (raw_clean, full_clean)]
            else:
                selected.append(raw_clean)

            user_states[user_id]["selected_channels"] = selected
            channels = state.get("available_channels") or await client_manager.get_user_admin_channels(user_id)
            keyboard = build_broadcast_channels_keyboard(channels, selected)
            try:
                await query.message.edit_reply_markup(reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Broadcast Select ALL Channels
        elif data == "bc_all_sel":
            state = user_states.get(user_id) or {}
            if state.get("step") != "WAITING_BROADCAST_CHANNELS":
                await query.answer("Session expire ho gaya. Dobara message bhejein.", show_alert=True)
                return

            channels = state.get("available_channels") or await client_manager.get_user_admin_channels(user_id)
            all_ids = [str(ch.get("raw_id", ch.get("id"))).replace("-100", "") for ch in channels]
            user_states[user_id]["selected_channels"] = all_ids
            keyboard = build_broadcast_channels_keyboard(channels, all_ids)
            try:
                await query.message.edit_reply_markup(reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer("⚡ Sabhi Channels Select ho gaye!", show_alert=False)

        # Broadcast Deselect ALL Channels
        elif data == "bc_all_desel":
            state = user_states.get(user_id) or {}
            if state.get("step") != "WAITING_BROADCAST_CHANNELS":
                await query.answer("Session expire ho gaya. Dobara message bhejein.", show_alert=True)
                return

            channels = state.get("available_channels") or await client_manager.get_user_admin_channels(user_id)
            user_states[user_id]["selected_channels"] = []
            keyboard = build_broadcast_channels_keyboard(channels, [])
            try:
                await query.message.edit_reply_markup(reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer("⏸️ Sabhi Channels Deselect ho gaye!", show_alert=False)

        # Save Current Selection as Default Preset
        elif data == "bc_save_preset":
            state = user_states.get(user_id) or {}
            if state.get("step") != "WAITING_BROADCAST_CHANNELS":
                await query.answer("Session expire ho gaya. Dobara message bhejein.", show_alert=True)
                return

            selected = state.get("selected_channels", [])
            if not selected:
                await query.answer("⚠️ Pehle kam se kam 1 channel select karein!", show_alert=True)
                return

            await database.save_default_broadcast_channels(user_id, selected)
            await query.answer(f"💾 {len(selected)} Channels aapke Default Preset me save ho gaye!", show_alert=True)

        # Load Saved Default Preset
        elif data == "bc_load_preset":
            state = user_states.get(user_id) or {}
            if state.get("step") != "WAITING_BROADCAST_CHANNELS":
                await query.answer("Session expire ho gaya. Dobara message bhejein.", show_alert=True)
                return

            saved_preset = await database.get_default_broadcast_channels(user_id)
            if not saved_preset:
                await query.answer("⚠️ Koi saved preset nahi mila! Pehle 'Save Preset' karein.", show_alert=True)
                return

            channels = state.get("available_channels") or await client_manager.get_user_admin_channels(user_id)
            valid_saved = [x for x in saved_preset if any(str(ch.get("raw_id", ch.get("id"))).replace("-100", "") == x for ch in channels)]

            user_states[user_id]["selected_channels"] = valid_saved
            keyboard = build_broadcast_channels_keyboard(channels, valid_saved, has_saved_preset=True)
            try:
                await query.message.edit_reply_markup(reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer(f"🔁 Saved Preset ({len(valid_saved)} Channels) load ho gaya!", show_alert=False)

        # Broadcast Send Now
        elif data == "bc_send_now":
            state = user_states.get(user_id) or {}
            if state.get("step") != "WAITING_BROADCAST_CHANNELS":
                await query.answer("Session expire ho gaya. Dobara message bhejein.", show_alert=True)
                return

            selected = state.get("selected_channels", [])
            if not selected:
                await query.answer("⚠️ Kam se kam 1 channel select karein!", show_alert=True)
                return

            broadcast_msg = state.get("broadcast_msg")
            if not broadcast_msg:
                await query.answer("Broadcast message nahi mila! Dobara bhejein.", show_alert=True)
                return

            # Auto-save current selection as default preset for next time convenience!
            await database.save_default_broadcast_channels(user_id, selected)

            await query.answer("🚀 Broadcasting shuru ho rahi hai...")
            try:
                await query.message.edit_text(f"⏳ **Broadcasting in Progress...**\n\nTotal `{len(selected)}` channels me post bheja ja raha hai. Kripya thoda intezar karein...")
            except TelegramBadRequest:
                pass

            res = await client_manager.broadcast_message_to_channels(user_id, broadcast_msg, selected)
            await cleanup_auth_state(user_id)
            user_data = await database.get_user(user_id)

            success_cnt = res.get("success", 0)
            failed_cnt = res.get("failed", 0)
            succ_names = res.get("successful_channels", [])
            succ_list = "\n".join([f"• ✅ **{name}**" for name in succ_names[:10]]) if succ_names else "• None"
            if len(succ_names) > 10:
                succ_list += f"\n• ...aur `{len(succ_names) - 10}` channels"

            summary_text = (
                "🎉 **Broadcast 100% Completed!**\n\n"
                f"📊 **Summary:**\n"
                f"• ✅ **Successfully Posted:** `{success_cnt}` Channels\n"
                f"• ❌ **Failed:** `{failed_cnt}` Channels\n\n"
                f"📋 **Successful Channels:**\n{succ_list}\n\n"
                "💾 *Ye channels aapke default preset me bhi save ho gaye hain taaki agli baar direct select milein!*"
            )
            buttons = [
                [InlineKeyboardButton(text="📡 Broadcast Another Message", callback_data="btn_broadcast_menu", style="success")],
                [InlineKeyboardButton(text="🔙 Back to Dashboard", callback_data="btn_menu", style="default")]
            ]
            try:
                await query.message.edit_text(summary_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            except TelegramBadRequest:
                pass

        # 13. Dedicated A-Z & 1-10 Quick DM Shortcuts Dashboard
        elif data == "btn_quick_triggers":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            text, keyboard = build_quick_triggers_dashboard(user_data)
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Add Custom A-Z / Keyword Shortcut Trigger
        elif data == "btn_add_custom_trigger":
            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            user_states[user_id] = {"step": "WAITING_TRIGGER_KEY"}
            text = (
                "➕ **Add Custom A-Z / Keyword DM Shortcut**\n\n"
                "👉 **Shortcut ka Trigger Word ya Letter chat me type karke bhejein:**\n"
                "*(Examples: `a`, `b`, `c`, `loss`, `qr`, `link`, `hi`, `price`, `upi`, `demo` etc.)*\n\n"
                "💡 **Fayda:** Ye shortcut private DM me **bina '.' ke bhi chalega** (jaise direct `a` ya `loss` bhejne par) aur `.` ke sath bhi chalega (jaise `.a` ya `.loss`)!"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Cancel", callback_data="btn_quick_triggers", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Edit Quick Shortcut Slot 1 to 10
        elif data.startswith("btn_edit_quick_"):
            slot_num_str = data.replace("btn_edit_quick_", "")
            if not slot_num_str.isdigit():
                return
            slot = int(slot_num_str)

            if not user_data:
                await query.answer("Pehle account connect karein!", show_alert=True)
                return

            user_states[user_id] = {"step": "WAITING_QUICK_MSG", "slot": slot}
            text = (
                f"📝 **Setting Shortcut `.{slot}` Custom DM Message**\n\n"
                "👉 **Apna message yahan chat me bhejein:**\n"
                "• 📝 Text Message (Animated Emojis & Links supported)\n"
                "• 🎤 Voice Note (Audio message)\n"
                "• 🖼️ Photo with Caption\n"
                "• 🎬 Video with Caption\n"
                "• 🎭 Telegram Sticker\n"
                "• 📁 Document / File\n\n"
                f"💡 *Aap kisi user ke DM me direct `{slot}` ya `.{slot}` likhenge toh yehi message deliver hoga!*"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Shortcuts", callback_data="btn_quick_triggers", style="default")]])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # Clear Shortcut Submenu
        elif data == "btn_clear_quick_menu":
            custom_triggers = (user_data.get("custom_triggers") or {}) if user_data else {}
            buttons = []
            
            # Custom keyword delete buttons
            cust_row = []
            for k in sorted(custom_triggers.keys()):
                cust_row.append(InlineKeyboardButton(text=f"🗑️ '{k}'", callback_data=f"btn_del_cust_{k}", style="danger"))
                if len(cust_row) == 3:
                    buttons.append(cust_row)
                    cust_row = []
            if cust_row:
                buttons.append(cust_row)

            # Number slots delete buttons
            buttons.append([
                InlineKeyboardButton(text="🗑️ .1", callback_data="btn_do_clear_1", style="danger"),
                InlineKeyboardButton(text="🗑️ .2", callback_data="btn_do_clear_2", style="danger"),
                InlineKeyboardButton(text="🗑️ .3", callback_data="btn_do_clear_3", style="danger"),
                InlineKeyboardButton(text="🗑️ .4", callback_data="btn_do_clear_4", style="danger"),
                InlineKeyboardButton(text="🗑️ .5", callback_data="btn_do_clear_5", style="danger")
            ])
            buttons.append([
                InlineKeyboardButton(text="🗑️ .6", callback_data="btn_do_clear_6", style="danger"),
                InlineKeyboardButton(text="🗑️ .7", callback_data="btn_do_clear_7", style="danger"),
                InlineKeyboardButton(text="🗑️ .8", callback_data="btn_do_clear_8", style="danger"),
                InlineKeyboardButton(text="🗑️ .9", callback_data="btn_do_clear_9", style="danger"),
                InlineKeyboardButton(text="🗑️ .10", callback_data="btn_do_clear_10", style="danger")
            ])
            buttons.append([InlineKeyboardButton(text="🔙 Back to Shortcuts", callback_data="btn_quick_triggers", style="default")])
            
            text = "🗑️ **Kaunsa Shortcut Clear / Remove karna chahte hain?**\n\nNeeche trigger select karein:"
            try:
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            except TelegramBadRequest:
                pass
            await query.answer()

        # Delete Custom A-Z Trigger
        elif data.startswith("btn_del_cust_"):
            trigger_key = data.replace("btn_del_cust_", "").strip()
            await database.delete_custom_trigger(user_id, trigger_key)
            await query.answer(f"Trigger '{trigger_key}' delete ho gaya!", show_alert=True)
            user_data = await database.get_user(user_id)
            text, keyboard = build_quick_triggers_dashboard(user_data)
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass

        # Execute Clear Numbered Slot
        elif data.startswith("btn_do_clear_"):
            slot_num_str = data.replace("btn_do_clear_", "")
            if slot_num_str.isdigit():
                slot = int(slot_num_str)
                await database.clear_quick_shortcut_slot(user_id, slot)
                await query.answer(f"Shortcut `.{slot}` clear ho gaya!", show_alert=True)
            
            user_data = await database.get_user(user_id)
            text, keyboard = build_quick_triggers_dashboard(user_data)
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass

        # 14. Logout Confirm
        elif data == "btn_logout_confirm":
            text = (
                "⚠️ **Logout Confirmation**\n\n"
                "Kya aap sach me apni ID disconnect karna chahte hain?\n"
                "Logout karne par Auto-DM band ho jayega."
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Haan, Disconnect Karein", callback_data="btn_logout_action", style="danger")],
                [InlineKeyboardButton(text="❌ Nahi, Cancel", callback_data="btn_menu", style="default")]
            ])
            try:
                await query.message.edit_text(text, reply_markup=keyboard)
            except TelegramBadRequest:
                pass
            await query.answer()

        # 13. Logout Action
        elif data == "btn_logout_action":
            await client_manager.stop_client(user_id)
            await database.delete_user_session(user_id)
            await cleanup_auth_state(user_id)
            await query.answer("Account safely disconnected!", show_alert=True)
            try:
                await query.message.edit_text("🚪 **Account Successfully Disconnected.**\n\nAap jab chahe dobara login kar sakte hain.", reply_markup=get_main_keyboard(None))
            except TelegramBadRequest:
                pass

    except Exception as e:
        logger.error(f"Error handling callback {data}: {e}", exc_info=True)
        await query.answer(f"Error: {e}", show_alert=True)

MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_storage")
os.makedirs(MEDIA_DIR, exist_ok=True)

@dp.message()
async def message_input_handler(message: types.Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    text_preview = message.text[:20] if message.text else f"Media: {message.content_type}"
    logger.info(f"💬 Received message ({text_preview}) from {user_id} (State: {state.get('step') if state else 'None'})")

    if not state:
        await start_handler(message)
        return

    step = state.get("step")

    # ---------------- Step: Direct Session String Paste ----------------
    if step == "WAITING_SESSION_STRING":
        session_str = message.text.strip()
        status_msg = await message.answer("⏳ Session string verify ho rahi hai...")
        logger.info(f"🔑 Verifying Session String for user {user_id}...")

        try:
            test_client = TelegramClient(
                session=StringSession(session_str),
                api_id=API_ID,
                api_hash=API_HASH,
                device_model="Telegram Desktop",
                system_version="Windows 11 x64",
                app_version="5.4.1 x64"
            )
            await test_client.connect()
            if not await test_client.is_user_authorized():
                await test_client.disconnect()
                raise ValueError("Session string is invalid or not authorized.")

            me = await test_client.get_me()
            phone_num = getattr(me, "phone", None) or str(me.id)
            user_display_name = me.first_name or "Telegram User"
            await test_client.disconnect()
            logger.info(f"✅ Session verified for user: {user_display_name} (+{phone_num})")

            # Save in MongoDB
            phone_formatted = f"+{phone_num.lstrip('+')}"
            await database.save_or_update_session(user_id, phone_formatted, session_str)
            logger.info(f"✅ Session saved to MongoDB for user {user_id}")

            # Start background automation
            asyncio.create_task(client_manager.start_client(user_id, session_str))

            await cleanup_auth_state(user_id)
            user_data = await database.get_user(user_id)

            await status_msg.edit_text(
                "🎉 **Login 100% Successful!**\n\n"
                f"👤 **Account:** `{user_display_name}` (`{phone_formatted}`)\n"
                f"🍃 **Status:** MongoDB me saved & Auto-DM active!\n\n"
                "Neeche diye gaye buttons se apna message aur settings control karein 👇",
                reply_markup=get_main_keyboard(user_data)
            )

        except Exception as e:
            logger.error(f"❌ Invalid session string: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **Invalid Session String!**\n\nError: `{e}`\n\nKripya valid Telethon/Pyrogram session string paste karein.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Dobara Try Karein", callback_data="btn_session_login", style="primary")],
                    [InlineKeyboardButton(text="📱 Use Phone OTP Instead", callback_data="btn_login", style="success")],
                    [InlineKeyboardButton(text="❌ Cancel", callback_data="btn_menu", style="danger")]
                ])
            )

    # ---------------- Step 1: Phone Number Input (Telethon Engine) ----------------
    elif step == "WAITING_PHONE":
        phone_number = "+" + message.text.strip().lstrip("+").replace(" ", "").replace("-", "")

        status_msg = await message.answer("⏳ Telegram se OTP request kiya ja raha hai...")
        logger.info(f"📲 Requesting OTP for phone: {phone_number} (User {user_id}) using Telethon Engine...")

        client = create_telethon_auth_client()

        try:
            await client.connect()
            send_code_res = await client.send_code_request(phone_number)
            phone_code_hash = send_code_res.phone_code_hash
            logger.info(f"✅ Telethon OTP sent successfully! Hash: {phone_code_hash}")

            user_states[user_id] = {
                "step": "WAITING_OTP",
                "client": client,
                "phone_number": phone_number,
                "phone_code_hash": phone_code_hash
            }

            await status_msg.edit_text(
                f"📩 **Telegram OTP Send Kar Diya Gaya Hai!**\n\n"
                f"Aapke Telegram app (`{phone_number}`) par ek login code aaya hoga.\n\n"
                f"⚠️ **STRICT INSTRUCTION (Space Dena Zaroori Hai):**\n"
                f"Telegram direct code ko security filter se block kar deta hai, isliye **har number ke beech SPACE zaroor dein!**\n\n"
                f"👉 **Aise likh kar bhejein:**\n"
                f"`1 2 3 4 5`\n\n"
                f"*(Example: Agar code 91509 hai toh `9 1 5 0 9` bhejein)*",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="btn_menu", style="danger")]])
            )

        except PhoneNumberBannedError:
            await cleanup_auth_state(user_id)
            logger.warning(f"Phone number banned: {phone_number}")
            await status_msg.edit_text("❌ Ye phone number Telegram par BANNED hai.")
        except PhoneNumberInvalidError:
            await cleanup_auth_state(user_id)
            logger.warning(f"Invalid phone number: {phone_number}")
            await status_msg.edit_text(
                "❌ **Phone number invalid hai!**\n\nKripya country code ke sath sahi number bhejein (jaise: `+919876543210` ya `+19127418551`).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Dobara Try Karein", callback_data="btn_login", style="primary")]])
            )
        except FloodWaitError as e:
            await cleanup_auth_state(user_id)
            logger.warning(f"FloodWait on send_code: {e.seconds}s")
            await status_msg.edit_text(f"⚠️ Telegram FloodWait: Kripya {e.seconds} seconds baad dobara try karein.")
        except Exception as e:
            await cleanup_auth_state(user_id)
            logger.error(f"Error in Telethon phone login: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **Error:** `{e}`\n\nKripya dobara try karein ya Session String use karein.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔑 Use Session String", callback_data="btn_session_login", style="success")],
                    [InlineKeyboardButton(text="🔄 Retry Phone", callback_data="btn_login", style="primary")]
                ])
            )

    # ---------------- Step 2: OTP Code Input (Telethon Engine) ----------------
    elif step == "WAITING_OTP":
        otp_code = message.text.strip().replace(" ", "").replace("-", "")
        client: TelegramClient = state.get("client")
        phone_number = state.get("phone_number")
        phone_code_hash = state.get("phone_code_hash")

        status_msg = await message.answer("⏳ Login verify kiya ja raha hai...")
        logger.info(f"🔑 Verifying OTP '{otp_code}' for {phone_number} (User {user_id})...")

        try:
            if not client.is_connected():
                await client.connect()

            user_obj = await client.sign_in(
                phone=phone_number,
                code=otp_code,
                phone_code_hash=phone_code_hash
            )
            logger.info(f"✅ Telethon sign in successful: {user_obj.first_name} (ID: {user_obj.id})")

            # Export Telethon String Session
            session_str = client.session.save()
            await client.disconnect()

            await database.save_or_update_session(user_id, phone_number, session_str)
            logger.info(f"✅ Session saved to MongoDB for user {user_id}")

            asyncio.create_task(client_manager.start_client(user_id, session_str))

            await cleanup_auth_state(user_id)
            user_data = await database.get_user(user_id)

            await status_msg.edit_text(
                "🎉 **Login 100% Successful!**\n\n"
                f"Aapka account `{phone_number}` successfully connect ho chuka hai aur Auto-DM active hai!\n\n"
                "Neeche buttons se apna custom message aur settings customize karein 👇",
                reply_markup=get_main_keyboard(user_data)
            )

        except SessionPasswordNeededError:
            logger.warning(f"🔐 Account {phone_number} requires 2FA Cloud Password.")
            user_states[user_id]["step"] = "WAITING_2FA"
            await status_msg.edit_text(
                "🔐 **Two-Step Verification (2FA) Detected!**\n\n"
                "Aapke account par 2FA Cloud Password enable hai.\n\n"
                "👉 **Kripya apna 2FA Cloud Password yahan type karke bhejein:**",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="btn_menu", style="danger")]])
            )

        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            logger.warning(f"❌ Invalid or expired OTP: {e}")
            await status_msg.edit_text(
                "❌ **OTP Galat Hai ya Expire Ho Chuka Hai!**\n\n"
                "⚠️ **STRICT INSTRUCTION:** Har digit ke beech SPACE hona chahiye, jaise: `1 2 3 4 5`.\n\n"
                "👉 Kripya sahi 5-digit code dobara space ke sath type karke bhejein:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Naya OTP Request Karein", callback_data="btn_login", style="primary")],
                    [InlineKeyboardButton(text="🔑 Paste Session String", callback_data="btn_session_login", style="success")],
                    [InlineKeyboardButton(text="❌ Cancel", callback_data="btn_menu", style="danger")]
                ])
            )
        except Exception as e:
            logger.error(f"❌ Error verifying OTP: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **Login Error:** `{e}`\n\nKripya dobara try karein.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔑 Use Session String", callback_data="btn_session_login", style="success")],
                    [InlineKeyboardButton(text="🔄 Restart Login", callback_data="btn_login", style="primary")]
                ])
            )

    # ---------------- Step 3: 2FA Password Input (Telethon Engine) ----------------
    elif step == "WAITING_2FA":
        password = message.text.strip()
        client: TelegramClient = state.get("client")
        phone_number = state.get("phone_number")

        status_msg = await message.answer("⏳ 2FA Password verify ho raha hai...")
        logger.info(f"🔐 Verifying 2FA Password for {phone_number} (User {user_id})...")

        try:
            if not client.is_connected():
                await client.connect()

            await client.sign_in(password=password)
            logger.info("✅ 2FA Password matched successfully!")

            session_str = client.session.save()
            await client.disconnect()

            await database.save_or_update_session(user_id, phone_number, session_str)
            logger.info(f"✅ Session saved to MongoDB for user {user_id}")

            asyncio.create_task(client_manager.start_client(user_id, session_str))

            await cleanup_auth_state(user_id)
            user_data = await database.get_user(user_id)

            await status_msg.edit_text(
                "🎉 **2FA Verified & Login Successful!**\n\n"
                f"Aapka account `{phone_number}` successfully connect ho chuka hai aur Auto-DM active hai!\n\n"
                "Neeche buttons se apna custom message aur settings customize karein 👇",
                reply_markup=get_main_keyboard(user_data)
            )

        except PasswordHashInvalidError:
            logger.warning(f"❌ Invalid 2FA password entered for user {user_id}")
            await status_msg.edit_text(
                "❌ **2FA Password Galat Hai!**\n\nKripya dobara sahi password bhejein:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="btn_menu", style="danger")]])
            )
        except Exception as e:
            logger.error(f"❌ Error verifying 2FA: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **2FA Error:** `{e}`\n\nKripya dobara try karein.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Restart Login", callback_data="btn_login", style="primary")]])
            )

    # ---------------- Step 4: Custom Message & Media Input ----------------
    elif step == "WAITING_CUSTOM_MSG":
        slot = state.get("slot", 1)
        status_msg = await message.answer(f"⏳ Message {slot} process kiya ja raha hai...")

        try:
            # 1. Sticker (Premium & Animated Stickers)
            if message.sticker:
                sticker = message.sticker
                s_file = await bot.get_file(sticker.file_id)
                ext = ".tgs" if sticker.is_animated else (".webm" if sticker.is_video else ".webp")
                save_path = os.path.join(MEDIA_DIR, f"custom_{user_id}_slot{slot}_sticker{ext}")
                await bot.download_file(s_file.file_path, save_path)
                
                await database.update_custom_message_slot(user_id, slot, "sticker", sticker.emoji or "🎭 Custom Sticker", save_path)
                await cleanup_auth_state(user_id)
                user_data = await database.get_user(user_id)

                await status_msg.edit_text(
                    f"🎉 **Message {slot}: Telegram (Premium) Sticker Saved!**\n\n"
                    f"🎭 **Sticker Emoji:** `{sticker.emoji or 'Premium Sticker'}`\n"
                    f"Aapki personal ID se **yehi Sticker Message {slot} ke roop me jayega!** 🚀",
                    reply_markup=get_main_keyboard(user_data)
                )

            # 2. Photo with Caption
            elif message.photo:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                save_path = os.path.join(MEDIA_DIR, f"custom_{user_id}_slot{slot}_photo.jpg")
                await bot.download_file(p_file.file_path, save_path)
                caption = message.html_text or ""

                await database.update_custom_message_slot(user_id, slot, "photo", caption, save_path)
                await cleanup_auth_state(user_id)
                user_data = await database.get_user(user_id)

                await status_msg.edit_text(
                    f"🎉 **Message {slot}: Photo + Custom Caption Saved!**\n\n"
                    f"📝 **Caption:**\n━━━━━━━━━━━━━━━━━━━\n{caption or '*(No Caption)*'}\n━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Join request aane par aapki ID se **ye Photo + Caption Message {slot} par send hoga!** 🖼️",
                    reply_markup=get_main_keyboard(user_data)
                )

            # 3. Video / GIF / Animation with Caption
            elif message.video or message.animation:
                v_obj = message.video or message.animation
                v_file = await bot.get_file(v_obj.file_id)
                save_path = os.path.join(MEDIA_DIR, f"custom_{user_id}_slot{slot}_video.mp4")
                await bot.download_file(v_file.file_path, save_path)
                caption = message.html_text or ""

                await database.update_custom_message_slot(user_id, slot, "video", caption, save_path)
                await cleanup_auth_state(user_id)
                user_data = await database.get_user(user_id)

                await status_msg.edit_text(
                    f"🎉 **Message {slot}: Video/GIF + Custom Caption Saved!**\n\n"
                    f"📝 **Caption:**\n━━━━━━━━━━━━━━━━━━━\n{caption or '*(No Caption)*'}\n━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Join request aane par aapki ID se **ye Video/GIF Message {slot} par send hoga!** 🎬",
                    reply_markup=get_main_keyboard(user_data)
                )

            # 4. Voice Note (Audio Message)
            elif message.voice:
                voice = message.voice
                v_file = await bot.get_file(voice.file_id)
                save_path = os.path.join(MEDIA_DIR, f"custom_{user_id}_slot{slot}_voice.ogg")
                await bot.download_file(v_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.update_custom_message_slot(user_id, slot, "voice", caption or "🎤 Voice Message", save_path)
                await cleanup_auth_state(user_id)
                user_data = await database.get_user(user_id)

                await status_msg.edit_text(
                    f"🎉 **Message {slot}: Voice Note (Audio Message) Successfully Saved!**\n\n"
                    f"⏱️ **Duration:** `{voice.duration} seconds`\n\n"
                    f"Join request aane par aapki personal ID se **yehi Voice Message DM me send hoga!** 🎤🚀",
                    reply_markup=get_main_keyboard(user_data)
                )

            # 5. Round Video Message (Video Note)
            elif message.video_note:
                vn = message.video_note
                vn_file = await bot.get_file(vn.file_id)
                save_path = os.path.join(MEDIA_DIR, f"custom_{user_id}_slot{slot}_videonote.mp4")
                await bot.download_file(vn_file.file_path, save_path)

                await database.update_custom_message_slot(user_id, slot, "video_note", "📹 Round Video Note", save_path)
                await cleanup_auth_state(user_id)
                user_data = await database.get_user(user_id)

                await status_msg.edit_text(
                    f"🎉 **Message {slot}: Round Video Note Saved!**\n\n"
                    f"📹 **Duration:** `{vn.duration} seconds`\n\n"
                    f"Join request aane par aapki ID se **ye Round Video Message {slot} par send hoga!** 📹🚀",
                    reply_markup=get_main_keyboard(user_data)
                )

            # 6. Audio File (MP3 / Music)
            elif message.audio:
                audio = message.audio
                a_file = await bot.get_file(audio.file_id)
                ext = os.path.splitext(audio.file_name or "audio.mp3")[1] or ".mp3"
                save_path = os.path.join(MEDIA_DIR, f"custom_{user_id}_slot{slot}_audio{ext}")
                await bot.download_file(a_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.update_custom_message_slot(user_id, slot, "audio", caption or audio.file_name or "🎵 Audio File", save_path)
                await cleanup_auth_state(user_id)
                user_data = await database.get_user(user_id)

                await status_msg.edit_text(
                    f"🎉 **Message {slot}: Audio File Saved!**\n\n"
                    f"🎵 **File Name:** `{audio.file_name or 'Audio'}`\n"
                    f"Join request aane par aapki ID se **ye Audio File Message {slot} par send hogi!** 🎵",
                    reply_markup=get_main_keyboard(user_data)
                )

            # 7. Document / PDF / File
            elif message.document:
                doc = message.document
                d_file = await bot.get_file(doc.file_id)
                ext = os.path.splitext(doc.file_name or "file.bin")[1] or ".bin"
                save_path = os.path.join(MEDIA_DIR, f"custom_{user_id}_slot{slot}_doc{ext}")
                await bot.download_file(d_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.update_custom_message_slot(user_id, slot, "document", caption or doc.file_name or "📁 Document", save_path)
                await cleanup_auth_state(user_id)
                user_data = await database.get_user(user_id)

                await status_msg.edit_text(
                    f"🎉 **Message {slot}: Document / File Saved!**\n\n"
                    f"📁 **File:** `{doc.file_name or 'Document'}`\n"
                    f"Join request aane par aapki ID se **ye Document Message {slot} par send hoga!** 📁",
                    reply_markup=get_main_keyboard(user_data)
                )

            # 8. Text Message (Preserving Telegram Premium Custom Emojis & HTML)
            elif message.text:
                new_custom_msg = message.html_text or message.text
                await database.update_custom_message_slot(user_id, slot, "text", new_custom_msg, None)
                await cleanup_auth_state(user_id)
                user_data = await database.get_user(user_id)

                await status_msg.edit_text(
                    f"✅ **Message {slot}: Custom Text Message Updated!**\n\n"
                    "*(💡 Telegram Premium Custom Emojis & Rich Formatting Preserved)*\n\n"
                    "**Preview:**\n"
                    "━━━━━━━━━━━━━━━━━━━\n"
                    f"{new_custom_msg}\n"
                    "━━━━━━━━━━━━━━━━━━━",
                    reply_markup=get_main_keyboard(user_data),
                    parse_mode=ParseMode.HTML
                )

        except Exception as e:
            logger.error(f"Error saving custom media/message: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **Error saving media:** `{e}`\n\nKripya dobara send karein.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back to Menu", callback_data="btn_menu", style="default")]])
            )

    # ---------------- Step 5: Channel Join Link Generator ----------------
    elif step == "WAITING_CHANNEL_LINK":
        channel_input = message.text.strip()
        status_msg = await message.answer("⏳ Exclusive Admin Approval Link banayi ja rahi hai...")
        
        ok, link_or_err = await client_manager.create_channel_join_link(user_id, channel_input)
        await cleanup_auth_state(user_id)
        user_data = await database.get_user(user_id)

        if ok:
            await status_msg.edit_text(
                "🎉 **Exclusive Join Request Link Generated!**\n\n"
                f"🔗 **Link:** `{link_or_err}`\n\n"
                "📌 **Ye Link Share Karein:**\n"
                "Is link par jo bhi banda 'Request to Join' dabayega, usko **aapki ID se Automatic DM** aur **Auto-Approve** ho jayega! 🚀",
                reply_markup=get_main_keyboard(user_data)
            )
        else:
            await status_msg.edit_text(
                f"{link_or_err}\n\nKripya check karein ki aapki ID us channel me Admin hai ya nahi.",
                reply_markup=get_main_keyboard(user_data)
            )

    # ---------------- Step 6: Target Channel Add Input ----------------
    elif step == "WAITING_ADD_TARGET_CHANNEL":
        channel_input = message.text.strip()
        status_msg = await message.answer("⏳ Channel verify kiya ja raha hai...")
        
        ok, res = await client_manager.resolve_channel_info(user_id, channel_input)
        await cleanup_auth_state(user_id)

        if ok and isinstance(res, dict):
            await database.add_target_channel(user_id, res)
            user_data = await database.get_user(user_id)
            ch_title = res.get("title", channel_input)
            ch_user = f" ({res.get('username')})" if res.get('username') else ""
            
            await status_msg.edit_text(
                f"✅ **Target Channel Successfully Added to Filter!**\n\n"
                f"📢 **Channel:** `{ch_title}`{ch_user}\n"
                f"🆔 **ID:** `{res.get('id')}`\n\n"
                f"🎯 Ab bot **sirf is channel (aur aapke listed channels)** par hi Auto-DM bhejega!",
                reply_markup=get_main_keyboard(user_data)
            )
        else:
            user_data = await database.get_user(user_id)
            await status_msg.edit_text(
                f"{res}\n\nKripya check karein ki aapki ID is channel me Admin hai ya nahi.",
                reply_markup=get_main_keyboard(user_data)
            )

    # ---------------- Step 7: Broadcast Message Input ----------------
    elif step == "WAITING_BROADCAST_MSG":
        status_msg = await message.answer("⏳ Message process kiya ja raha hai aur Channels load ho rahe hain...")

        msg_type = "text"
        media_path = None
        content = ""

        try:
            if message.sticker:
                msg_type = "sticker"
                ext = ".tgs" if message.sticker.is_animated else (".webm" if message.sticker.is_video else ".webp")
                media_path = os.path.join(MEDIA_DIR, f"bc_{user_id}_sticker{ext}")
                s_file = await bot.get_file(message.sticker.file_id)
                await bot.download_file(s_file.file_path, media_path)
                content = message.sticker.emoji or "🎭 Sticker"

            elif message.photo:
                msg_type = "photo"
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                media_path = os.path.join(MEDIA_DIR, f"bc_{user_id}_photo.jpg")
                await bot.download_file(p_file.file_path, media_path)
                content = message.html_text or ""

            elif message.video or message.animation:
                msg_type = "video"
                v_obj = message.video or message.animation
                v_file = await bot.get_file(v_obj.file_id)
                media_path = os.path.join(MEDIA_DIR, f"bc_{user_id}_video.mp4")
                await bot.download_file(v_file.file_path, media_path)
                content = message.html_text or ""

            elif message.voice:
                msg_type = "voice"
                v_file = await bot.get_file(message.voice.file_id)
                media_path = os.path.join(MEDIA_DIR, f"bc_{user_id}_voice.ogg")
                await bot.download_file(v_file.file_path, media_path)
                content = message.html_text or message.caption or "🎤 Voice Note"

            elif message.video_note:
                msg_type = "video_note"
                vn_file = await bot.get_file(message.video_note.file_id)
                media_path = os.path.join(MEDIA_DIR, f"bc_{user_id}_videonote.mp4")
                await bot.download_file(vn_file.file_path, media_path)
                content = "📹 Round Video Note"

            elif message.audio:
                msg_type = "audio"
                a_file = await bot.get_file(message.audio.file_id)
                ext = os.path.splitext(message.audio.file_name or "audio.mp3")[1] or ".mp3"
                media_path = os.path.join(MEDIA_DIR, f"bc_{user_id}_audio{ext}")
                await bot.download_file(a_file.file_path, media_path)
                content = message.html_text or message.caption or message.audio.file_name or "🎵 Audio"

            elif message.document:
                msg_type = "document"
                d_file = await bot.get_file(message.document.file_id)
                ext = os.path.splitext(message.document.file_name or "file.bin")[1] or ".bin"
                media_path = os.path.join(MEDIA_DIR, f"bc_{user_id}_doc{ext}")
                await bot.download_file(d_file.file_path, media_path)
                content = message.html_text or message.caption or message.document.file_name or "📁 Document"

            elif message.text:
                msg_type = "text"
                content = message.html_text or message.text

            # Fetch user's Owner / Admin channels
            channels = await client_manager.get_user_admin_channels(user_id)
            all_ids = [str(ch.get("raw_id", ch.get("id"))).replace("-100", "") for ch in channels]

            # Check if user has a saved default preset list
            saved_preset = await database.get_default_broadcast_channels(user_id)
            valid_preset = [x for x in saved_preset if x in all_ids]

            # If user has a saved preset, pre-select it! Otherwise pre-select all
            initial_selection = valid_preset if valid_preset else all_ids

            # Save state
            user_states[user_id] = {
                "step": "WAITING_BROADCAST_CHANNELS",
                "broadcast_msg": {
                    "type": msg_type,
                    "text": content,
                    "media_path": media_path
                },
                "selected_channels": initial_selection,
                "available_channels": channels
            }

            preview_text = content[:100] + "..." if len(content) > 100 else (content or f"[{msg_type.upper()} Attachment]")
            preset_info = f"💾 *(Auto-loaded your Saved Preset: {len(initial_selection)} Channels)*" if valid_preset else "⚡ *(Default: All Channels Selected)*"
            text = (
                "📡 **Select Channels to Broadcast**\n\n"
                f"📌 **Message Type:** `{msg_type.upper()}`\n"
                f"📝 **Preview:**\n━━━━━━━━━━━━━━━━━━━\n{preview_text}\n━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 **Found:** `{len(channels)}` Admin/Owner Channels\n"
                f"{preset_info}\n\n"
                "👇 **Kin-kin channels me post karna hai, unhe select/toggle karein:**"
            )
            keyboard = build_broadcast_channels_keyboard(channels, initial_selection, has_saved_preset=bool(valid_preset or saved_preset))
            await status_msg.edit_text(text, reply_markup=keyboard)

        except Exception as e:
            logger.error(f"Error handling broadcast input: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **Error processing message:** `{e}`",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="btn_menu", style="default")]])
            )

    # ---------------- Step 8: Dedicated Quick DM Shortcut Message Input ----------------
    elif step == "WAITING_QUICK_MSG":
        slot = state.get("slot", 1)
        status_msg = await message.answer(f"⏳ Shortcut `.{slot}` ka message save kiya ja raha hai...")

        try:
            # 1. Telegram Premium Animated Sticker
            if message.sticker:
                ext = ".tgs" if message.sticker.is_animated else (".webm" if message.sticker.is_video else ".webp")
                save_path = os.path.join(MEDIA_DIR, f"quick_{user_id}_slot{slot}_sticker{ext}")
                s_file = await bot.get_file(message.sticker.file_id)
                await bot.download_file(s_file.file_path, save_path)
                content = message.sticker.emoji or "🎭 Sticker"

                await database.update_quick_shortcut_slot(user_id, slot, "sticker", content, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Shortcut `.{slot}`: Telegram Sticker Successfully Saved!**\n\n"
                    f"Aap kisi bhi user ke DM me `.{slot}` likhenge toh ye Sticker send hoga! 🎭🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 2. Photo with Caption
            elif message.photo:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                save_path = os.path.join(MEDIA_DIR, f"quick_{user_id}_slot{slot}_photo.jpg")
                await bot.download_file(p_file.file_path, save_path)
                caption = message.html_text or ""

                await database.update_quick_shortcut_slot(user_id, slot, "photo", caption, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Shortcut `.{slot}`: Photo + Custom Caption Saved!**\n\n"
                    f"📝 **Caption:**\n━━━━━━━━━━━━━━━━━━━\n{caption or '*(No Caption)*'}\n━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Aap kisi bhi user ke DM me `.{slot}` likhenge toh ye Photo send hoga! 🖼️🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 3. Video / GIF with Caption
            elif message.video or message.animation:
                v_obj = message.video or message.animation
                v_file = await bot.get_file(v_obj.file_id)
                save_path = os.path.join(MEDIA_DIR, f"quick_{user_id}_slot{slot}_video.mp4")
                await bot.download_file(v_file.file_path, save_path)
                caption = message.html_text or ""

                await database.update_quick_shortcut_slot(user_id, slot, "video", caption, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Shortcut `.{slot}`: Video/GIF + Custom Caption Saved!**\n\n"
                    f"📝 **Caption:**\n━━━━━━━━━━━━━━━━━━━\n{caption or '*(No Caption)*'}\n━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Aap kisi bhi user ke DM me `.{slot}` likhenge toh ye Video send hoga! 🎬🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 4. Voice Note (Audio Message)
            elif message.voice:
                voice = message.voice
                v_file = await bot.get_file(voice.file_id)
                save_path = os.path.join(MEDIA_DIR, f"quick_{user_id}_slot{slot}_voice.ogg")
                await bot.download_file(v_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.update_quick_shortcut_slot(user_id, slot, "voice", caption or "🎤 Voice Message", save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Shortcut `.{slot}`: Voice Note (Audio Message) Saved!**\n\n"
                    f"⏱️ **Duration:** `{voice.duration}s`\n\n"
                    f"Aap kisi bhi user ke DM me `.{slot}` likhenge toh ye Voice Note send hoga! 🎤🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 5. Round Video Note
            elif message.video_note:
                vn = message.video_note
                vn_file = await bot.get_file(vn.file_id)
                save_path = os.path.join(MEDIA_DIR, f"quick_{user_id}_slot{slot}_videonote.mp4")
                await bot.download_file(vn_file.file_path, save_path)

                await database.update_quick_shortcut_slot(user_id, slot, "video_note", "📹 Round Video Note", save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Shortcut `.{slot}`: Round Video Note Saved!**\n\n"
                    f"Aap kisi bhi user ke DM me `.{slot}` likhenge toh ye Video Note send hoga! 📹🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 6. Audio File (MP3)
            elif message.audio:
                audio = message.audio
                a_file = await bot.get_file(audio.file_id)
                ext = os.path.splitext(audio.file_name or "audio.mp3")[1] or ".mp3"
                save_path = os.path.join(MEDIA_DIR, f"quick_{user_id}_slot{slot}_audio{ext}")
                await bot.download_file(a_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.update_quick_shortcut_slot(user_id, slot, "audio", caption, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Shortcut `.{slot}`: Audio File Saved!**\n\n"
                    f"Aap kisi bhi user ke DM me `.{slot}` likhenge toh ye Audio send hoga! 🎵🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 7. Document / File
            elif message.document:
                doc = message.document
                d_file = await bot.get_file(doc.file_id)
                ext = os.path.splitext(doc.file_name or "file.bin")[1] or ".bin"
                save_path = os.path.join(MEDIA_DIR, f"quick_{user_id}_slot{slot}_doc{ext}")
                await bot.download_file(d_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.update_quick_shortcut_slot(user_id, slot, "document", caption, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Shortcut `.{slot}`: Document File Saved!**\n\n"
                    f"Aap kisi bhi user ke DM me `.{slot}` likhenge toh ye File send hoga! 📁🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 8. Text Message (Preserving Telegram Premium Custom Emojis & HTML)
            elif message.text:
                new_quick_msg = message.html_text or message.text
                await database.update_quick_shortcut_slot(user_id, slot, "text", new_quick_msg, None)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"✅ **Shortcut `.{slot}`: Custom Text Message Updated!**\n\n"
                    "*(💡 Telegram Premium Custom Emojis & Rich Links Preserved)*\n\n"
                    "**Preview:**\n"
                    "━━━━━━━━━━━━━━━━━━━\n"
                    f"{new_quick_msg}\n"
                    "━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Aap kisi bhi user ke DM me `.{slot}` likhenge toh ye Text send hoga! 🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ]),
                    parse_mode=ParseMode.HTML
                )

        except Exception as e:
            logger.error(f"Error saving quick shortcut media/message: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **Error saving shortcut:** `{e}`\n\nKripya dobara send karein.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="btn_quick_triggers", style="default")]])
            )

    # ---------------- Step 9: WAITING_TRIGGER_KEY (A-Z or Custom Keyword Input) ----------------
    elif step == "WAITING_TRIGGER_KEY":
        trigger_input = message.text.strip().lower().lstrip(".")
        if not trigger_input:
            await message.answer("⚠️ Kripya valid trigger word/letter enter karein (jaise: `a`, `loss`, `qr`, `1`):")
            return

        user_states[user_id] = {
            "step": "WAITING_TRIGGER_MEDIA",
            "trigger_key": trigger_input
        }

        text = (
            f"✅ **Trigger Key Set: `{trigger_input}`**\n\n"
            f"👉 **Ab is `{trigger_input}` trigger ke liye message bhejein:**\n"
            "• 📝 Text Message (Custom Animated Emojis & Links)\n"
            "• 🎤 Voice Note (Audio Message)\n"
            "• 🖼️ Photo with Caption\n"
            "• 🎬 Video with Caption\n"
            "• 🎭 Telegram Sticker\n"
            "• 📁 Document / File\n\n"
            f"💡 *Jab bhi aap kisi user ke DM me `{trigger_input}` ya `.{trigger_input}` likhenge, yehi message deliver hoga!*"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Cancel", callback_data="btn_quick_triggers", style="default")]])
        await message.answer(text, reply_markup=keyboard)

    # ---------------- Step 10: WAITING_TRIGGER_MEDIA (A-Z Custom Trigger Media/Text Input) ----------------
    elif step == "WAITING_TRIGGER_MEDIA":
        trigger_key = state.get("trigger_key", "custom")
        status_msg = await message.answer(f"⏳ Trigger `{trigger_key}` ka message save kiya ja raha hai...")

        try:
            # 1. Telegram Premium Animated Sticker
            if message.sticker:
                ext = ".tgs" if message.sticker.is_animated else (".webm" if message.sticker.is_video else ".webp")
                save_path = os.path.join(MEDIA_DIR, f"cust_{user_id}_{trigger_key}_sticker{ext}")
                s_file = await bot.get_file(message.sticker.file_id)
                await bot.download_file(s_file.file_path, save_path)
                content = message.sticker.emoji or "🎭 Sticker"

                await database.add_or_update_custom_trigger(user_id, trigger_key, "sticker", content, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Trigger `{trigger_key}`: Telegram Sticker Successfully Saved!**\n\n"
                    f"Aap kisi bhi user ke DM me `{trigger_key}` ya `.{trigger_key}` likhenge toh ye Sticker send hoga! 🎭🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 2. Photo with Caption
            elif message.photo:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                save_path = os.path.join(MEDIA_DIR, f"cust_{user_id}_{trigger_key}_photo.jpg")
                await bot.download_file(p_file.file_path, save_path)
                caption = message.html_text or ""

                await database.add_or_update_custom_trigger(user_id, trigger_key, "photo", caption, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Trigger `{trigger_key}`: Photo + Custom Caption Saved!**\n\n"
                    f"📝 **Caption:**\n━━━━━━━━━━━━━━━━━━━\n{caption or '*(No Caption)*'}\n━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Aap kisi bhi user ke DM me `{trigger_key}` ya `.{trigger_key}` likhenge toh ye Photo send hoga! 🖼️🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 3. Video / GIF with Caption
            elif message.video or message.animation:
                v_obj = message.video or message.animation
                v_file = await bot.get_file(v_obj.file_id)
                save_path = os.path.join(MEDIA_DIR, f"cust_{user_id}_{trigger_key}_video.mp4")
                await bot.download_file(v_file.file_path, save_path)
                caption = message.html_text or ""

                await database.add_or_update_custom_trigger(user_id, trigger_key, "video", caption, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Trigger `{trigger_key}`: Video/GIF + Caption Saved!**\n\n"
                    f"📝 **Caption:**\n━━━━━━━━━━━━━━━━━━━\n{caption or '*(No Caption)*'}\n━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Aap kisi bhi user ke DM me `{trigger_key}` ya `.{trigger_key}` likhenge toh ye Video send hoga! 🎬🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 4. Voice Note (Audio Message)
            elif message.voice:
                voice = message.voice
                v_file = await bot.get_file(voice.file_id)
                save_path = os.path.join(MEDIA_DIR, f"cust_{user_id}_{trigger_key}_voice.ogg")
                await bot.download_file(v_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.add_or_update_custom_trigger(user_id, trigger_key, "voice", caption or "🎤 Voice Message", save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Trigger `{trigger_key}`: Voice Note Saved!**\n\n"
                    f"⏱️ **Duration:** `{voice.duration}s`\n\n"
                    f"Aap kisi bhi user ke DM me `{trigger_key}` ya `.{trigger_key}` likhenge toh ye Voice Note send hoga! 🎤🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 5. Round Video Note
            elif message.video_note:
                vn = message.video_note
                vn_file = await bot.get_file(vn.file_id)
                save_path = os.path.join(MEDIA_DIR, f"cust_{user_id}_{trigger_key}_videonote.mp4")
                await bot.download_file(vn_file.file_path, save_path)

                await database.add_or_update_custom_trigger(user_id, trigger_key, "video_note", "📹 Round Video Note", save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Trigger `{trigger_key}`: Round Video Note Saved!**\n\n"
                    f"Aap kisi bhi user ke DM me `{trigger_key}` ya `.{trigger_key}` likhenge toh ye Video Note send hoga! 📹🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 6. Audio File (MP3)
            elif message.audio:
                audio = message.audio
                a_file = await bot.get_file(audio.file_id)
                ext = os.path.splitext(audio.file_name or "audio.mp3")[1] or ".mp3"
                save_path = os.path.join(MEDIA_DIR, f"cust_{user_id}_{trigger_key}_audio{ext}")
                await bot.download_file(a_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.add_or_update_custom_trigger(user_id, trigger_key, "audio", caption, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Trigger `{trigger_key}`: Audio File Saved!**\n\n"
                    f"Aap kisi bhi user ke DM me `{trigger_key}` ya `.{trigger_key}` likhenge toh ye Audio send hoga! 🎵🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 7. Document / File
            elif message.document:
                doc = message.document
                d_file = await bot.get_file(doc.file_id)
                ext = os.path.splitext(doc.file_name or "file.bin")[1] or ".bin"
                save_path = os.path.join(MEDIA_DIR, f"cust_{user_id}_{trigger_key}_doc{ext}")
                await bot.download_file(d_file.file_path, save_path)
                caption = message.html_text or message.caption or ""

                await database.add_or_update_custom_trigger(user_id, trigger_key, "document", caption, save_path)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"🎉 **Trigger `{trigger_key}`: Document File Saved!**\n\n"
                    f"Aap kisi bhi user ke DM me `{trigger_key}` ya `.{trigger_key}` likhenge toh ye File send hoga! 📁🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ])
                )

            # 8. Text Message
            elif message.text:
                new_quick_msg = message.html_text or message.text
                await database.add_or_update_custom_trigger(user_id, trigger_key, "text", new_quick_msg, None)
                await cleanup_auth_state(user_id)

                await status_msg.edit_text(
                    f"✅ **Trigger `{trigger_key}`: Custom Text Message Saved!**\n\n"
                    "*(💡 Telegram Premium Custom Emojis & Rich Links Preserved)*\n\n"
                    "**Preview:**\n"
                    "━━━━━━━━━━━━━━━━━━━\n"
                    f"{new_quick_msg}\n"
                    "━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Aap kisi bhi user ke DM me `{trigger_key}` ya `.{trigger_key}` likhenge toh ye Text send hoga! 🚀",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⚡ Open Shortcuts Dashboard", callback_data="btn_quick_triggers", style="primary")],
                        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="btn_menu", style="default")]
                    ]),
                    parse_mode=ParseMode.HTML
                )

        except Exception as e:
            logger.error(f"Error saving custom trigger message: {e}", exc_info=True)
            await status_msg.edit_text(
                f"❌ **Error saving trigger:** `{e}`\n\nKripya dobara send karein.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data="btn_quick_triggers", style="default")]])
            )
