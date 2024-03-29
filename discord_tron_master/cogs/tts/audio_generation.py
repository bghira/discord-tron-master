import logging, traceback
from discord.ext import commands
from discord_tron_master.classes.app_config import AppConfig
from discord_tron_master.bot import DiscordBot
from discord_tron_master.classes.jobs.bark_tts_job import BarkTtsJob
from discord_tron_master.bot import clean_traceback
# For queue manager, etc.
config = AppConfig()
discord = DiscordBot.get_instance()
logging.debug(f"Loading StableLM predict helper")
# Commands used for Stable Diffusion image gen.
class Audio_generation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config = AppConfig()
    VOICES = {
        # Checked and filtered:
        "fr_speaker_1": "fr_speaker_1", "fr_speaker_3": "fr_speaker_3", "fr_speaker_4": "fr_speaker_4",
        "hi_speaker_1": "hi_speaker_1", "hi_speaker_5": "hi_speaker_5", "hi_speaker_8": "hi_speaker_8", "hi_speaker_9": "hi_speaker_9",
        "announcer": "announcer",
        "cartoon_extreme": "cartoon_extreme",
        "de_speaker_0": "de_speaker_0","de_speaker_1": "de_speaker_1","de_speaker_2": "de_speaker_2","de_speaker_5": "de_speaker_5","de_speaker_7": "de_speaker_7","de_speaker_9": "de_speaker_9",
        "en_speaker_0": "en_speaker_0",
        "en_speaker_3": "en_speaker_3", "en_speaker_4": "en_speaker_4", "en_speaker_5": "en_speaker_5", "en_speaker_6": "en_speaker_6", "en_speaker_7": "en_speaker_7", "en_speaker_8": "en_speaker_8", "en_speaker_9": "en_speaker_9", "en_british": "en_british","en_deadpan": "en_deadpan","en_female_intense": "en_female_intense",
        "en_german_professor": "en_german_professor","en_interesting_tone": "en_interesting_tone","en_male_nervous_subdued": "en_male_nervous_subdued","en_male_professional_reader": "en_male_professional_reader","en_man_giving_ted_talk": "en_man_giving_ted_talk","en_narrator_deep": "en_narrator_deep","en_narrator_light_bg": "en_narrator_light_bg","en_old_movie_actor": "en_old_movie_actor","en_public_speaker_2": "en_public_speaker_2","en_public_speaker": "en_public_speaker","en_quiet_intense": "en_quiet_intense","en_smooth_gruff": "en_smooth_gruff","en_solo_singer": "en_solo_singer",  "en_tv_commercial": "en_tv_commercial",
        "en_female_professional_reader": "en_female_professional_reader","en_female_storyteller": "en_female_storyteller",
        "es_speaker_0": "es_speaker_0", "es_speaker_3": "es_speaker_3", "es_speaker_4": "es_speaker_4", "es_speaker_5": "es_speaker_5", "es_speaker_6": "es_speaker_6", "es_speaker_7": "es_speaker_7", "es_speaker_8": "es_speaker_8", "es_speaker_9": "es_speaker_9",
        "zh_speaker_0": "zh_speaker_0","zh_speaker_1": "zh_speaker_1","zh_speaker_3": "zh_speaker_3","zh_speaker_4": "zh_speaker_4","zh_speaker_5": "zh_speaker_5","zh_speaker_6": "zh_speaker_6",
        "it_speaker_2": "it_speaker_2","it_speaker_5": "it_speaker_5","it_speaker_6": "it_speaker_6","it_speaker_7": "it_speaker_7","it_speaker_8": "it_speaker_8","it_speaker_9": "it_speaker_9",
        "ja_speaker_0": "ja_speaker_0", "ja_speaker_1": "ja_speaker_1", "ja_speaker_3": "ja_speaker_3", "ja_speaker_4": "ja_speaker_4",
        "ko_speaker_1": "ko_speaker_1", "ko_speaker_3": "ko_speaker_3", "ko_speaker_7": "ko_speaker_7", "ko_speaker_8": "ko_speaker_8", "ko_speaker_9": "ko_speaker_9",
        "pl_speaker_3": "pl_speaker_3", "pl_speaker_4": "pl_speaker_4", "pl_speaker_7": "pl_speaker_7",
        "pt_speaker_3": "pt_speaker_3", "pt_speaker_4": "pt_speaker_4", "pt_speaker_5": "pt_speaker_5", "pt_speaker_6": "pt_speaker_6",
        "ru_speaker_2": "ru_speaker_2", "ru_speaker_3": "ru_speaker_3", "ru_speaker_7": "ru_speaker_7",
        "kpop_acoustic": "kpop_acoustic",
        "music_off_the_rails": "music_off_the_rails",
        # Weird, needs more investigation:
        "cool_duo": "cool_duo", "en_guitar": "en_guitar",
        "classic_robot_tts": "classic_robot_tts",
        # Unchecked:
        "rock_maybe": "rock_maybe",
        "sing1": "sing1", "sing2": "sing2", "sing_3": "sing_3",
        "snarky_but_noisy": "snarky_but_noisy", "snarky_narrator_but_noisy": "snarky_narrator_but_noisy",
        "speaker_2": "speaker_2", "speaker_3": "speaker_3",
        "talkradio": "talkradio",
        "timid_jane": "timid_jane",
        "weirdvibes2": "weirdvibes2",
        "weirdvibes": "weirdvibes",
    }
    @commands.command(name="t", help="An alias for `!tts`")
    async def t(self, ctx, *, prompt):
        self.tts(ctx, prompt=prompt)

    @commands.command(name="tts", help="Generates an audio file based on the given prompt.")
    async def tts(self, ctx, *, prompt):
        try:
            # Generate a "Job" object that will be put into the queue.
            discord_first_message = await DiscordBot.send_large_message(ctx=ctx, text="A worker has been selected for your query: `" + prompt + "`")

            self.config.reload_config()

            job = BarkTtsJob(ctx.author.id, (self.bot, self.config, ctx, prompt, discord_first_message))
            # Get the worker that will process the job.
            worker = discord.worker_manager.find_best_fit_worker(job)
            if worker is None:
                await discord_first_message.edit(content="No workers available. TTS request was **not** added to queue. 😭 aw, how sad. 😭")
                # Wait a few seconds before deleting:
                await discord_first_message.delete(delay=10)
                return
            logging.info("Worker selected for job: " + str(worker.worker_id))
            # Add it to the queue
            await discord.queue_manager.enqueue_job(worker, job)
        except Exception as e:
            await ctx.send(
                f"Error generating image: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
            )

    @commands.command(name="tts-voice", help="Set a voice for your whole TTS experience.")
    async def tts_voice_set(self, ctx, *, voice):
        try:
            self.config.reload_config()
            self.config.set_user_setting(ctx.author.id, "tts_voice", voice)
        except Exception as e:
            await ctx.send(
                f"Error setting TTS voice: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
            )
    @commands.command(name="actors", help="Configure specific voices for characters, allowing you to use {CHARACTER_NAME_HERE} as a string .")
    async def tts_actors(self, ctx, actor = None, voice = None):
        try:
            self.config.reload_config()
            user_config = self.config.get_user_config(ctx.author.id)
            current_actors = user_config.get("tts_actors", {})
            if len(current_actors) > 0:
                current_actor_text = f"{len(current_actors)} voice actor(s) configured:\n{self.list_actors(current_actors)}"
            else:
                current_actor_text = f"Zero voice actors are configured."
            if actor is None:
                sent_message = await ctx.send(
                    f"{ctx.author.mention} Since no actor name was provided, here are your current actor settings:\n{current_actor_text}"
                )
                await sent_message.delete(delay=15)
                return
            if voice is None:
                if actor not in current_actors:
                    message_text = f"The actor '{actor}' is not currently defined for your profile. You must use `{config.get_command_prefix()}tts-voice {actor} <voice>` to define a voice for this actor."
                else:
                    message_text = f"The actor '{actor}' is currently configured to use `{current_actors[actor]}` for its voice."
                sent_message = await ctx.send(message_text)
                await sent_message.delete(delay=15)
                return
            current_actor = current_actors.get(actor, None)
            if current_actor is None:
                # Set the message up so that no actor becomes the default voice.
                current_actor = {"voice": "default"}
            old_voice = current_actor.get("voice", "default")
            message_text = f"The actor '{actor} was set to use the `{old_voice}` voice, and will now use `{voice}`"
            sent_message = await ctx.send(message_text)
            await sent_message.delete(delay=15)
            current_actor["voice"] = voice
            current_actors[actor] = current_actor
            self.config.set_user_setting(ctx.author.id, "tts_actors", current_actors)

        except Exception as e:
            await ctx.send(
                f"Error setting TTS voice: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
            )
        
        finally:
            ctx.delete()

    @commands.command(name="tts-voices", help="List the available TTS voices.")
    async def tts_voice_list(self, ctx):
            try:
                available_languages = self.list_available_languages()
                for chunk in available_languages:
                    await discord.send_large_message(ctx=ctx, text=chunk)
            except Exception as e:
                await ctx.send(
                    f"Error listing voices: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
                )
    def list_available_languages(self, languages=None):
        if languages is None:
            languages = Audio_generation.VOICES
        min_entities = 4
        max_entities = 12
        grouped_languages = {}
        misc_group = []

        for lang in languages:
            prefix = lang.split("_")[0]
            if prefix not in grouped_languages:
                grouped_languages[prefix] = []
            else:
                if len(grouped_languages[prefix]) > max_entities:
                    prefix = f"more_{prefix}"
                    if prefix not in grouped_languages:
                        grouped_languages[prefix] = []
                    else:
                        if len(grouped_languages[prefix]) > max_entities:
                            prefix = f"more{prefix}"
                            if prefix not in grouped_languages:
                                grouped_languages[prefix] = []
            grouped_languages[prefix].append(lang)

        # Move single-item groups to the miscellaneous group
        for prefix, voices in list(grouped_languages.items()):
            if len(voices) < min_entities:
                misc_group.extend(voices)
                del grouped_languages[prefix]

        if misc_group:
            grouped_languages["misc"] = misc_group

        # Split the grouped languages into blocks of 5 columns
        max_columns = 5
        language_blocks = [dict(list(grouped_languages.items())[i:i + max_columns]) for i in range(0, len(grouped_languages), max_columns)]

        output = []

        for block in language_blocks:
            # Get the maximum number of rows
            max_rows = max(len(voices) for voices in block.values())

            # Calculate the maximum field text width for each column
            max_field_widths = {}
            for prefix, voices in block.items():
                max_field_widths[prefix] = max(len(lang) for lang in voices) + 4

            # Create the header row
            header_row = "| " + " | ".join(prefix.ljust(max_field_widths[prefix]) for prefix in block.keys()) + " |\n"

            # Create the separator row
            separator_row = "+-" + "-+-".join("-" * max_field_widths[prefix] for prefix in block.keys()) + "-+\n"

            language_list = header_row + separator_row

            for i in range(max_rows):
                row_text = "| "
                for prefix, voices in block.items():
                    if i < len(voices):
                        lang = voices[i]
                        row_text += lang.ljust(max_field_widths[prefix]) + " | "
                    else:
                        row_text += " ".ljust(max_field_widths[prefix]) + " | "
                language_list += row_text + "\n"

            output.append(f"```\n{language_list}\n```")
        return output
    
    def list_actors(self, actors: dict):
        output = ""
        for actor in actors:
            output = output + f"        `{actor}`: `{actors[actor]['voice']}`\n"
            output = output + f"                   To use it in a prompt, on its own line, type" + " {`" + f"{actor}" + "}`: Hello, world!\n"
            
        return output