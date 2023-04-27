async def send_large_messages(ctx, text, max_chars=2000, delete_delay=None):
    ctx = fix_onmessage_context(ctx)
    if len(text) <= max_chars:
        if hasattr(ctx, "channel"):
            response = await ctx.channel.send(text)
        elif hasattr(ctx, "send"):
            response = await ctx.send(text)
        if delete_delay is not None:
            await response.delete(delay=delete_delay)
        return

    lines = text.split("\n")
    buffer = ""
    for line in lines:
        if len(buffer) + len(line) + 1 > max_chars:
            if hasattr(ctx, "channel"):
                response = await ctx.channel.send(buffer)
            elif hasattr(ctx, "send"):
                response = await ctx.send(buffer)

            if delete_delay is not None:
                await response.delete(delay=delete_delay)
            buffer = ""
        buffer += line + "\n"
    if buffer:
        if hasattr(ctx, "channel"):
            response = await ctx.channel.send(buffer)
        elif hasattr(ctx, "send"):
            response = await ctx.send(buffer)
        if delete_delay is not None:
            await response.delete(delay=delete_delay)

async def fix_onmessage_context(ctx, bot = None):
    context = ctx
    if not hasattr(ctx, "send") and bot is None:
        raise RuntimeError("Cannot fix context without access to discord bot instance. You must import DiscordBot and use get_instance().")
    elif not hasattr(ctx, "send") and bot is not None:
        # Likely this came from on_message. Get the context properly.
        context = await bot.get_context(ctx)
    return context