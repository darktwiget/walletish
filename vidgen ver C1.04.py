



import os
import time
import requests
import json
import random
from datetime import timedelta, datetime
from lumaai import AsyncLumaAI
from google.cloud import storage
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, \
    ContextTypes, ConversationHandler
from telegram.constants import ParseMode
import asyncio
import logging
import sqlite3
from asyncio import to_thread
from xrpl.asyncio.clients import AsyncJsonRpcClient, AsyncWebsocketClient
from xrpl.models.requests import Subscribe, Tx

# Configuration
TELEGRAM_TOKEN = "7601707085:AAGmgn07jnxyIOeseZRfy8enP7a0xdioFfg"
SERVICE_ACCOUNT_KEY_PATH = "insert yourown gcs credintia,s "
GCS_BUCKET_NAME = "gcs bucket "
LUMA_API_KEY = "userr yourownluma api"
DB_FILE = "insert db here"
WALLET_ADDRESS = "put wallet you want tokens or xrp sent to."
ADMINS = ["Admin tools put tg username"]
XRPL_RPC_URL = "https://s1.ripple.com:51234/"
XRPL_WS_URL = "wss://s1.ripple.com"

# Initialize XRPL clients
xrpl_json_client = AsyncJsonRpcClient(XRPL_RPC_URL)

# Logging Setup
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot_logs.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Global Variables
user_quotas = {}
counter_data = {}
pending_deposits = {}

# Conversation States
AWAITING_OPTION = "awaiting_option"
AWAITING_PROMPT = "awaiting_prompt"
AWAITING_IMAGE = "awaiting_image"
AWAITING_START_FRAME_IMAGE = "awaiting_start_frame_image"
AWAITING_LOOP_FRAME_IMAGE = "awaiting_loop_frame_image"
AWAITING_END_FRAME_IMAGE = "awaiting_end_frame_image"
AWAITING_START_END_FIRST_IMAGE = "awaiting_start_end_first_image"
AWAITING_START_END_SECOND_IMAGE = "awaiting_start_end_second_image"
AWAITING_EXTEND_VIDEO_ID = "awaiting_extend_video_id"
AWAITING_REVERSE_EXTEND_VIDEO_ID = "awaiting_reverse_extend_video_id"
AWAITING_GROW_VIDEO_ID = "awaiting_grow_video_id"
AWAITING_GROW_END_IMAGE = "awaiting_grow_end_image"
AWAITING_REWIND_VIDEO_ID = "awaiting_rewind_video_id"
AWAITING_REWIND_START_IMAGE = "awaiting_rewind_start_image"
AWAITING_FIRST_BLEND_VIDEO_ID = "awaiting_first_blend_video_id"
AWAITING_SECOND_BLEND_VIDEO_ID = "awaiting_second_blend_video_id"
AWAITING_AUDIO_GENERATION_ID = "awaiting_audio_generation_id"
AWAITING_AUDIO_PROMPT = "awaiting_audio_prompt"
CURRENCY_SELECTION = "currency_selection"
SELECT_DEPOSIT_AMOUNT = "select_deposit_amount"
AWAITING_SETTINGS = "awaiting_settings"

# Issuer Addresses for Currencies
ISSUERS = {
    "Radiant": "rM957DcfnfEQBatTNZXh42WrfBURZPh75h",
    "XMeme": "r4UPddYeGeZgDhSGPkooURsQtmGda4oYQW",
    "Bear": "rBEARGUAsyu7tUw53rufQzFdWmJHpJEqFW"
}

# Helper Functions
def get_user_identifier(update):
    user = update.callback_query.from_user if update.callback_query else update.message.from_user
    return f"@{user.username}" if user.username else str(user.id)

def initialize_database():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_identifier TEXT UNIQUE,
            quota INTEGER DEFAULT 4,
            counter INTEGER DEFAULT 0
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS generation_ids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_identifier TEXT,
            generation_id TEXT UNIQUE,
            nickname TEXT,
            FOREIGN KEY (user_identifier) REFERENCES user_data(user_identifier)
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS used_transactions (
            tx_hash TEXT PRIMARY KEY,
            user_identifier TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT user_identifier, quota, counter FROM user_data")
    for row in cursor.fetchall():
        user_quotas[row["user_identifier"]] = row["quota"]
        counter_data[row["user_identifier"]] = row["counter"]
    conn.close()

initialize_database()

def upload_to_gcs(file_path, bucket_name, expiration=timedelta(hours=1)):
    credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_KEY_PATH)
    client = storage.Client(credentials=credentials)
    bucket = client.bucket(bucket_name)
    blob_name = os.path.basename(file_path)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(file_path, timeout=300)
    return blob.generate_signed_url(version="v4", expiration=expiration, method="GET")

async def upload_video_to_gcs_and_get_url(video_file_path, bucket_name, expiration=timedelta(days=7)):
    signed_url = await to_thread(upload_to_gcs, video_file_path, bucket_name, expiration)
    return signed_url

async def check_and_deduct_quota(user_identifier, update):
    conn = sqlite3.connect(DB_FILE)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT quota FROM user_data WHERE user_identifier = ?", (user_identifier,))
        result = cursor.fetchone()
        quota = result[0] if result else 4

        if quota <= 0:
            await update.message.reply_text("🌈 Your usage quota is exhausted. Use /letsboogie to refill.")
            return False

        cursor.execute(
            "INSERT INTO user_data (user_identifier, quota, counter) VALUES (?, ?, 0) "
            "ON CONFLICT(user_identifier) DO UPDATE SET quota = quota - 1, counter = counter + 1",
            (user_identifier, quota - 1)
        )
        conn.commit()
        user_quotas[user_identifier] = quota - 1
        counter_data[user_identifier] = counter_data.get(user_identifier, 0) + 1
        return True
    finally:
        conn.close()

async def refund_quota(user_identifier):
    conn = sqlite3.connect(DB_FILE)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT quota FROM user_data WHERE user_identifier = ?", (user_identifier,))
        result = cursor.fetchone()
        if result:
            quota = result[0]
            cursor.execute(
                "UPDATE user_data SET quota = ? WHERE user_identifier = ?",
                (quota + 1, user_identifier)
            )
            conn.commit()
            user_quotas[user_identifier] = quota + 1
    finally:
        conn.close()

