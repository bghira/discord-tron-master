from discord.ext import commands
from asyncio import Lock
from typing import List
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.models.schedulers import Schedulers
import logging, traceback

class Scheduler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = AppConfig()

    @commands.command(name="scheduler-list", help="List all schedulers currently approved for use.")
    async def scheduler_list(self, ctx):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)
        app = AppConfig.flask
        with app.app_context():
            all_schedulers = Schedulers.get_all()
        wrapper = "```"
        def build_scheduler_output(scheduler):
            cluster = f"{scheduler.name} (`{scheduler.scheduler}`): {scheduler.description}\n" \
                f"!scheduler {scheduler.name}\n"
            return cluster

        def split_into_chunks(text_lines: List[str], max_length: int = 2000) -> List[str]:
            chunks = []
            current_chunk = ""
            for line in text_lines:
                if len(current_chunk) + len(line) + len(wrapper) + len(wrapper) > max_length:
                    chunks.append(current_chunk)
                    current_chunk = ""
                current_chunk += wrapper + line + wrapper
            if current_chunk:
                chunks.append(current_chunk)
            return chunks

        scheduler_outputs = [build_scheduler_output(t) for t in all_schedulers]
        message_chunks = split_into_chunks(scheduler_outputs)

        if message_chunks:
            for chunk in message_chunks:
                await ctx.send(chunk)
        else:
            message = "Evidently, there are zero registered schedulers available."
            await ctx.send(message)

    @commands.command(name="scheduler-description", help="Are you an image admin? Set a description for your scheduler.")
    async def scheduler_description(self, ctx, name, *, description):
        user_id = ctx.author.id
        user_config = self.config.get_user_config(user_id=user_id)

        user_roles = ctx.author.roles
        message = "Sorry, you can not do that."
        for role in user_roles:
            if role.name == "Image Admin":
                message = "You do not have permission to do that."
                app = AppConfig.flask
                with app.app_context():
                    scheduler = Schedulers.set_description(name, description)
                    if not scheduler:
                        message = "That scheduler does not exist."
                    else:
                        message = "Successfully set the new description for " + str(name)
        await ctx.send(message)

    @commands.command(name="scheduler", help="Set your currently-used scheduler.")
    async def scheduler(self, ctx, name: str = None):
        app = AppConfig.flask

        if not name:
            current_scheduler = self.config.get_user_setting(ctx.author.id, "scheduler", "default")
            if current_scheduler:
                await ctx.send(f"Your current scheduler is: {current_scheduler}")
            else:
                await ctx.send("You do not have a scheduler set.")
            return

        with app.app_context():
            existing = Schedulers.get_by_name(name)
        if not existing:
            await ctx.send("That scheduler is not registered for use. To make your horse cock porn, or whatever, use `!scheduler-add <scheduler> <image|text> <description>` where `image|text` determines whether it's a diffuser or language scheduler.")
            return
        await ctx.send("Your scheduler is now set to: " + str(name))
        self.config.set_user_setting(ctx.author.id, "scheduler", name)

    @commands.command(name="scheduler-delete", help="Delete a scheduler. Not available to non-admins.")
    async def scheduler_delete(self, ctx, name: str):
        # Is the user in the Image Admin role?
        app = AppConfig.flask
        is_admin = await self.is_admin(ctx)
        if not is_admin:
            await ctx.send("sory bae, u must be admuin 😭😭😭 u rek me inside in the worst waysz")
            return
        logging.warning("Deleting scheduler!")
        try:
            with app.app_context():
                schedulers = Schedulers()
                schedulers.delete_by_name(name)
                await ctx.send(f"Sigh. Well, it is done. That scheduler is now obliviated from existence.")
        except Exception as e:
            logging.error(f"Could not delete scheduler: {e}")
            await ctx.send(f"Sorry bae, could not delete that scheduler for you. Have you tried using more lube? {e}")
    @commands.command(name="scheduler-usage", help="Set a scheduler use case list. Not available to non-admins.")
    async def scheduler_usage(self, ctx, name: str, *, usage):
        is_admin = await self.is_admin(ctx)
        if not is_admin:
            await ctx.send("sory bae, u must be admuin 😭😭😭 u rek me inside in the worst waysz")
            return
        app = AppConfig.flask
        with app.app_context():
            existing = Schedulers.get_by_name(name)
            if not existing:
                await ctx.send("That scheduler is not registered.")
                return
        try:
            import json
            new_value = json.loads(usage)
            with app.app_context():
                new_record = Schedulers()
                new_record.set_use_case(name=name, use_case=new_value)
        except Exception as e:
            logging.error(f"Error while attempting to set scheduler use case: {e}, traceback: {traceback.format_exc()}")
            await ctx.send(f"I am very sorry. I was unable to set your new use cases for that scheduler. You might be touching yourself at night too frequently. Have you tried touching yourself less?")
            return
        await ctx.send("That scheduler exists? Cool. It's ready for use.")
    @commands.command(name="scheduler-add", help="Add a scheduler to the list. Not available to non-admins.")
    async def scheduler_add(self, ctx, name: str, internal_name: str, steps_begin: int, steps_end: int, *, description):
        is_admin = await self.is_admin(ctx)
        if not is_admin:
            await ctx.send("sory bae, u must be admuin 😭😭😭 u rek me inside in the worst waysz")
            return
        app = AppConfig.flask
        with app.app_context():
            existing = Schedulers.get_by_name(name)
            if existing:
                await ctx.send("That scheduler is already registered. Go use it to make your horse cock porn, or whatever!")
                return
        if not str(steps_begin).isdigit() or not str(steps_end).isdigit():
            await ctx.send("You have to provide <steps-begin> and <steps-end>, e.g. `!scheduler-add <name> <internal name> <steps begin> <steps end> <description>")
            return
        try:
            with app.app_context():
                new_record = Schedulers()
                use_cases = []
                new_record.create(name=name, scheduler=internal_name, steps_range_begin=steps_begin, steps_range_end=steps_end, use_cases=use_cases, description=description)
        except Exception as e:
            logging.error(f"Error while attempting to create new record: {e}, traceback: {traceback.format_exc()}")
            await ctx.send(f"I am very sorry. I was unable to create a new record for that scheduler. You might be touching yourself at night too frequently. Have you tried touching yourself less?")
            return
        await ctx.send("That scheduler exists? Cool. It's ready for use.")

    async def is_admin(self, ctx):
        # Was the user in the "Image Admin" group?
        user_roles = ctx.author.roles
        for role in user_roles:
            if role.name == "Image Admin":
                return True
        return False