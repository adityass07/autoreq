import os
import sys
import asyncio
import random
import logging
from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    UserPrivacyRestrictedError,
    UserIsBlockedError,
    AuthKeyUnregisteredError,
    UserDeactivatedError,
    ChatAdminRequiredError
)
from config import API_ID, API_HASH
import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ClientManager")

# In-memory dictionary for active Telethon clients: {user_id: TelegramClient}
active_clients: dict[int, TelegramClient] = {}

# Set of already processed (owner_id, req_user_id, peer_id) to avoid duplicate DMs
processed_requests: set[str] = set()

def format_custom_message(template: str, user_entity, chat_title: str) -> str:
    """Format custom message template with dynamic user placeholders."""
    first_name = getattr(user_entity, "first_name", None) or "User"
    last_name = getattr(user_entity, "last_name", None) or ""
    full_name = f"{first_name} {last_name}".strip()
    username = f"@{user_entity.username}" if getattr(user_entity, "username", None) else first_name
    
    return template.format(
        name=first_name,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        username=username,
        channel=chat_title or "our channel"
    )

def get_clean_channel_ids(peer) -> tuple[str, str]:
    """Extract (raw_id, full_id) e.g. ('2148272343', '-1002148272343') from any peer object."""
    if isinstance(peer, int):
        return database.normalize_channel_id(peer)
    for attr in ["channel_id", "chat_id", "id"]:
        val = getattr(peer, attr, None)
        if val is not None:
            return database.normalize_channel_id(val)
    return database.normalize_channel_id(str(peer))