async def save_generation_id(user_identifier, generation_id, nickname):
    conn = sqlite3.connect(DB_FILE)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO generation_ids (user_identifier, generation_id, nickname) VALUES (?, ?, ?)",
            (user_identifier, generation_id, nickname)
        )
        conn.commit()
    finally:
        conn.close()

async def get_generation_id_by_nickname(user_identifier, nickname):
    conn = sqlite3.connect(DB_FILE)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT generation_id FROM generation_ids WHERE user_identifier = ? AND nickname = ?",
            (user_identifier, nickname)
        )
        result = cursor.fetchone()
        return result[0] if result else None
    finally:
        conn.close()

async def generate_video(prompt, update, context, gcs_url=None, resolution=None, keyframes=None,
                         loop=False, aspect_ratio=None, user_identifier=None, is_edit=False):
    client = AsyncLumaAI(auth_token=LUMA_API_KEY)
    kwargs = {
        "prompt": prompt,
        "model": "ray-2",
        "loop": loop,
        "resolution": resolution if resolution else "1080p",
        "duration": context.user_data.get("selected_duration", "5s") if not is_edit else "9s"
    }
    if aspect_ratio:
        kwargs["aspect_ratio"] = aspect_ratio
    if gcs_url and not keyframes:
        kwargs["keyframes"] = {"frame0": {"type": "image", "url": gcs_url}}
    elif keyframes:
        kwargs["keyframes"] = keyframes

    generation = await client.generations.create(**kwargs)
    await update.message.reply_text("🌈 Bringing your Dream to life! This may take a moment...")

    max_retries = 100
    for _ in range(max_retries):
        generation = await client.generations.get(id=generation.id)
        if generation.state == "completed":
            video_url = generation.assets.video
            local_filename = f"{generation.id}.mp4"
            with open(local_filename, "wb") as file:
                file.write(requests.get(video_url).content)
            return local_filename, generation.id
        elif generation.state == "failed":
            raise RuntimeError(f"Generation failed: {generation.failure_reason}")
        await asyncio.sleep(3)
    else:
        raise TimeoutError("Video generation took too long")

async def add_audio_to_generation(generation_id, prompt, update, context, user_identifier):
    if not await check_and_deduct_quota(user_identifier, update):
        return

    url = f"https://api.lumalabs.ai/dream-machine/v1/generations/{generation_id}/audio"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {LUMA_API_KEY}"
    }
    payload = {
        "generation_type": "add_audio",
        "prompt": prompt,
        "negative_prompt": "static"
    }

    try:
        # Submit the audio addition request
        logger.debug(f"Submitting audio request for {generation_id}: {json.dumps(payload)}")
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        response_data = response.json()
        logger.debug(f"Audio POST response: {json.dumps(response_data, indent=2)}")

        # Check if the POST response contains a new generation ID
        new_generation_id = response_data.get("id", generation_id)  # Fallback to original ID if not provided
        if new_generation_id != generation_id:
            logger.info(f"New generation ID for audio: {new_generation_id}")
        else:
            logger.debug("No new generation ID in POST response, using original ID")

        # Poll the new generation ID (or original if no new ID was provided)
        status_url = f"https://api.lumalabs.ai/dream-machine/v1/generations/{new_generation_id}"
        await update.message.reply_text("🌈 Adding audio to your video... Please wait!")

        max_retries = 30  # Up to 150 seconds
        retry_interval = 5
        for attempt in range(max_retries):
            status_response = requests.get(status_url, headers=headers)
            status_response.raise_for_status()
            status = status_response.json()
            logger.debug(f"Status check {attempt + 1}/{max_retries} for {new_generation_id}: {json.dumps(status, indent=2)}")

            if status.get("state") == "completed":
                video_url = status.get("assets", {}).get("video")
                if not video_url:
                    raise ValueError("No video URL found in completed generation")
                await update.message.reply_text(
                    f"🌈 Audio added successfully! View: {video_url}\nID: {new_generation_id}",
                    parse_mode=ParseMode.HTML
                )
                context.user_data["last_generation_id"] = new_generation_id
                await ask_nickname(update, context)
                return
            elif status.get("state") == "failed":
                raise RuntimeError(f"Audio addition failed: {status.get('failure_reason', 'Unknown error')}")

            await asyncio.sleep(retry_interval)

        # If we timeout, suggest checking manually
        await update.message.reply_text(
            f"🌈 Audio processing timed out. Check the generation ID {new_generation_id} later or try again."
        )
        logger.warning(f"Timed out waiting for audio on generation {new_generation_id}")

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed for {generation_id}: {str(e)}")
        await refund_quota(user_identifier)
        await update.message.reply_text(
            "🌈 Sorry, there was an error communicating with the audio service."
        )
    except Exception as e:
        logger.error(f"Error adding audio to {generation_id}: {str(e)}")
        await refund_quota(user_identifier)
        await update.message.reply_text(
            "🌈 Sorry, there was an error adding audio to the video. Please try again later."
        )

