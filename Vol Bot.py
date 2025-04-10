import asyncio
import random
import logging
import sqlite3
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.transactions import Payment, OfferCreate, TrustSet
from xrpl.asyncio.transaction import submit_and_wait as async_submit_and_wait
from xrpl.asyncio.account import get_balance as async_get_balance
from xrpl.utils import xrp_to_drops
from cryptography.fernet import Fernet
import base64

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# XRPL Mainnet client
client = JsonRpcClient("https://s2.ripple.com:51234")  # Mainnet URL

# Telegram bot token (replace with your token in production)
TELEGRAM_TOKEN = "YOUR_TELEGRAM_TOKEN_HERE"

# Database file
DB_FILE = "wallets.db"

# Temporary storage for user data and volume bots
user_data = {}
volume_bots = {}

# Token configuration
TOKEN_CURRENCY = "MYTOKEN"
TOKEN_ISSUER = "rM957DcfnfEQBatTNZXh42WrfBURZPh75h"

# Initialize SQLite database
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS wallets
                 (user_id INTEGER, seed TEXT, address TEXT UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

# Save wallet to database
def save_wallet_to_db(user_id, seed, address):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO wallets (user_id, seed, address) VALUES (?, ?, ?)",
              (user_id, seed, address))
    conn.commit()
    conn.close()
    logger.info(f"Saved wallet for user {user_id}: {address}")

