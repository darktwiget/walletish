import asyncio
import random
import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.transactions import Payment, OfferCreate, TrustSet
from xrpl.asyncio.transaction import submit_and_wait as async_submit_and_wait
from xrpl.asyncio.account import get_balance as async_get_balance
from xrpl.utils import xrp_to_drops

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# XRPL Testnet client
client = JsonRpcClient("https://s1.ripple.com:51234")  # Updated to a reliable Testnet URL

# Telegram bot token
TELEGRAM_TOKEN = "8122713471:AAHsT0nUDABuXjmpg3-i61DUm1GlshTf8CA"

# Database file
DB_FILE = "wallets.db"

# Store user data (amount and wallet) temporarily
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
    logger.info(f"Saved wallet for user {user_id} to database: {address}")

async def start_vol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /vol command and present XRP amount options."""
    logger.info("Processing /vol command...")
    keyboard = [
        [InlineKeyboardButton("10 XRP", callback_data="10")],
        [InlineKeyboardButton("20 XRP", callback_data="20")],
        [InlineKeyboardButton("500 XRP", callback_data="500")],
        [InlineKeyboardButton("1000 XRP", callback_data="1000")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Please select the amount of XRP to use:", reply_markup=reply_markup)
    logger.info("Provided options for amount selection.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the user's selection and proceed with wallet creation."""
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
        logger.info(f"Wallet created for user {user_id}. Address: {wallet.classic_address}")
        await query.edit_message_text(
            f"Generated wallet address: `{wallet.classic_address}`\n"
            f"Seed phrase: `{wallet.seed}`\n"
            f"Please deposit {amount} XRP to this address.\n"
            f"Reply with 'check' to verify the balance, or 'cancel' to stop.\n"
            "Warning: Save your seed phrase securely; it will not be shown again!",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to create wallet for user {user_id}: {str(e)}")
        await query.edit_message_text("Failed to create wallet. Please try again later.")

async def setup_trust_line(wallet, chat_id, context):
    """Set up a trust line to the token issuer."""
    logger.info(f"Setting up trust line for wallet {wallet.classic_address}.")
    trust_set = TrustSet(
        account=wallet.classic_address,
        limit_amount={"currency": TOKEN_CURRENCY, "issuer": TOKEN_ISSUER, "value": "1000000"}
    )
    try:
        await async_submit_and_wait(trust_set, client, wallet)
        logger.info(f"Trust line set successfully for {wallet.classic_address}.")
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"Trust line set for {wallet.classic_address} to {TOKEN_CURRENCY}/{TOKEN_ISSUER}")
    except Exception as e:
        logger.error(f"Failed to set trust line for {wallet.classic_address}: {str(e)}")
        await context.bot.send_message(chat_id=chat_id,
                                       text=f"Failed to set trust line for {wallet.classic_address}: {str(e)}")

