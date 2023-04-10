from discord.ext import commands
import logging

class CustomHelp(commands.HelpCommand):
    async def send_bot_help(self, mapping):
        logging.debug("CustomHelp.send_bot_help() called.")

        ctx = self.context
        specific_role = "Image Admin"  # Replace with the name of the specific role

        # Check if the user has the specific role
        user_has_role = any(role.name == specific_role for role in ctx.author.roles)
        logging.info(f"User {ctx.author.name} ({ctx.author.id}) has role {specific_role}: {user_has_role}.")
        # Filter commands based on whether they are hidden and the user's role
        if not user_has_role:
            filtered_mapping = {cog: cmds for cog, cmds in mapping.items() if not any(cmd.hidden for cmd in cmds)}
        else:
            filtered_mapping = mapping
        logging.debug(f"Filtered mapping: {filtered_mapping}")
        await super().send_bot_help(filtered_mapping)