# Retrieve wallets from database
def get_wallets_from_db(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT seed, address FROM wallets WHERE user_id = ?", (user_id,))
    wallets = c.fetchall()
    conn.close()
    return [Wallet(seed=seed, sequence=0) for seed, address in wallets]

# Encrypt seed phrase
def encrypt_seed(seed, passphrase):
    key = base64.urlsafe_b64encode(passphrase.encode().ljust(32, b'\0'))  # Pad/truncate to 32 bytes
    fernet = Fernet(key)
    encrypted_seed = fernet.encrypt(seed.encode())
    return encrypted_seed.decode()

async def start_vol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /vol command and present XRP amount options."""
    logger.info("Processing /vol command...")
    keyboard = [
        [InlineKeyboardButton("10 XRP", callback_data="10")],
        [InlineKeyboardButton("20 XRP", callback_data="20")],
        [InlineKeyboardButton("500 XRP", callback_data="500")],
        [InlineKeyboardButton("1000 XRP", callback_data="1000")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select the amount of XRP to use:", reply_markup=reply_markup)
    logger.info("Provided amount selection options.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user's amount selection and proceed with wallet creation."""
    query = update.callback_query
    await query.answer()

    amount = int(query.data)
    user_id = query.from_user.id
    user_data[user_id] = {"amount": amount}
    logger.info(f"User {user_id} selected {amount} XRP.")

    try:
        wallet = Wallet.create()
        user_data[user_id]["wallet"] = wallet
        save_wallet_to_db(user_id, wallet.seed, wallet.classic_address)
        logger.info(f"Wallet created for user {user_id}: {wallet.classic_address}")
        await query.edit_message_text(
            f"Generated wallet address: `{wallet.classic_address}`\n"
            f"Please deposit {amount} XRP to this address.\n"
            f"Reply with 'check' to verify the balance, or 'cancel' to stop.\n"
            f"Next, reply with a passphrase to encrypt your seed phrase.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_passphrase"] = True
    except Exception as e:
        logger.error(f"Failed to create wallet for user {user_id}: {str(e)}")
        await query.edit_message_text("Failed to create wallet. Please try again later.")

async def handle_passphrase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle passphrase input and send encrypted seed."""
    user_id = update.message.from_user.id
    if context.user_data.get("awaiting_passphrase") and user_id in user_data:
        passphrase = update.message.text
        wallet = user_data[user_id]["wallet"]
        encrypted_seed = encrypt_seed(wallet.seed, passphrase)
        await update.message.reply_text(
            f"Your encrypted seed phrase:\n`{encrypted_seed}`\n"
            f"Save this securely. To decrypt, use the passphrase you provided with a Fernet-compatible tool "
            f"(e.g., Python's cryptography library). Reply 'check' or 'cancel' next.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_passphrase"] = False
        logger.info(f"Sent encrypted seed to user {user_id}.")
    # Pass to check_deposit if 'check' or 'cancel'
    if update.message.text.lower() in ["check", "cancel"]:
        await check_deposit(update, context)

async def setup_trust_line(wallet, chat_id, context):
    """Set up a trust line to the token issuer."""
    logger.info(f"Setting up trust line for {wallet.classic_address}.")
    trust_set = TrustSet(
        account=wallet.classic_address,
        limit_amount={"currency": TOKEN_CURRENCY, "issuer": TOKEN_ISSUER, "value": "1000000"}
    )
    try:
        await async_submit_and_wait(trust_set, client, wallet)
        logger.info(f"Trust line set for {wallet.classic_address}.")
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"Trust line set for {wallet.classic_address} to {TOKEN_CURRENCY}/{TOKEN_ISSUER}")
    except Exception as e:
        logger.error(f"Failed to set trust line for {wallet.classic_address}: {str(e)}")
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"Failed to set trust line: {str(e)}")

async def distribute_mytoken(main_wallet, new_wallets, chat_id, context):
    """Distribute MYTOKEN to new wallets from the main wallet."""
    for new_wallet in new_wallets:
        payment_tx = Payment(
            account=main_wallet.classic_address,
            amount={"currency": TOKEN_CURRENCY, "issuer": TOKEN_ISSUER, "value": "1000"},
            destination=new_wallet.classic_address
        )
        try:
            await async_submit_and_wait(payment_tx, client, main_wallet)
            logger.info(f"Distributed 1000 MYTOKEN to {new_wallet.classic_address}.")
        except Exception as e:
            logger.error(f"Failed to distribute MYTOKEN to {new_wallet.classic_address}: {str(e)}")
            await context.bot.send_message(chat_id=chat_id,
                                           text=f"Failed to distribute MYTOKEN: {str(e)}")

async def check_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify deposit, distribute funds, and start volume bot."""
    user_id = update.message.from_user.id
    message_text = update.message.text.lower()
    logger.info(f"Processing '{message_text}' for user {user_id}...")

    if message_text == "cancel":
        if user_id in user_data:
            del user_data[user_id]
            await update.message.reply_text("Process canceled. Start over with /vol.")
            logger.info(f"User {user_id} canceled.")
        else:
            await update.message.reply_text("Nothing to cancel. Use /vol to start.")
        return

    if message_text != "check":
        return  # Ignore unless handled by passphrase handler

    if user_id not in user_data:
        logger.info(f"No data for {user_id}. Creating default session.")
        default_amount = 10
        try:
            wallet = Wallet.create()
            user_data[user_id] = {"amount": default_amount, "wallet": wallet}
            save_wallet_to_db(user_id, wallet.seed, wallet.classic_address)
            await update.message.reply_text(
                f"No session found. Created new wallet!\n"
                f"Address: `{wallet.classic_address}`\n"
                f"Deposit {default_amount} XRP, then reply 'check' or 'cancel'.\n"
                f"Next, reply with a passphrase to encrypt your seed.",
                parse_mode="Markdown"
            )
            context.user_data["awaiting_passphrase"] = True
            return
        except Exception as e:
            logger.error(f"Failed to create wallet: {str(e)}")
            await update.message.reply_text("Failed to create wallet. Try again.")
            return

    amount = user_data[user_id]["amount"]
    wallet = user_data[user_id]["wallet"]
    expected_drops = int(xrp_to_drops(amount))

    try:
        balance = await async_get_balance(wallet.classic_address, client)
        logger.info(f"Balance for {wallet.classic_address}: {balance} drops.")
    except Exception as e:
        logger.error(f"Balance check failed: {str(e)}")
        await update.message.reply_text(f"Error checking balance: {str(e)}. Retry with 'check'.")
        return

    if balance < expected_drops:
        await update.message.reply_text(
            f"Insufficient balance. Expected {amount} XRP ({expected_drops} drops), got {balance} drops.\n"
            f"Retry with 'check' or 'cancel'."
        )
        return

    # Check for existing wallets
    existing_wallets = get_wallets_from_db(user_id)
    if len(existing_wallets) >= 5:
        new_wallets = existing_wallets[:5]
        logger.info(f"Reusing {len(new_wallets)} wallets for user {user_id}.")
    else:
        new_wallets = [Wallet.create() for _ in range(5 - len(existing_wallets))]
        for w in new_wallets:
            save_wallet_to_db(user_id, w.seed, w.classic_address)
        new_wallets = existing_wallets + new_wallets
        logger.info(f"Created {5 - len(existing_wallets)} new wallets.")

    # Distribute XRP
    amount_per_wallet = amount / 5.0
    drops_per_wallet = xrp_to_drops(amount_per_wallet)
    for new_wallet in new_wallets:
        payment_tx = Payment(
            account=wallet.classic_address,
            amount=drops_per_wallet,
            destination=new_wallet.classic_address
        )
        try:
            await async_submit_and_wait(payment_tx, client, wallet)
            logger.info(f"Sent {amount_per_wallet} XRP to {new_wallet.classic_address}.")
        except Exception as e:
            logger.error(f"XRP distribution failed: {str(e)}")
            await update.message.reply_text(f"Failed to distribute XRP: {str(e)}")
            return

    # Distribute MYTOKEN
    await distribute_mytoken(wallet, new_wallets, update.message.chat_id, context)

    # Set up trust lines
    for new_wallet in new_wallets:
        await setup_trust_line(new_wallet, update.message.chat_id, context)

    wallet_list = "\n".join([f"- {w.classic_address}" for w in new_wallets])
    await update.message.reply_text(
        f"Deposit of {amount} XRP verified!\n"
        f"Using 5 wallets:\n{wallet_list}\n"
        f"Starting volume bot with {TOKEN_CURRENCY}/{TOKEN_ISSUER}...",
        parse_mode="Markdown"
    )

    volume_bots[user_id] = new_wallets
    asyncio.create_task(run_volume_bot(user_id, new_wallets, update.message.chat_id, context))
    del user_data[user_id]

async def stop_vol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the volume bot."""
    user_id = update.message.from_user.id
    if user_id in volume_bots:
        del volume_bots[user_id]
        await update.message.reply_text("Volume bot stopped.")
        logger.info(f"Volume bot stopped for user {user_id}.")
    else:
        await update.message.reply_text("No active volume bot.")
        logger.warning(f"No volume bot found for {user_id}.")

async def run_volume_bot(user_id, wallets, chat_id, context):
    """Run the volume bot with trading cycles."""
    logger.info(f"Starting volume bot for user {user_id}.")
    while user_id in volume_bots:
        try:
            for i in range(len(wallets)):
                wallet = wallets[i]
                next_wallet = wallets[(i + 1) % len(wallets)]
                trade_amount_xrp = random.uniform(1, 5)
                trade_amount_drops = xrp_to_drops(trade_amount_xrp)

                # Sell XRP for MYTOKEN
                offer_tx = OfferCreate(
                    account=wallet.classic_address,
                    taker_gets=trade_amount_drops,
                    taker_pays={"currency": TOKEN_CURRENCY, "issuer": TOKEN_ISSUER, "value": str(trade_amount_xrp * 100)}
                )
                await async_submit_and_wait(offer_tx, client, wallet)
                logger.info(f"{wallet.classic_address} offers {trade_amount_xrp} XRP for MYTOKEN.")

                # Buy XRP with MYTOKEN
                counter_offer_tx = OfferCreate(
                    account=next_wallet.classic_address,
                    taker_gets={"currency": TOKEN_CURRENCY, "issuer": TOKEN_ISSUER, "value": str(trade_amount_xrp * 100)},
                    taker_pays=trade_amount_drops
                )
                await async_submit_and_wait(counter_offer_tx, client, next_wallet)
                logger.info(f"{next_wallet.classic_address} offers MYTOKEN for {trade_amount_xrp} XRP.")

            await context.bot.send_message(chat_id=chat_id, text=f"Volume bot cycle completed for user {user_id}.")
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Volume bot error: {str(e)}")
            await context.bot.send_message(chat_id=chat_id, text=f"Volume bot error: {str(e)}. Restarting in 60s.")
            await asyncio.sleep(60)

def main():
    """Start the Telegram bot."""
    init_db()
    logger.info("Starting bot...")
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("vol", start_vol))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_passphrase))
    # Use precompiled regex with re.IGNORECASE
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^(check|cancel)$', re.IGNORECASE)), check_deposit))
    application.add_handler(CommandHandler("stop", stop_vol))

    logger.info("Bot running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()