async def modify_user_data(user_identifier, quota_increment=0, counter_increment=0):
    new_quota = user_quotas.get(user_identifier, 4) + quota_increment
    counter_data[user_identifier] = counter_data.get(user_identifier, 0) + counter_increment
    user_quotas[user_identifier] = new_quota

    conn = sqlite3.connect(DB_FILE)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_data (user_identifier, quota, counter)
            VALUES (?, ?, ?)
            ON CONFLICT(user_identifier)
            DO UPDATE SET quota = ?, counter = ?
        """, (user_identifier, new_quota, counter_data[user_identifier], new_quota, counter_data[user_identifier]))
        conn.commit()
    finally:
        conn.close()

async def generate_and_send_video(update, context, user_identifier, frame_type, gcs_url=None):
    if not await check_and_deduct_quota(user_identifier, update):
        return
    aspect_ratio = context.user_data.get("selected_aspect", "16:9")
    resolution = context.user_data.get("selected_resolution", "1080p")
    loop = context.user_data.get("selected_loop", False)

    client = AsyncLumaAI(auth_token=LUMA_API_KEY)

    if frame_type in ["extend", "rewind", "grow", "rewind_with_start", "blend"]:
        video_ids = []
        if frame_type == "extend":
            video_ids.append(context.user_data["extend_video_id"])
        elif frame_type == "rewind":
            video_ids.append(context.user_data["rewind_video_id"])
        elif frame_type == "grow":
            video_ids.append(context.user_data["grow_video_id"])
        elif frame_type == "rewind_with_start":
            video_ids.append(context.user_data["rewind_video_id"])
        elif frame_type == "blend":
            video_ids.extend([context.user_data["first_blend_video_id"], context.user_data["second_blend_video_id"]])

        for video_id in video_ids:
            generation = await client.generations.get(id=video_id)
            if generation.state != "completed":
                await update.message.reply_text("🌈 Source video is not completed. Please try again later.")
                await refund_quota(user_identifier)
                return

    if frame_type == "extend":
        keyframes = {"frame0": {"type": "generation", "id": context.user_data["extend_video_id"]}}
        prompt = "A teddy bear in sunglasses playing electric guitar and dancing"
    elif frame_type == "rewind":
        keyframes = {"frame1": {"type": "generation", "id": context.user_data["rewind_video_id"]}}
        prompt = "A teddy bear in sunglasses playing electric guitar and dancing"
    elif frame_type == "grow":
        keyframes = {
            "frame0": {"type": "generation", "id": context.user_data["grow_video_id"]},
            "frame1": {"type": "image", "url": gcs_url}
        }
        prompt = "Low-angle shot of a majestic tiger prowling through a snowy landscape"
    elif frame_type == "rewind_with_start":
        keyframes = {
            "frame0": {"type": "image", "url": gcs_url},
            "frame1": {"type": "generation", "id": context.user_data["rewind_video_id"]}
        }
        prompt = "Low-angle shot of a majestic tiger prowling through a snowy landscape"
    elif frame_type == "blend":
        keyframes = {
            "frame0": {"type": "generation", "id": context.user_data["first_blend_video_id"]},
            "frame1": {"type": "generation", "id": context.user_data["second_blend_video_id"]}
        }
        prompt = "A teddy bear in sunglasses playing electric guitar and dancing"

    try:
        video_file_path, generation_id = await generate_video(
            prompt=prompt,
            update=update,
            context=context,
            resolution=resolution,
            keyframes=keyframes,
            loop=loop,
            aspect_ratio=aspect_ratio,
            user_identifier=user_identifier,
            is_edit=True
        )
        signed_url = await upload_video_to_gcs_and_get_url(video_file_path, GCS_BUCKET_NAME)
        await update.message.reply_text(
            f"🌈 Dream video ready! View <a href=\"{signed_url}\">UwU</a> ID: {generation_id}",
            parse_mode=ParseMode.HTML)
        context.user_data["last_generation_id"] = generation_id
        await ask_nickname(update, context)
        if os.path.exists(video_file_path):
            os.remove(video_file_path)
    except Exception as e:
        logger.error(f"Error during video generation or upload: {e}")
        await refund_quota(user_identifier)
        await update.message.reply_text("🌈 Sorry, there was an error generating or uploading the Dream video.")

async def resolve_generation_id(user_identifier, input_str):
    client = AsyncLumaAI(auth_token=LUMA_API_KEY)
    try:
        generation = await client.generations.get(id=input_str)
        return input_str
    except Exception:
        gen_id = await get_generation_id_by_nickname(user_identifier, input_str)
        return gen_id if gen_id else None

# Menu Functions
async def show_main_options(update, context, user_identifier):
    keyboard = [
        [InlineKeyboardButton("❤️ Create", callback_data="create_menu"),
         InlineKeyboardButton("💛 Edit", callback_data="edit_menu")],
        [InlineKeyboardButton("🔍 Tools", callback_data="tools_menu"),
         InlineKeyboardButton("🚪 Exit", callback_data="exit")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"🌈 Welcome to Dream Machine! (Quota: {user_quotas.get(user_identifier, 4)})\n"
            f"What would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            f"🌈 Welcome to Dream Machine! (Quota: {user_quotas.get(user_identifier, 4)})\n"
            f"What would you like to do?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def show_create_menu(query, context, user_identifier):
    keyboard = [
        [InlineKeyboardButton("📝 Text to Video", callback_data="create_words"),
         InlineKeyboardButton("🖼️ Image to Video (Start)", callback_data="create_start_frame")],
        [InlineKeyboardButton("🔄 Image to Video (Loop)", callback_data="create_loop_frame"),
         InlineKeyboardButton("🏁 Image to Video (End)", callback_data="create_end_frame")],
        [InlineKeyboardButton("🎬 Start & End Frames", callback_data="create_start_end_frames"),
         InlineKeyboardButton("⚙️ Settings", callback_data="settings_menu")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    await query.edit_message_text(
        f"🌈 Create a Dream Video! (Quota: {user_quotas.get(user_identifier, 4)})\n"
        f"How would you like to start?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_settings_menu(query, context, user_identifier):
    current_duration = context.user_data.get("selected_duration", "5s")
    current_resolution = context.user_data.get("selected_resolution", "1080p")
    keyboard = [
        [InlineKeyboardButton(f"Duration: 5s {'✅' if current_duration == '5s' else ''}", callback_data="duration_5s"),
         InlineKeyboardButton(f"9s {'✅' if current_duration == '9s' else ''}", callback_data="duration_9s")],
        [InlineKeyboardButton(f"Resolution: 720p {'✅' if current_resolution == '720p' else ''}",
                              callback_data="resolution_720p"),
         InlineKeyboardButton(f"1080p {'✅' if current_resolution == '1080p' else ''}",
                              callback_data="resolution_1080p")],
        [InlineKeyboardButton("✅ Confirm", callback_data="confirm_settings"),
         InlineKeyboardButton("🔙 Back", callback_data="back_to_create")]
    ]
    await query.edit_message_text(
        f"🌈 Video Settings\nCurrent: {current_duration}, {current_resolution}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_edit_menu(query, context, user_identifier):
    keyboard = [
        [InlineKeyboardButton("🌱 Grow", callback_data="extend_video"),
         InlineKeyboardButton("⏪ Rewind", callback_data="reverse_extend_video")],
        [InlineKeyboardButton("🌿 Grow with End Pic", callback_data="extend_with_end"),
         InlineKeyboardButton("⏮️ Rewind with Start Pic", callback_data="rewind_with_start")],
        [InlineKeyboardButton("🔄 Blend Two", callback_data="interpolate_videos"),
         InlineKeyboardButton("🎵 Add Audio", callback_data="add_audio")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    await query.edit_message_text(
        f"🌈 Edit a Dream Video! (Quota: {user_quotas.get(user_identifier, 4)})\n"
        f"Choose an edit option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_tools_menu(query, context, user_identifier):
    keyboard = [
        [InlineKeyboardButton("📊 Check Quota", callback_data="check_quota"),
         InlineKeyboardButton("📜 View History", callback_data="view_history")],
        [InlineKeyboardButton("🔔 Check Status", callback_data="check_status"),
         InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
    ]
    await query.edit_message_text(
        f"🌈 Tools Menu (Quota: {user_quotas.get(user_identifier, 4)})\n"
        f"Select a tool:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# WebSocket Monitoring with Transaction Fixes
async def monitor_transactions(application):
    while True:
        try:
            async with AsyncWebsocketClient(XRPL_WS_URL) as client:
                logger.info(f"Subscribing to account: {WALLET_ADDRESS}")
                await client.send(Subscribe(accounts=[WALLET_ADDRESS]))
                async for message in client:
                    logger.debug(f"Full message received: {message}")
                    if message.get("type") == "transaction":
                        tx = message.get("tx_json")
                        if tx and tx.get("TransactionType") == "Payment":
                            logger.debug(f"Received transaction: {tx}")
                            if tx.get("Destination") != WALLET_ADDRESS:
                                logger.debug(f"Destination mismatch: expected {WALLET_ADDRESS}, got {tx.get('Destination')}")
                                continue
                            meta = message.get("meta")
                            if not meta or meta.get("TransactionResult") != "tesSUCCESS":
                                logger.debug(f"Transaction not successful or meta missing: {meta}")
                                continue
                            delivered_amount = meta.get("delivered_amount")
                            if not delivered_amount:
                                logger.debug("No delivered_amount in meta")
                                continue

                            for user_identifier, deposit in list(pending_deposits.items()):
                                if verify_amount(delivered_amount, deposit, tx):
                                    tx_hash = message["hash"]
                                    conn = sqlite3.connect(DB_FILE)
                                    cursor = conn.cursor()
                                    cursor.execute("SELECT * FROM used_transactions WHERE tx_hash = ?", (tx_hash,))
                                    if cursor.fetchone():
                                        logger.debug(f"Transaction {tx_hash} already processed")
                                        conn.close()
                                        continue

                                    await modify_user_data(user_identifier, quota_increment=deposit["increment_value"])
                                    cursor.execute("INSERT INTO used_transactions (tx_hash, user_identifier) VALUES (?, ?)",
                                                   (tx_hash, user_identifier))
                                    conn.commit()
                                    conn.close()

                                    await application.bot.send_message(
                                        chat_id=deposit["chat_id"],
                                        text=f"🌈 Transaction confirmed! Quota increased by {deposit['increment_value']}!"
                                    )
                                    del pending_deposits[user_identifier]
                                    logger.info(f"Processed deposit for {user_identifier}: {tx_hash}")
                                    break
                                else:
                                    logger.debug(f"Verification failed for user {user_identifier} with deposit: {deposit}")
        except Exception as e:
            logger.error(f"WebSocket error: {e}, reconnecting in 5 seconds...", exc_info=True)
            await asyncio.sleep(5)

def decode_currency(hex_currency):
    """Decode a hex-encoded XRPL currency string to its human-readable form."""
    try:
        hex_currency = hex_currency.rstrip('0')
        return bytes.fromhex(hex_currency).decode('utf-8')
    except Exception:
        logger.error(f"Failed to decode currency: {hex_currency}")
        return hex_currency

def verify_amount(delivered_amount, deposit, tx):
    """Verify if the delivered amount matches the pending deposit."""
    memos = tx.get("Memos", [])
    memo_match = any(m["Memo"]["MemoData"] == deposit["memo"].encode().hex().upper() for m in memos)
    if not memo_match:
        logger.debug(f"Memo mismatch: expected {deposit['memo'].encode().hex().upper()}, got {memos}")
        return False
    if deposit["is_xrp"]:
        if isinstance(delivered_amount, str) and delivered_amount == deposit["amount"]:
            return True
        logger.debug(f"XRP amount mismatch: expected {deposit['amount']}, got {delivered_amount}")
        return False
    if isinstance(delivered_amount, dict):
        currency = decode_currency(delivered_amount["currency"])
        if (currency.upper() == deposit["currency"] and
                delivered_amount["issuer"] == deposit["issuer"] and
                delivered_amount["value"] == deposit["amount"]):
            return True
        logger.debug(f"Currency mismatch: expected {deposit['currency']}, got {currency}")
        logger.debug(f"Issuer mismatch: expected {deposit['issuer']}, got {delivered_amount['issuer']}")
        logger.debug(f"Value mismatch: expected {deposit['amount']}, got {delivered_amount['value']}")
        return False
    logger.debug(f"Invalid delivered_amount format: {delivered_amount}")
    return False

# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌈 Welcome! Use /dream to generate videos or /letsboogie to manage quota.")

async def dream_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_identifier = get_user_identifier(update)
    context.user_data["initiator_id"] = user_identifier
    context.user_data["user_id"] = user_identifier
    context.user_data["state"] = AWAITING_OPTION
    await show_main_options(update, context, user_identifier)

async def lets_boogie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_identifier = get_user_identifier(update)
    context.user_data["initiator_id"] = user_identifier
    context.user_data["user_id"] = user_identifier
    keyboard = [
        [InlineKeyboardButton("Radiant", callback_data="select_radiant"),
         InlineKeyboardButton("XMeme", callback_data="select_xmeme")],
        [InlineKeyboardButton("Bear", callback_data="select_bear"),
         InlineKeyboardButton("XRP", callback_data="select_xrp")]
    ]
    await update.message.reply_text("🌈 Select currency to deposit:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CURRENCY_SELECTION

async def quota_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_identifier = get_user_identifier(update)
    await update.message.reply_text(
        f"🌈 Quota: {user_quotas.get(user_identifier, 4)}\nGenerations: {counter_data.get(user_identifier, 0)}"
    )

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_identifier = get_user_identifier(update)
    conn = sqlite3.connect(DB_FILE)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT generation_id, nickname FROM generation_ids WHERE user_identifier = ?",
                       (user_identifier,))
        results = cursor.fetchall()
        if not results:
            await update.message.reply_text("🌈 No history found.")
            return
        history_text = "🌈 Your History:\n" + "\n".join(
            f"ID: {gen_id} | Nickname: {nickname or 'None'}" for gen_id, nickname in results)
        await update.message.reply_text(history_text)
    finally:
        conn.close()

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_identifier = get_user_identifier(update)
    if not context.args:
        await update.message.reply_text("🌈 Provide a generation ID or nickname (e.g., /status myvideo).")
        return
    input_id = context.args[0]
    gen_id = await resolve_generation_id(user_identifier, input_id)
    if gen_id is None:
        await update.message.reply_text("🌈 Invalid ID or nickname.")
        return
    client = AsyncLumaAI(auth_token=LUMA_API_KEY)
    generation = await client.generations.get(id=gen_id)
    await update.message.reply_text(f"🌈 Status for {gen_id}: {generation.state}")

async def add_quota_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_identifier = get_user_identifier(update)
    if user_identifier not in ADMINS:
        await update.message.reply_text("🌈 Unauthorized.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("🌈 Usage: /add_quota <username> <amount>")
        return
    target, amount = context.args[0], int(context.args[1])
    await modify_user_data(target, quota_increment=amount)
    await update.message.reply_text(f"🌈 Added {amount} quota to {target}.")

# Conversation Handlers
async def handle_currency_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    currency = query.data
    if currency == "select_radiant":
        keyboard = [[InlineKeyboardButton(f"{amt}k Radiant ({cred} credits)", callback_data=f"radiant_{i + 1}")
                     for i, (amt, cred) in enumerate([(100, 5), (200, 10), (300, 15), (400, 20)])]]
    elif currency == "select_xmeme":
        keyboard = [[InlineKeyboardButton(f"{amt}k XMeme ({cred} credits)", callback_data=f"xmeme_{i + 1}")
                     for i, (amt, cred) in enumerate([(300, 5), (600, 10), (800, 15), (1000, 25)])]]
    elif currency == "select_bear":
        keyboard = [[InlineKeyboardButton(f"{amt}k Bear ({cred} credits)", callback_data=f"bear_{i + 1}")
                     for i, (amt, cred) in enumerate([(50, 5), (100, 10), (150, 15), (200, 20)])]]
    elif currency == "select_xrp":
        keyboard = [[InlineKeyboardButton(f"{amt} XRP ({cred} credits)", callback_data=f"xrp_{i + 1}")
                     for i, (amt, cred) in enumerate([(1, 5), (5, 25), (10, 50), (20, 100)])]]
    else:
        await query.edit_message_text("🌈 Invalid currency selection. Please try again with /letsboogie.")
        return ConversationHandler.END

    await query.edit_message_text(f"🌈 Select amount:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_DEPOSIT_AMOUNT

async def handle_boogie_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    option = query.data
    increments = {"radiant": [5, 10, 15, 20], "xmeme": [5, 10, 15, 25], "bear": [5, 10, 15, 20],
                  "xrp": [5, 25, 50, 100]}
    amounts = {"radiant": ["100000", "200000", "300000", "400000"], "xmeme": ["300000", "600000", "800000", "1000000"],
               "bear": ["50000", "100000", "150000", "200000"], "xrp": ["1000000", "5000000", "10000000", "20000000"]}
    currency = option.split("_")[0]
    idx = int(option.split("_")[1]) - 1
    user_identifier = context.user_data["user_id"]
    chat_id = update.effective_chat.id

    memo = f"{random.randint(0, 9999):04d}"
    deposit_info = {
        "currency": currency.upper() if currency != "xrp" else "XRP",
        "amount": amounts[currency][idx],
        "increment_value": increments[currency][idx],
        "issuer": ISSUERS.get(currency.capitalize()),
        "is_xrp": currency == "xrp",
        "chat_id": chat_id,
        "memo": memo
    }
    pending_deposits[user_identifier] = deposit_info

    payment = f"{int(deposit_info['amount']) / 1000000} XRP" if deposit_info["is_xrp"] else f"{deposit_info['amount']} {deposit_info['currency']}"
    await query.edit_message_text(
        f"🌈 Send {payment} to:\n{WALLET_ADDRESS}\nMemo: {memo}\n\nPlease wait a moment for confirmation..."
    )
    return ConversationHandler.END

async def ask_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Yes", callback_data="yes_nickname"),
                 InlineKeyboardButton("No", callback_data="no_nickname")]]
    await update.message.reply_text(
        f"🌈 Video generated! ID: {context.user_data['last_generation_id']}\nSave with a nickname?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return "ASK_NICKNAME"

async def handle_nickname_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "yes_nickname":
        await query.edit_message_text("🌈 Enter a nickname:")
        return "SAVE_NICKNAME"
    await query.edit_message_text("🌈 Video not saved with a nickname.")
    preserved_data = {
        "initiator_id": context.user_data.get("initiator_id"),
        "user_id": context.user_data.get("user_id"),
        "state": context.user_data.get("state")
    }
    context.user_data.clear()
    context.user_data.update(preserved_data)
    return ConversationHandler.END

async def save_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nickname = update.message.text.strip()
    await save_generation_id(context.user_data["user_id"], context.user_data["last_generation_id"], nickname)
    await update.message.reply_text(f"🌈 Saved as '{nickname}'!")
    preserved_data = {
        "initiator_id": context.user_data.get("initiator_id"),
        "user_id": context.user_data.get("user_id"),
        "state": context.user_data.get("state")
    }
    context.user_data.clear()
    context.user_data.update(preserved_data)
    return ConversationHandler.END

# Message Handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_identifier = get_user_identifier(update)
    state = context.user_data.get("state")
    if not state or user_identifier != context.user_data.get("initiator_id"):
        return

    if state == AWAITING_PROMPT:
        if not await check_and_deduct_quota(user_identifier, update):
            return
        prompt = update.message.text
        try:
            video_file_path, generation_id = await generate_video(
                prompt=prompt,
                update=update,
                context=context,
                resolution=context.user_data.get("selected_resolution", "1080p"),
                loop=context.user_data.get("selected_loop", False),
                aspect_ratio=context.user_data.get("selected_aspect", "16:9"),
                user_identifier=user_identifier,
                is_edit=False
            )
            signed_url = await upload_video_to_gcs_and_get_url(video_file_path, GCS_BUCKET_NAME)
            await update.message.reply_text(
                f"🌈 Dream video ready! View <a href=\"{signed_url}\">UwU</a> ID: {generation_id}",
                parse_mode=ParseMode.HTML)
            context.user_data["last_generation_id"] = generation_id
            await ask_nickname(update, context)
            if os.path.exists(video_file_path):
                os.remove(video_file_path)
        except Exception as e:
            logger.error(f"Error during video generation or upload: {e}")
            await refund_quota(user_identifier)
            await update.message.reply_text("🌈 Sorry, there was an error generating or uploading the Dream video.")
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_IMAGE:
        if not update.message.photo:
            return  # Silently ignore if no photo is provided
        if not await check_and_deduct_quota(user_identifier, update):
            return
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = f"{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        gcs_url = upload_to_gcs(file_path, GCS_BUCKET_NAME)
        prompt = update.message.caption or "Animate this image"
        try:
            video_file_path, generation_id = await generate_video(
                prompt=prompt,
                update=update,
                context=context,
                gcs_url=gcs_url,
                resolution=context.user_data.get("selected_resolution", "1080p"),
                loop=context.user_data.get("selected_loop", False),
                aspect_ratio=context.user_data.get("selected_aspect", "16:9"),
                user_identifier=user_identifier,
                is_edit=False
            )
            signed_url = await upload_video_to_gcs_and_get_url(video_file_path, GCS_BUCKET_NAME)
            await update.message.reply_text(
                f"🌈 Dream video ready! View <a href=\"{signed_url}\">UwU</a> ID: {generation_id}",
                parse_mode=ParseMode.HTML)
            context.user_data["last_generation_id"] = generation_id
            await ask_nickname(update, context)
            if os.path.exists(video_file_path):
                os.remove(video_file_path)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error during video generation or upload: {e}")
            await refund_quota(user_identifier)
            await update.message.reply_text("🌈 Sorry, there was an error generating or uploading the Dream video.")
            if os.path.exists(file_path):
                os.remove(file_path)
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_START_FRAME_IMAGE:
        if not update.message.photo:
            return  # Silently ignore if no photo is provided
        if not await check_and_deduct_quota(user_identifier, update):
            return
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = f"{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        gcs_url = upload_to_gcs(file_path, GCS_BUCKET_NAME)
        prompt = update.message.caption or "Low-angle shot of a majestic tiger prowling through a snowy landscape"
        try:
            video_file_path, generation_id = await generate_video(
                prompt=prompt,
                update=update,
                context=context,
                keyframes={"frame0": {"type": "image", "url": gcs_url}},
                resolution=context.user_data.get("selected_resolution", "1080p"),
                loop=False,
                aspect_ratio=context.user_data.get("selected_aspect", "16:9"),
                user_identifier=user_identifier,
                is_edit=False
            )
            signed_url = await upload_video_to_gcs_and_get_url(video_file_path, GCS_BUCKET_NAME)
            await update.message.reply_text(
                f"🌈 Dream video ready! View <a href=\"{signed_url}\">UwU</a> ID: {generation_id}",
                parse_mode=ParseMode.HTML)
            context.user_data["last_generation_id"] = generation_id
            await ask_nickname(update, context)
            if os.path.exists(video_file_path):
                os.remove(video_file_path)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error during video generation: {e}")
            await refund_quota(user_identifier)
            await update.message.reply_text("🌈 Sorry, there was an error generating the Dream video.")
            if os.path.exists(file_path):
                os.remove(file_path)
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_LOOP_FRAME_IMAGE:
        if not update.message.photo:
            return  # Silently ignore if no photo is provided
        if not await check_and_deduct_quota(user_identifier, update):
            return
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = f"{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        gcs_url = upload_to_gcs(file_path, GCS_BUCKET_NAME)
        prompt = update.message.caption or "Low-angle shot of a majestic tiger prowling through a snowy landscape"
        try:
            video_file_path, generation_id = await generate_video(
                prompt=prompt,
                update=update,
                context=context,
                keyframes={"frame0": {"type": "image", "url": gcs_url}},
                resolution=context.user_data.get("selected_resolution", "1080p"),
                loop=True,
                aspect_ratio=context.user_data.get("selected_aspect", "16:9"),
                user_identifier=user_identifier,
                is_edit=False
            )
            signed_url = await upload_video_to_gcs_and_get_url(video_file_path, GCS_BUCKET_NAME)
            await update.message.reply_text(
                f"🌈 Looping Dream video ready! View <a href=\"{signed_url}\">UwU</a> ID: {generation_id}",
                parse_mode=ParseMode.HTML)
            context.user_data["last_generation_id"] = generation_id
            await ask_nickname(update, context)
            if os.path.exists(video_file_path):
                os.remove(video_file_path)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error during video generation: {e}")
            await refund_quota(user_identifier)
            await update.message.reply_text("🌈 Sorry, there was an error generating the Dream video.")
            if os.path.exists(file_path):
                os.remove(file_path)
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_END_FRAME_IMAGE:
        if not update.message.photo:
            return  # Silently ignore if no photo is provided
        if not await check_and_deduct_quota(user_identifier, update):
            return
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = f"{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        gcs_url = upload_to_gcs(file_path, GCS_BUCKET_NAME)
        prompt = update.message.caption or "Low-angle shot of a majestic tiger prowling through a snowy landscape"
        try:
            video_file_path, generation_id = await generate_video(
                prompt=prompt,
                update=update,
                context=context,
                keyframes={"frame1": {"type": "image", "url": gcs_url}},
                resolution=context.user_data.get("selected_resolution", "1080p"),
                loop=False,
                aspect_ratio=context.user_data.get("selected_aspect", "16:9"),
                user_identifier=user_identifier,
                is_edit=False
            )
            signed_url = await upload_video_to_gcs_and_get_url(video_file_path, GCS_BUCKET_NAME)
            await update.message.reply_text(
                f"🌈 Dream video ready! View <a href=\"{signed_url}\">UwU</a> ID: {generation_id}",
                parse_mode=ParseMode.HTML)
            context.user_data["last_generation_id"] = generation_id
            await ask_nickname(update, context)
            if os.path.exists(video_file_path):
                os.remove(video_file_path)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error during video generation: {e}")
            await refund_quota(user_identifier)
            await update.message.reply_text("🌈 Sorry, there was an error generating the Dream video.")
            if os.path.exists(file_path):
                os.remove(file_path)
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_START_END_FIRST_IMAGE:
        if not update.message.photo:
            return  # Silently ignore if no photo is provided
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = f"{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        gcs_url = upload_to_gcs(file_path, GCS_BUCKET_NAME)
        context.user_data["start_frame_url"] = gcs_url
        context.user_data["start_end_prompt"] = update.message.caption or "Low-angle shot of a majestic tiger prowling through a snowy landscape"
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        await update.message.reply_text(
            "🌈 Upload the end image:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data["state"] = AWAITING_START_END_SECOND_IMAGE
        if os.path.exists(file_path):
            os.remove(file_path)

    elif state == AWAITING_START_END_SECOND_IMAGE:
        if not update.message.photo:
            return  # Silently ignore if no photo is provided
        if not await check_and_deduct_quota(user_identifier, update):
            return
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = f"{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        end_gcs_url = upload_to_gcs(file_path, GCS_BUCKET_NAME)
        prompt = context.user_data["start_end_prompt"]
        try:
            video_file_path, generation_id = await generate_video(
                prompt=prompt,
                update=update,
                context=context,
                keyframes={
                    "frame0": {"type": "image", "url": context.user_data["start_frame_url"]},
                    "frame1": {"type": "image", "url": end_gcs_url}
                },
                resolution=context.user_data.get("selected_resolution", "1080p"),
                loop=False,
                aspect_ratio=context.user_data.get("selected_aspect", "16:9"),
                user_identifier=user_identifier,
                is_edit=False
            )
            signed_url = await upload_video_to_gcs_and_get_url(video_file_path, GCS_BUCKET_NAME)
            await update.message.reply_text(
                f"🌈 Dream video ready! View <a href=\"{signed_url}\">UwU</a> ID: {generation_id}",
                parse_mode=ParseMode.HTML)
            context.user_data["last_generation_id"] = generation_id
            await ask_nickname(update, context)
            if os.path.exists(video_file_path):
                os.remove(video_file_path)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error during video generation: {e}")
            await refund_quota(user_identifier)
            await update.message.reply_text("🌈 Sorry, there was an error generating the Dream video.")
            if os.path.exists(file_path):
                os.remove(file_path)
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_EXTEND_VIDEO_ID:
        input_str = update.message.text
        gen_id = await resolve_generation_id(user_identifier, input_str)
        if gen_id is None:
            await update.message.reply_text("� based🌈 Invalid video ID or nickname. Please try again.")
            return
        context.user_data["extend_video_id"] = gen_id
        await generate_and_send_video(update, context, user_identifier, "extend")
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_REVERSE_EXTEND_VIDEO_ID:
        input_str = update.message.text
        gen_id = await resolve_generation_id(user_identifier, input_str)
        if gen_id is None:
            await update.message.reply_text("🌈 Invalid video ID or nickname. Please try again.")
            return
        context.user_data["rewind_video_id"] = gen_id
        await generate_and_send_video(update, context, user_identifier, "rewind")
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_GROW_VIDEO_ID:
        input_str = update.message.text
        gen_id = await resolve_generation_id(user_identifier, input_str)
        if gen_id is None:
            await update.message.reply_text("🌈 Invalid video ID or nickname. Please try again.")
            return
        context.user_data["grow_video_id"] = gen_id
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        await update.message.reply_text(
            "🌈 Upload the end image with a caption as the prompt:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data["state"] = AWAITING_GROW_END_IMAGE

    elif state == AWAITING_GROW_END_IMAGE:
        if not update.message.photo:
            return  # Silently ignore if no photo is provided
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = f"{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        gcs_url = upload_to_gcs(file_path, GCS_BUCKET_NAME)
        await generate_and_send_video(update, context, user_identifier, "grow", gcs_url)
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION
        if os.path.exists(file_path):
            os.remove(file_path)

    elif state == AWAITING_REWIND_VIDEO_ID:
        input_str = update.message.text
        gen_id = await resolve_generation_id(user_identifier, input_str)
        if gen_id is None:
            await update.message.reply_text("🌈 Invalid video ID or nickname. Please try again.")
            return
        context.user_data["rewind_video_id"] = gen_id
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        await update.message.reply_text(
            "🌈 Upload the start image with a caption as the prompt:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data["state"] = AWAITING_REWIND_START_IMAGE

    elif state == AWAITING_REWIND_START_IMAGE:
        if not update.message.photo:
            return  # Silently ignore if no photo is provided
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_path = f"{photo.file_id}.jpg"
        await file.download_to_drive(file_path)
        gcs_url = upload_to_gcs(file_path, GCS_BUCKET_NAME)
        await generate_and_send_video(update, context, user_identifier, "rewind_with_start", gcs_url)
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION
        if os.path.exists(file_path):
            os.remove(file_path)

    elif state == AWAITING_FIRST_BLEND_VIDEO_ID:
        input_str = update.message.text
        gen_id = await resolve_generation_id(user_identifier, input_str)
        if gen_id is None:
            await update.message.reply_text("🌈 Invalid video ID or nickname. Please try again.")
            return
        context.user_data["first_blend_video_id"] = gen_id
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        await update.message.reply_text(
            "🌈 Enter the second video ID to blend:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data["state"] = AWAITING_SECOND_BLEND_VIDEO_ID

    elif state == AWAITING_SECOND_BLEND_VIDEO_ID:
        input_str = update.message.text
        gen_id = await resolve_generation_id(user_identifier, input_str)
        if gen_id is None:
            await update.message.reply_text("🌈 Invalid video ID or nickname. Please try again.")
            return
        context.user_data["second_blend_video_id"] = gen_id
        await generate_and_send_video(update, context, user_identifier, "blend")
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == AWAITING_AUDIO_GENERATION_ID:
        input_str = update.message.text
        gen_id = await resolve_generation_id(user_identifier, input_str)
        if gen_id is None:
            await update.message.reply_text("🌈 Invalid video ID or nickname. Please try again.")
            return
        context.user_data["audio_generation_id"] = gen_id
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        await update.message.reply_text(
            "🌈 Enter the audio prompt:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        context.user_data["state"] = AWAITING_AUDIO_PROMPT

    elif state == AWAITING_AUDIO_PROMPT:
        prompt = update.message.text
        await add_audio_to_generation(
            context.user_data["audio_generation_id"],
            prompt,
            update,
            context,
            user_identifier
        )
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif state == "awaiting_status_input":
        input_str = update.message.text
        gen_id = await resolve_generation_id(user_identifier, input_str)
        if gen_id is None:
            await update.message.reply_text("🌈 Invalid ID or nickname.")
            return
        client = AsyncLumaAI(auth_token=LUMA_API_KEY)
        generation = await client.generations.get(id=gen_id)
        await update.message.reply_text(f"🌈 Status for {gen_id}: {generation.state}")
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

# Button Handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    user_identifier = get_user_identifier(update)

    if user_identifier != context.user_data.get("initiator_id"):
        await query.edit_message_text("🌈 Only the session starter can interact. Use /dream to begin!")
        return

    state = context.user_data.get("state")

    if state == AWAITING_OPTION:
        if callback_data == "create_menu":
            await show_create_menu(query, context, user_identifier)
        elif callback_data == "edit_menu":
            await show_edit_menu(query, context, user_identifier)
        elif callback_data == "tools_menu":
            await show_tools_menu(query, context, user_identifier)
        elif callback_data == "exit":
            await query.edit_message_text("🌈 Session ended. Use /dream to start again!")
            context.user_data.clear()
            return

        elif callback_data == "settings_menu":
            await show_settings_menu(query, context, user_identifier)
            context.user_data["state"] = AWAITING_SETTINGS

        elif callback_data == "create_words":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Enter your prompt for a Dream video:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_PROMPT
        elif callback_data == "create_pictures":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Upload an image with a caption for a Dream video:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_IMAGE
        elif callback_data == "create_start_frame":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Upload an image with a caption for a Dream video (start frame):",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_START_FRAME_IMAGE
        elif callback_data == "create_loop_frame":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Upload an image with a caption for a looping Dream video:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_LOOP_FRAME_IMAGE
        elif callback_data == "create_end_frame":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Upload an image with a caption for a Dream video (end frame):",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_END_FRAME_IMAGE
        elif callback_data == "create_start_end_frames":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Upload the start image with a caption for a Dream video:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_START_END_FIRST_IMAGE

        elif callback_data == "extend_video":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Enter the video ID to grow (extend forward):",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_EXTEND_VIDEO_ID
        elif callback_data == "reverse_extend_video":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Enter the video ID to rewind (reverse extend):",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_REVERSE_EXTEND_VIDEO_ID
        elif callback_data == "extend_with_end":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Enter the video ID to grow with an end image:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_GROW_VIDEO_ID
        elif callback_data == "rewind_with_start":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Enter the video ID to rewind with a start image:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_REWIND_VIDEO_ID
        elif callback_data == "interpolate_videos":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Enter the first video ID to blend:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_FIRST_BLEND_VIDEO_ID
        elif callback_data == "add_audio":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Enter the video ID to add audio to:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = AWAITING_AUDIO_GENERATION_ID

        elif callback_data == "check_quota":
            await query.message.reply_text(
                f"🌈 Quota: {user_quotas.get(user_identifier, 4)}\nGenerations: {counter_data.get(user_identifier, 0)}"
            )
            await show_main_options(update, context, user_identifier)
        elif callback_data == "view_history":
            conn = sqlite3.connect(DB_FILE)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT generation_id, nickname FROM generation_ids WHERE user_identifier = ?",
                               (user_identifier,))
                results = cursor.fetchall()
                if not results:
                    await query.message.reply_text("🌈 No history found.")
                else:
                    history_text = "🌈 Your History:\n" + "\n".join(
                        f"ID: {gen_id} | Nickname: {nickname or 'None'}" for gen_id, nickname in results)
                    await query.message.reply_text(history_text)
            finally:
                conn.close()
            await show_main_options(update, context, user_identifier)
        elif callback_data == "check_status":
            keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
            await query.message.reply_text(
                "🌈 Enter the generation ID or nickname to check status:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            context.user_data["state"] = "awaiting_status_input"

    elif state == AWAITING_SETTINGS:
        if callback_data == "duration_5s":
            context.user_data["selected_duration"] = "5s"
            await show_settings_menu(query, context, user_identifier)
        elif callback_data == "duration_9s":
            context.user_data["selected_duration"] = "9s"
            await show_settings_menu(query, context, user_identifier)
        elif callback_data == "resolution_720p":
            context.user_data["selected_resolution"] = "720p"
            await show_settings_menu(query, context, user_identifier)
        elif callback_data == "resolution_1080p":
            context.user_data["selected_resolution"] = "1080p"
            await show_settings_menu(query, context, user_identifier)
        elif callback_data == "confirm_settings":
            await show_create_menu(query, context, user_identifier)
            context.user_data["state"] = AWAITING_OPTION
        elif callback_data == "back_to_create":
            await show_create_menu(query, context, user_identifier)
            context.user_data["state"] = AWAITING_OPTION

    elif callback_data == "back_to_main":
        await show_main_options(query, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

    elif callback_data == "cancel":
        await query.edit_message_text("🌈 Action cancelled.")
        await show_main_options(update, context, user_identifier)
        context.user_data["state"] = AWAITING_OPTION

# Main Function
def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("dream", dream_command))
    application.add_handler(CommandHandler("quota", quota_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("add_quota", add_quota_command))

    # Conversation handler for deposits
    boogie_handler = ConversationHandler(
        entry_points=[CommandHandler("letsboogie", lets_boogie_command)],
        states={
            CURRENCY_SELECTION: [CallbackQueryHandler(handle_currency_selection)],
            SELECT_DEPOSIT_AMOUNT: [CallbackQueryHandler(handle_boogie_selection)]
        },
        fallbacks=[]
    )
    application.add_handler(boogie_handler)

    # Conversation handler for saving nicknames
    nickname_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^(Video generated|Dream video ready)"), ask_nickname)],
        states={
            "ASK_NICKNAME": [CallbackQueryHandler(handle_nickname_choice, pattern="^(yes_nickname|no_nickname)$")],
            "SAVE_NICKNAME": [MessageHandler(filters.TEXT & ~filters.COMMAND, save_nickname)]
        },
        fallbacks=[]
    )
    application.add_handler(nickname_handler)

    # Message and callback handlers
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    # Schedule the WebSocket monitoring task
    application.job_queue.run_once(lambda _: asyncio.create_task(monitor_transactions(application)), 0)

    # Start the bot
    application.run_polling()

if __name__ == "__main__":
    main()