import asyncio
import logging
import traceback

import discord

SEND_RETRY_COUNT = 3
SEND_RETRY_WAIT_SECONDS = 1


def _is_retryable_send_error(exc: Exception) -> bool:
    if isinstance(exc, discord.DiscordServerError):
        return True
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", None)
        return status in {502, 503, 504}
    return False


async def _send_once(ctx, text):
    if hasattr(ctx, "channel") and getattr(ctx, "channel", None) is not None:
        return await ctx.channel.send(text)
    if hasattr(ctx, "send"):
        return await ctx.send(text)
    raise RuntimeError("Context has no send target")


async def _send_with_retry(
    ctx,
    text,
    retries: int = SEND_RETRY_COUNT,
    wait_seconds: int = SEND_RETRY_WAIT_SECONDS,
):
    attempt = 0
    while True:
        try:
            return await _send_once(ctx, text)
        except Exception as exc:
            if not _is_retryable_send_error(exc) or attempt >= retries:
                raise
            attempt += 1
            logging.warning(
                "Retrying Discord send after transient error (attempt %s/%s): %s",
                attempt,
                retries,
                exc,
            )
            await asyncio.sleep(max(0, int(wait_seconds)))


async def send_large_messages(ctx, text, max_chars=2000, delete_delay=None):
    ctx = await fix_onmessage_context(ctx)
    if len(text) <= max_chars:
        response = await _send_with_retry(ctx, text)
        if delete_delay is not None:
            await response.delete(delay=delete_delay)
        return response

    lines = text.split("\n")
    buffer = ""
    last_response = None
    for line in lines:
        if len(buffer) + len(line) + 1 > max_chars:
            if buffer.strip():
                last_response = await _send_with_retry(ctx, buffer.rstrip())
                if delete_delay is not None and last_response:
                    await last_response.delete(delay=delete_delay)
            buffer = ""
        if len(line) > max_chars:
            for i in range(0, len(line), max_chars):
                chunk = line[i : i + max_chars]
                last_response = await _send_with_retry(ctx, chunk)
                if delete_delay is not None and last_response:
                    await last_response.delete(delay=delete_delay)
        else:
            buffer += line + "\n"
    if buffer.strip():
        last_response = await _send_with_retry(ctx, buffer.rstrip())
        if delete_delay is not None and last_response:
            await last_response.delete(delay=delete_delay)
    return last_response


async def fix_onmessage_context(ctx, bot=None):
    context = ctx
    if hasattr(ctx, "channel"):
        logging.debug(f"Context already has channel attribute.")
        return context
    logging.debug(
        f"Running fix_onmessage_context with\nContext: {ctx}\nBot: {bot}, Traceback: {traceback.format_stack()}"
    )
    if not hasattr(ctx, "send") and bot is None:
        error = "Cannot fix context without access to discord bot instance. You must import DiscordBot and use get_instance()."
        logging.error(error)
        raise RuntimeError(error)
    elif not hasattr(ctx, "send") and bot is not None:
        # Likely this came from on_message. Get the context properly.
        logging.debug(f"Running get_context on bot object.")
        context = await bot.get_context(ctx)
    else:
        logging.debug(f"Passing through context object.")
    return context


async def most_recently_active_thread(channel):
    threads = channel.threads
    if threads:
        if len(threads) > 1:
            # Sort by last message id so that we grab the most recent thread.
            threads.sort(key=lambda x: x.last_message_id, reverse=True)
        return threads[0]
    return None