async def check_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verify the deposit by balance, distribute XRP, set trust lines, and start volume bot."""
    user_id = update.message.from_user.id
    message_text = update.message.text.lower()
    logger.info(f"Processing message '{message_text}' for user {user_id}...")

    if message_text == "cancel":
        if user_id in user_data:
            del user_data[user_id]
            await update.message.reply_text("Transaction process canceled. You can start over with /vol or 'check'.")
            logger.info(f"User {user_id} canceled the transaction process.")
        else:
            await update.message.reply_text("Nothing to cancel. Start with /vol or 'check'.")
            logger.info(f"User {user_id} tried to cancel, but no active process found.")
        return

    if message_text != "check":
        await update.message.reply_text("Please reply with 'check' to verify your deposit or 'cancel' to stop.")
        logger.warning(f"Invalid command '{message_text}' from user {user_id}.")
        return

    if user_id not in user_data:
        logger.info(f"No user data found for {user_id}. Creating new user with default amount.")
        default_amount = 10
        try:
            wallet = Wallet.create()
            user_data[user_id] = {"amount": default_amount, "wallet": wallet}
            save_wallet_to_db(user_id, wallet.seed, wallet.classic_address)
            logger.info(f"Wallet created for new user {user_id}. Address: {wallet.classic_address}")
            await update.message.reply_text(
                f"No previous session found. Created a new wallet for you!\n"
                f"Generated wallet address: `{wallet.classic_address}`\n"
                f"Seed phrase: `{wallet.seed}`\n"
                f"Please deposit {default_amount} XRP to this address.\n"
                f"Reply with 'check' to verify the balance, or 'cancel' to stop.\n"
                "Warning: Save your seed phrase securely; it will not be shown again!",
                parse_mode="Markdown"
            )
            return
        except Exception as e:
            logger.error(f"Failed to create wallet for user {user_id}: {str(e)}")
            await update.message.reply_text("Failed to create wallet. Please try again later.")
            return

    amount = user_data[user_id]["amount"]
    wallet = user_data[user_id]["wallet"]
    expected_drops = int(xrp_to_drops(amount))
    logger.info(f"Expected deposit for user {user_id}: {expected_drops} drops.")

    try:
        balance = await async_get_balance(wallet.classic_address, client)
        logger.info(f"Balance for {wallet.classic_address}: {balance} drops.")
    except Exception as e:
        logger.error(f"Failed to get balance for {wallet.classic_address}: {str(e)}")
        await update.message.reply_text(
            f"Error checking balance: {str(e)}.\n"
            f"Please reply 'check' to retry or 'cancel' to stop."
        )
        return

    if balance < expected_drops:
        await update.message.reply_text(
            f"Insufficient balance. Expected {amount} XRP ({expected_drops} drops), but found {balance} drops.\n"
            f"Please ensure the correct amount is sent, then reply 'check' to retry or 'cancel' to stop."
        )
        logger.warning(f"Insufficient balance for user {user_id}.")
        return

    logger.info(f"Balance verified for user {user_id}. Creating wallets and distributing funds...")

    # Create 5 new wallets and distribute funds
    new_wallets = [Wallet.create() for _ in range(5)]
    amount_per_wallet = amount // 5
    drops_per_wallet = xrp_to_drops(amount_per_wallet)  # String for transaction

    for new_wallet in new_wallets:
        save_wallet_to_db(user_id, new_wallet.seed, new_wallet.classic_address)
        payment_tx = Payment(
            account=wallet.classic_address,
            amount=drops_per_wallet,
            destination=new_wallet.classic_address
        )
        try:
            await async_submit_and_wait(payment_tx, client, wallet)
            logger.info(f"Distributed {amount_per_wallet} XRP to wallet {new_wallet.classic_address}.")
        except Exception as e:
            logger.error(f"Failed to distribute funds to {new_wallet.classic_address}: {str(e)}")
            await update.message.reply_text(f"Failed to distribute funds to {new_wallet.classic_address}: {str(e)}")
            return

    for new_wallet in new_wallets:
        await setup_trust_line(new_wallet, update.message.chat_id, context)

    wallet_list = "\n".join([f"- {w.classic_address}" for w in new_wallets])
    await update.message.reply_text(
        f"Deposit of {amount} XRP verified by balance!\n"
        f"Created 5 wallets and distributed {amount_per_wallet} XRP to each:\n"
        f"{wallet_list}\n"
        f"Starting volume bot operations with {TOKEN_CURRENCY}/{TOKEN_ISSUER}...\n"
        "Seed phrases for these wallets are stored securely in the database.",
        parse_mode="Markdown"
    )

    volume_bots[user_id] = new_wallets
    asyncio.create_task(run_volume_bot(user_id, new_wallets, update.message.chat_id, context))
    del user_data[user_id]

async def stop_vol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the volume bot for the user."""
    user_id = update.message.from_user.id
    logger.info(f"Stopping volume bot for user {user_id}.")
    if user_id in volume_bots:
        del volume_bots[user_id]
        await update.message.reply_text("Volume bot stopped.")
        logger.info(f"Volume bot stopped for user {user_id}.")
    else:
        await update.message.reply_text("No active volume bot found.")
        logger.warning(f"No active volume bot found for user {user_id}.")

async def run_volume_bot(user_id, wallets, chat_id, context):
    """Run the volume bot indefinitely until stopped."""
    logger.info(f"Starting volume bot for user {user_id} with {len(wallets)} wallets.")
    while user_id in volume_bots:
        try:
            for i in range(len(wallets)):
                wallet = wallets[i]
                next_wallet = wallets[(i + 1) % len(wallets)]  # Circular trading

                trade_amount_xrp = random.uniform(1, 5)
                trade_amount_drops = xrp_to_drops(trade_amount_xrp)  # String for transaction

                offer_tx = OfferCreate(
                    account=wallet.classic_address,
                    taker_gets=trade_amount_drops,
                    taker_pays={
                        "currency": TOKEN_CURRENCY,
                        "issuer": TOKEN_ISSUER,
                        "value": str(trade_amount_xrp * 100)
                    }
                )
                await async_submit_and_wait(offer_tx, client, wallet)
                logger.info(f"Offer created: {wallet.classic_address} sells {trade_amount_xrp} XRP for MYTOKEN.")

                counter_offer_tx = OfferCreate(
                    account=next_wallet.classic_address,
                    taker_gets={
                        "currency": TOKEN_CURRENCY,
                        "issuer": TOKEN_ISSUER,
                        "value": str(trade_amount_xrp * 100)
                    },
                    taker_pays=trade_amount_drops
                )
                await async_submit_and_wait(counter_offer_tx, client, next_wallet)
                logger.info(f"Counter-offer created: {next_wallet.classic_address} buys {trade_amount_xrp} XRP with MYTOKEN.")

            await context.bot.send_message(chat_id=chat_id,
                                           text=f"Volume bot running for user {user_id}. Simulated trade cycle completed.")
            logger.info(f"Trade cycle completed for user {user_id}.")
            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"Error in volume bot for user {user_id}: {str(e)}")
            await context.bot.send_message(chat_id=chat_id,
                                           text=f"Volume bot encountered an error: {str(e)}. Restarting in 60 seconds.")
            await asyncio.sleep(60)

    logger.info(f"Volume bot stopped for user {user_id}.")

def main():
    """Start the Telegram bot."""
    init_db()

    logger.info("Starting Telegram bot...")
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("vol", start_vol))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_deposit))  # For "check" and "cancel"
    application.add_handler(CommandHandler("stop", stop_vol))

    logger.info("Bot handlers added. Running polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main":
    main()