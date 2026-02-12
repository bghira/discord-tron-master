import logging, traceback


async def send_large_messages(ctx, text, max_chars=2000, delete_delay=None):
    ctx = await fix_onmessage_context(ctx)
    if len(text) <= max_chars:
        if hasattr(ctx, "channel"):
            response = await ctx.channel.send(text)
        elif hasattr(ctx, "send"):
            response = await ctx.send(text)
        if delete_delay is not None:
            await response.delete(delay=delete_delay)
        return response

    lines = text.split("\n")
    buffer = ""
    last_response = None
    for line in lines:
        if len(buffer) + len(line) + 1 > max_chars:
            if buffer.strip():
                if hasattr(ctx, "channel"):
                    last_response = await ctx.channel.send(buffer.rstrip())
                elif hasattr(ctx, "send"):
                    last_response = await ctx.send(buffer.rstrip())
                if delete_delay is not None and last_response:
                    await last_response.delete(delay=delete_delay)
            buffer = ""
        if len(line) > max_chars:
            for i in range(0, len(line), max_chars):
                chunk = line[i : i + max_chars]
                if hasattr(ctx, "channel"):
                    last_response = await ctx.channel.send(chunk)
                elif hasattr(ctx, "send"):
                    last_response = await ctx.send(chunk)
                if delete_delay is not None and last_response:
                    await last_response.delete(delay=delete_delay)
        else:
            buffer += line + "\n"
    if buffer.strip():
        if hasattr(ctx, "channel"):
            last_response = await ctx.channel.send(buffer.rstrip())
        elif hasattr(ctx, "send"):
            last_response = await ctx.send(buffer.rstrip())
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