async def dispatch_single_dm_item(client: TelegramClient, recipient_id: int, req_name: str, chat_title: str, req_user, msg_type: str, template: str | None, media_path: str | None, owner_id: int, slot_num: int = 1):
    """Format and send one individual DM item with auto-retry and FloodWait protection."""
    # 1. Format text if template exists
    if template:
        try:
            if req_user:
                formatted_text = format_custom_message(template, req_user, chat_title)
            else:
                formatted_text = template.replace("{name}", req_name).replace("{channel}", chat_title)
        except Exception:
            formatted_text = template
    else:
        formatted_text = f"Hey {req_name}! Welcome to {chat_title}." if msg_type not in ("sticker", "voice", "video_note") else None

    # 2. Dispatch with auto-retry on FloodWait / Rate limits
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if msg_type == "sticker" and media_path and os.path.exists(media_path):
                await client.send_file(recipient_id, media_path)
                logger.info(f"✅ [Account {owner_id}] [Msg {slot_num}] Sticker DM sent to {req_name} ({recipient_id})")

            elif msg_type == "voice" and media_path and os.path.exists(media_path):
                caption_text = formatted_text if formatted_text and not formatted_text.startswith("🎤") else None
                try:
                    await client.send_file(recipient_id, media_path, voice_note=True, caption=caption_text, parse_mode="html")
                    logger.info(f"✅ [Account {owner_id}] [Msg {slot_num}] Voice Note DM sent to {req_name} ({recipient_id})")
                except Exception as ve:
                    logger.warning(f"Voice note with caption note: {ve}. Sending voice note and text separately...")
                    await client.send_file(recipient_id, media_path, voice_note=True)
                    if caption_text:
                        await client.send_message(recipient_id, caption_text, parse_mode="html")
                    logger.info(f"✅ [Account {owner_id}] [Msg {slot_num}] Voice Note + Text DM sent to {req_name} ({recipient_id})")

            elif msg_type == "video_note" and media_path and os.path.exists(media_path):
                await client.send_file(recipient_id, media_path, video_note=True)
                logger.info(f"✅ [Account {owner_id}] [Msg {slot_num}] Round Video Note DM sent to {req_name} ({recipient_id})")

            elif media_path and os.path.exists(media_path):
                await client.send_file(recipient_id, media_path, caption=formatted_text, parse_mode="html")
                logger.info(f"✅ [Account {owner_id}] [Msg {slot_num}] Media ({msg_type}) DM sent to {req_name} ({recipient_id})")

            elif formatted_text:
                await client.send_message(recipient_id, formatted_text, parse_mode="html")
                logger.info(f"✅ [Account {owner_id}] [Msg {slot_num}] Text/Emoji DM sent to {req_name} ({recipient_id})")

            return # Success!
        except FloodWaitError as fe:
            wait_time = fe.seconds + 1
            logger.warning(f"⏳ [Account {owner_id}] Rate-limited (FloodWait). Waiting {wait_time}s before retrying for {req_name}...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            err_str = str(e)
            if "Too many requests" in err_str or "flood" in err_str.lower():
                wait_sec = 2.0 * (attempt + 1)
                logger.warning(f"⏳ [Account {owner_id}] Concurrent spike for {req_name}. Retrying in {wait_sec}s...")
                await asyncio.sleep(wait_sec)
            else:
                logger.error(f"❌ [Account {owner_id}] Error dispatching [Msg {slot_num}] ({msg_type}) to {req_name} ({recipient_id}): {e}")
                raise e

async def process_single_join_request(client: TelegramClient, owner_id: int, peer, req_user_id: int, chat_title: str | None = None):
    """Core logic to send DM and auto-approve a single join requester INSTANTLY with strict 1-time DM protection."""
    # 1. In-memory fast deduplication
    request_key = f"{owner_id}_{req_user_id}"
    if request_key in processed_requests:
        user_data = await database.get_user(owner_id)
        if user_data and user_data.get("auto_approve") == 1:
            try:
                await client(functions.messages.HideChatJoinRequestRequest(approved=True, peer=peer, user_id=req_user_id))
            except Exception:
                pass
        return
    processed_requests.add(request_key)

    # 2. Database persistent deduplication (checks if user ever got a DM from this account)
    already_sent = await database.has_already_received_dm(owner_id, req_user_id)
    if already_sent:
        logger.info(f"🚫 [Account {owner_id}] User {req_user_id} already received a DM previously. Skipping duplicate DM!")
        user_data = await database.get_user(owner_id)
        if user_data and user_data.get("auto_approve") == 1:
            try:
                await client(functions.messages.HideChatJoinRequestRequest(approved=True, peer=peer, user_id=req_user_id))
                logger.info(f"🎉 [Account {owner_id}] Auto-approved re-join request for user {req_user_id} (No duplicate DM sent)")
            except Exception:
                pass
        return

    user_data = await database.get_user(owner_id)
    if not user_data:
        return

    if not user_data.get("is_active", 1):
        return

    # Fetch user entity and chat entity concurrently
    try:
        req_user = await client.get_entity(req_user_id)
        req_name = getattr(req_user, "first_name", "User") or "User"
    except Exception:
        req_user = None
        req_name = "User"

    chat_username = None
    if not chat_title:
        try:
            chat_entity = await client.get_entity(peer)
            chat_title = getattr(chat_entity, "title", "Channel") or "Channel"
            chat_username = getattr(chat_entity, "username", None)
        except Exception:
            chat_title = "Channel"
    else:
        try:
            chat_entity = await client.get_entity(peer)
            chat_username = getattr(chat_entity, "username", None)
        except Exception:
            pass

    # 3. Target Channel Filter Check (If user specified custom target channels)
    target_channels = user_data.get("target_channels") or []
    if target_channels:
        peer_raw_id, peer_full_id = get_clean_channel_ids(peer)
        
        matched = False
        for tc in target_channels:
            tc_id = str(tc.get("id", ""))
            tc_user = tc.get("username", "")
            if tc_id and (tc_id == peer_raw_id or tc_id == peer_full_id or tc_id.endswith(peer_raw_id)):
                matched = True
                break
            if tc_user and chat_username and tc_user.lower().lstrip("@") == chat_username.lower().lstrip("@"):
                matched = True
                break

        if not matched:
            logger.info(f"⏭️ [Account {owner_id}] Request for [{chat_title}] skipped (Not in Target Channels Filter).")
            return

    # 4. Channel ON/OFF Toggle Check (If user toggled this specific channel OFF)
    disabled_channels = user_data.get("disabled_channels") or []
    if disabled_channels:
        peer_raw_id, peer_full_id = get_clean_channel_ids(peer)
        is_disabled = (peer_raw_id in disabled_channels) or (peer_full_id in disabled_channels) or any(
            str(d) in (peer_raw_id, peer_full_id) or str(d).endswith(peer_raw_id) for d in disabled_channels
        )
        if is_disabled:
            logger.info(f"⏸️ [Account {owner_id}] Request for [{chat_title}] ({peer_full_id}) skipped (Channel Auto-DM is turned OFF).")
            return

    logger.info(f"⚡ [Account {owner_id}] Instant DM to {req_name} ({req_user_id}) for [{chat_title}]")

    # Optional randomized delay (only if configured > 0)
    min_d = user_data.get("min_delay", 0)
    max_d = user_data.get("max_delay", 0)
    if max_d > 0 and min_d >= 0:
        delay = random.uniform(min_d, max_d)
        if delay > 0:
            await asyncio.sleep(delay)

    # 1. Dispatch Message 1 (First Message)
    msg_1_type = user_data.get("msg_1_type") or user_data.get("msg_type", "text")
    msg_1_content = user_data.get("msg_1_content") or user_data.get("custom_message") or "Hey {name}, welcome to {channel}!"
    msg_1_media = user_data.get("msg_1_media_path") or user_data.get("media_path")

    try:
        await dispatch_single_dm_item(
            client=client,
            recipient_id=req_user_id,
            req_name=req_name,
            chat_title=chat_title,
            req_user=req_user,
            msg_type=msg_1_type,
            template=msg_1_content,
            media_path=msg_1_media,
            owner_id=owner_id,
            slot_num=1
        )

        # 2. Dispatch Message 2 (Second Message - Follow-up) if enabled
        msg_2_enabled = user_data.get("msg_2_enabled", False)
        msg_2_type = user_data.get("msg_2_type", "text")
        msg_2_content = user_data.get("msg_2_content")
        msg_2_media = user_data.get("msg_2_media_path")

        if msg_2_enabled and (msg_2_content or msg_2_media):
            # Short natural 0.4s pause so Telegram delivers 1st message first, then 2nd message
            await asyncio.sleep(0.4)
            await dispatch_single_dm_item(
                client=client,
                recipient_id=req_user_id,
                req_name=req_name,
                chat_title=chat_title,
                req_user=req_user,
                msg_type=msg_2_type,
                template=msg_2_content,
                media_path=msg_2_media,
                owner_id=owner_id,
                slot_num=2
            )

        # 3. Auto-Approve if enabled
        if user_data.get("auto_approve") == 1:
            try:
                await client(functions.messages.HideChatJoinRequestRequest(
                    approved=True,
                    peer=peer,
                    user_id=req_user_id
                ))
                logger.info(f"🎉 [Account {owner_id}] Auto-approved join request for {req_name}")
            except Exception as e:
                logger.warning(f"Could not auto-approve request: {e}")

        # 4. Log the sent DM in MongoDB
        await database.log_dm_sent(owner_id, req_user_id, req_name, chat_title)

    except FloodWaitError as e:
        logger.warning(f"⚠️ Telegram FloodWait for account {owner_id}: Sleeping for {e.seconds} seconds")
        await asyncio.sleep(e.seconds)
    except UserPrivacyRestrictedError:
        logger.info(f"⚠️ User {req_user_id} privacy settings prevent DMs.")
    except UserIsBlockedError:
        logger.info(f"⚠️ Cannot DM {req_user_id} (Blocked).")
    except Exception as e:
        logger.error(f"❌ Error sending DM from account {owner_id}: {e}")

def extract_join_updates(event) -> list:
    """Recursively extract join requester tuples (peer, user_id) from any MTProto update type."""
    results = []
    if isinstance(event, types.UpdateBotChatInviteRequester):
        results.append((event.peer, event.user_id))
    elif hasattr(event, "updates") and isinstance(event.updates, list):
        for u in event.updates:
            results.extend(extract_join_updates(u))
    elif hasattr(event, "update"):
        results.extend(extract_join_updates(event.update))
    return results

async def pending_requests_scanner_loop(client: TelegramClient, owner_id: int):
    """Zero-flood background scanner with cached admin channels."""
    logger.info(f"⚡ Zero-flood scanner active for account {owner_id}")
    
    cached_channels = []
    last_cache_time = 0

    while user_id_is_active(owner_id):
        # Auto-reconnect if client disconnected due to temporary network drop
        if not client.is_connected():
            try:
                await client.connect()
            except Exception as conn_err:
                logger.debug(f"Client reconnect attempt for {owner_id}: {conn_err}")
                await asyncio.sleep(5)
                continue

        now = asyncio.get_event_loop().time()
        
        # Refresh admin channels list every 60 seconds (prevents GetDialogsRequest flood wait)
        if now - last_cache_time > 60 or not cached_channels:
            try:
                new_list = []
                async for dialog in client.iter_dialogs(limit=30):
                    if dialog.is_channel or dialog.is_group:
                        new_list.append((dialog.input_entity, dialog.name))
                cached_channels = new_list
                last_cache_time = now
            except Exception as e:
                logger.debug(f"Dialog cache update note: {e}")

        try:
            user_data = await database.get_user(owner_id)
            if user_data and user_data.get("is_active", 1):
                disabled_channels = user_data.get("disabled_channels") or []
                for peer, chat_name in cached_channels:
                    if not user_id_is_active(owner_id):
                        break

                    # Skip disabled channels directly in scanner
                    peer_raw, peer_full = get_clean_channel_ids(peer)
                    if (peer_raw in disabled_channels) or (peer_full in disabled_channels):
                        continue

                    try:
                        res = await client(functions.messages.GetChatInviteImportersRequest(
                            peer=peer,
                            requested=True,
                            limit=20,
                            offset_date=None,
                            offset_user=types.InputUserEmpty()
                        ))
                        if res and res.importers:
                            for importer in res.importers:
                                req_uid = importer.user_id
                                asyncio.create_task(process_single_join_request(
                                    client=client,
                                    owner_id=owner_id,
                                    peer=peer,
                                    req_user_id=req_uid,
                                    chat_title=chat_name
                                ))
                                await asyncio.sleep(0.4) # Natural anti-flood stagger prevents SendMessageRequest rate-limit
                    except (ChatAdminRequiredError, FloodWaitError):
                        pass
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Scanner error on {owner_id}: {e}")

        # High speed check interval: 1.5 seconds (zero flood wait)
        await asyncio.sleep(1.5)

def user_id_is_active(user_id: int) -> bool:
    return user_id in active_clients

async def start_client(user_id: int, session_string: str) -> bool:
    """Start and register a user's Telethon client in the background."""
    if user_id in active_clients:
        await stop_client(user_id)

    try:
        session = StringSession(session_string)
        client = TelegramClient(
            session=session,
            api_id=API_ID,
            api_hash=API_HASH,
            device_model="Telegram Desktop",
            system_version="Windows 11 x64",
            app_version="5.4.1 x64",
            lang_code="en",
            system_lang_code="en-US"
        )

        # 1. Real-time raw event handler
        @client.on(events.Raw)
        async def on_raw_update(event):
            try:
                # Direct check for PendingJoinRequests update
                if isinstance(event, types.UpdatePendingJoinRequests):
                    peer = getattr(event, "peer", None) or getattr(event, "channel_id", None)
                    if peer:
                        asyncio.create_task(client(functions.messages.GetChatInviteImportersRequest(
                            peer=peer,
                            requested=True,
                            limit=20,
                            offset_date=None,
                            offset_user=types.InputUserEmpty()
                        )))
                    return

                # Check all requester updates
                requesters = extract_join_updates(event)
                for peer, req_user_id in requesters:
                    asyncio.create_task(process_single_join_request(client, user_id, peer, req_user_id))
            except Exception as e:
                logger.error(f"Error handling raw update: {e}", exc_info=True)

        # 2. Outgoing DM Quick Shortcuts (A-Z, Custom Words, 1-10, with or without '.' prefix!) - Strictly Private DMs
        @client.on(events.NewMessage(outgoing=True))
        async def on_outgoing_userbot_command(event):
            try:
                # Sirf personal DMs / Private chats me chalega, group/channel me nahi
                if not event.is_private:
                    return

                raw_text = (event.raw_text or "").strip()
                if not raw_text:
                    return
                
                cmd = raw_text.lower().strip()
                clean_key = cmd.lstrip(".").strip()
                
                u_data = await database.get_user(user_id)
                if not u_data:
                    return

                peer = event.peer_id
                try:
                    chat = await event.get_chat()
                    first_name = getattr(chat, "first_name", getattr(chat, "title", "User")) or "User"
                except Exception:
                    chat = None
                    first_name = "User"

                # Check for combination command .both, .12, .all, both, 12, all
                if cmd in [".both", ".12", ".all", "both", "12", "all"]:
                    slot_1 = database.get_quick_shortcut_data(u_data, 1)
                    if slot_1["is_set"]:
                        try:
                            await event.delete()
                        except Exception:
                            pass

                        await dispatch_single_dm_item(
                            client=client,
                            recipient_id=peer,
                            req_name=first_name,
                            chat_title="Direct Chat",
                            req_user=chat,
                            msg_type=slot_1["type"],
                            template=slot_1["content"],
                            media_path=slot_1["media_path"],
                            owner_id=user_id,
                            slot_num=1
                        )
                        slot_2 = database.get_quick_shortcut_data(u_data, 2)
                        if slot_2["is_set"]:
                            await asyncio.sleep(0.4)
                            await dispatch_single_dm_item(
                                client=client,
                                recipient_id=peer,
                                req_name=first_name,
                                chat_title="Direct Chat",
                                req_user=chat,
                                msg_type=slot_2["type"],
                                template=slot_2["content"],
                                media_path=slot_2["media_path"],
                                owner_id=user_id,
                                slot_num=2
                            )
                        logger.info(f"⚡ [DM Quick Shortcut {user_id}] '{cmd}' sent Shortcut 1 & 2 to {peer}")
                        return

                # 1. First check in custom_triggers (A to Z, custom words, e.g. 'a', 'b', 'loss', 'qr')
                custom_triggers = u_data.get("custom_triggers", {})
                matched_trigger = custom_triggers.get(clean_key)
                
                if matched_trigger and (matched_trigger.get("content") or matched_trigger.get("media_path")):
                    try:
                        await event.delete()
                    except Exception:
                        pass
                    
                    await dispatch_single_dm_item(
                        client=client,
                        recipient_id=peer,
                        req_name=first_name,
                        chat_title="Direct Chat",
                        req_user=chat,
                        msg_type=matched_trigger.get("type", "text"),
                        template=matched_trigger.get("content"),
                        media_path=matched_trigger.get("media_path"),
                        owner_id=user_id,
                        slot_num=1
                    )
                    logger.info(f"⚡ [DM Custom Trigger {user_id}] '{raw_text}' triggered key '{clean_key}' for {peer}")
                    return

                # 2. Check in numeric slots (1 to 10)
                clean_num_str = clean_key.lstrip("m")
                if clean_num_str.isdigit():
                    slot_num = int(clean_num_str)
                    if 1 <= slot_num <= 10:
                        slot_data = database.get_quick_shortcut_data(u_data, slot_num)
                        if slot_data["is_set"]:
                            try:
                                await event.delete()
                            except Exception:
                                pass
                            
                            await dispatch_single_dm_item(
                                client=client,
                                recipient_id=peer,
                                req_name=first_name,
                                chat_title="Direct Chat",
                                req_user=chat,
                                msg_type=slot_data["type"],
                                template=slot_data["content"],
                                media_path=slot_data["media_path"],
                                owner_id=user_id,
                                slot_num=slot_num
                            )
                            logger.info(f"⚡ [DM Quick Shortcut {user_id}] '{raw_text}' sent Shortcut {slot_num} to {peer}")

            except Exception as e:
                logger.error(f"Error handling quick trigger command on account {user_id}: {e}", exc_info=True)

        await client.connect()
        if not await client.is_user_authorized():
            logger.error(f"Session string for user {user_id} is unauthorized or expired.")
            await database.delete_user_session(user_id)
            return False

        me = await client.get_me()
        active_clients[user_id] = client
        logger.info(f"🚀 Started active Telethon client for user {user_id} (@{me.username or me.first_name})")

        # 2. Launch background pending requests scanner
        asyncio.create_task(pending_requests_scanner_loop(client, user_id))

        return True

    except (AuthKeyUnregisteredError, UserDeactivatedError):
        logger.error(f"Session expired or revoked for user {user_id}. Cleaning up...")
        await database.delete_user_session(user_id)
        return False
    except Exception as e:
        logger.error(f"Failed to start Telethon client for user {user_id}: {e}", exc_info=True)
        return False

async def stop_client(user_id: int):
    """Gracefully stop and disconnect a user's active client."""
    client = active_clients.pop(user_id, None)
    if client:
        try:
            if client.is_connected():
                await client.disconnect()
            logger.info(f"🛑 Disconnected client session for user {user_id}")
        except Exception as e:
            logger.warning(f"Error while stopping client {user_id}: {e}")

async def resolve_channel_info(user_id: int, channel_identifier: str) -> tuple[bool, dict | str]:
    """Resolve and return channel info {'id': int, 'title': str, 'username': str} using user's Telethon client."""
    client = active_clients.get(user_id)
    if not client or not client.is_connected():
        return False, "❌ Aapka account connected nahi hai. Pehle login karein."

    try:
        clean_input = channel_identifier.strip()
        if clean_input.startswith("https://t.me/"):
            clean_input = clean_input.replace("https://t.me/", "")
            if clean_input.startswith("+") or clean_input.startswith("joinchat/"):
                return False, "⚠️ Kripya channel ka @username (jaise `@mychannel`) ya Channel ID enter karein."

        entity = await client.get_entity(clean_input)
        chat_id = getattr(entity, "id", None)
        title = getattr(entity, "title", clean_input) or clean_input
        username = getattr(entity, "username", None)
        full_id = f"-100{chat_id}" if chat_id and not str(chat_id).startswith("-100") else str(chat_id)

        return True, {
            "id": full_id,
            "title": title,
            "username": f"@{username}" if username else None
        }
    except Exception as e:
        logger.error(f"Error resolving channel for user {user_id}: {e}", exc_info=True)
        return False, f"❌ Channel find nahi hua: {e}"

async def create_channel_join_link(user_id: int, channel_identifier: str) -> tuple[bool, str]:
    """Generate an official Admin Approval Join Link using the user's logged-in session."""
    client = active_clients.get(user_id)
    if not client or not client.is_connected():
        return False, "❌ Aapka account connected nahi hai. Pehle login karein."

    try:
        # Resolve peer
        entity = await client.get_entity(channel_identifier)
        res = await client(functions.messages.ExportChatInviteRequest(
            peer=entity,
            request_needed=True,
            title="Auto-DM Invite Link"
        ))
        return True, res.link
    except ChatAdminRequiredError:
        return False, "❌ Aapki personal ID is channel me Admin nahi hai (Invite links permission chahiye)!"
    except Exception as e:
        logger.error(f"Error creating join link for user {user_id}: {e}", exc_info=True)
async def get_user_admin_channels(user_id: int) -> list[dict]:
    """Fetch ONLY channels and groups where the user's logged-in session is Owner or Admin."""
    client = active_clients.get(user_id)
    if not client or not client.is_connected():
        return []

    channels = []
    try:
        dialogs = await client.get_dialogs(limit=100)
        for d in dialogs:
            if d.is_channel or d.is_group:
                entity = d.entity
                is_creator = getattr(entity, "creator", False)
                admin_rights = getattr(entity, "admin_rights", None)
                
                # STRICT FILTER: Sirf wahi channels aayenge jisme aap Owner ya Admin hain
                if not is_creator and not admin_rights:
                    continue

                role = "👑 Owner" if is_creator else "🛡️ Admin"
                raw_id = d.id
                full_id = f"-100{raw_id}" if not str(raw_id).startswith("-100") else str(raw_id)
                channels.append({
                    "id": full_id,
                    "raw_id": raw_id,
                    "title": d.name or "Channel",
                    "username": getattr(entity, "username", None),
                    "is_owner": is_creator,
                    "is_admin": True,
                    "role": role
                })
    except Exception as e:
        logger.error(f"Error fetching admin channels for {user_id}: {e}", exc_info=True)
    return channels

async def broadcast_message_to_channels(owner_id: int, msg_data: dict, channel_ids: list[str]) -> dict:
    """Broadcast a single message (text/media/voice/sticker) to multiple selected channels."""
    client = active_clients.get(owner_id)
    if not client or not client.is_connected():
        return {"success": 0, "failed": len(channel_ids), "error": "Account disconnected"}

    msg_type = msg_data.get("type", "text")
    text_content = msg_data.get("text")
    media_path = msg_data.get("media_path")
    
    results = {"success": 0, "failed": 0, "successful_channels": [], "failed_channels": []}

    for ch_id in channel_ids:
        raw_id, full_id = database.normalize_channel_id(ch_id)
        # Try both integer id and full channel id
        target_peer = int(full_id) if full_id.lstrip("-").isdigit() else full_id
        
        try:
            entity = await client.get_entity(target_peer)
            ch_name = getattr(entity, "title", "Channel")

            if msg_type == "text":
                await client.send_message(entity, text_content, parse_mode="html")
            elif msg_type == "sticker" and media_path and os.path.exists(media_path):
                await client.send_file(entity, media_path)
            elif msg_type == "voice" and media_path and os.path.exists(media_path):
                caption_text = text_content if text_content and not text_content.startswith("🎤") else None
                try:
                    await client.send_file(entity, media_path, voice_note=True, caption=caption_text, parse_mode="html")
                except Exception:
                    await client.send_file(entity, media_path, voice_note=True)
                    if caption_text:
                        await client.send_message(entity, caption_text, parse_mode="html")
            elif msg_type == "video_note" and media_path and os.path.exists(media_path):
                await client.send_file(entity, media_path, video_note=True)
            elif media_path and os.path.exists(media_path):
                await client.send_file(entity, media_path, caption=text_content, parse_mode="html")
            elif text_content:
                await client.send_message(entity, text_content, parse_mode="html")
            
            results["success"] += 1
            results["successful_channels"].append(ch_name)
            logger.info(f"📢 [Account {owner_id}] Broadcast posted to [{ch_name}] ({full_id})")
            
            # Staggered 0.4s delay between channels to avoid rate limits
            await asyncio.sleep(0.4)

        except FloodWaitError as e:
            logger.warning(f"FloodWait on broadcast to {full_id}: Sleeping for {e.seconds}s")
            await asyncio.sleep(e.seconds)
            results["failed"] += 1
            results["failed_channels"].append(str(ch_id))
        except Exception as e:
            logger.error(f"Failed to post broadcast to channel {ch_id}: {e}")
            results["failed"] += 1
            results["failed_channels"].append(str(ch_id))

    return results

async def init_all_clients():
    """Start all active sessions stored in the database on bot startup."""
    active_users = await database.get_all_active_users()
    logger.info(f"🔄 Initializing {len(active_users)} active user sessions from MongoDB...")
    
    for u in active_users:
        user_id = u["user_id"]
        session_str = u.get("session_string")
        if session_str:
            try:
                await start_client(user_id, session_str)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error starting client for {user_id}: {e}")
