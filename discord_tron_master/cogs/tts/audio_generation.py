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
        "default": "en_fiery",
        "announcer": "announcer",
        "cartoon_extreme": "cartoon_extreme",
        "classic_robot_tts": "classic_robot_tts",
        "cool_duo": "cool_duo",
        "de_speaker_0": "de_speaker_0","de_speaker_1": "de_speaker_1","de_speaker_2": "de_speaker_2","de_speaker_3": "de_speaker_3","de_speaker_4": "de_speaker_4","de_speaker_5": "de_speaker_5","de_speaker_6": "de_speaker_6","de_speaker_7": "de_speaker_7","de_speaker_8": "de_speaker_8","de_speaker_9": "de_speaker_9",
        "en_speaker_0": "en_speaker_0", "en_speaker_11": "en_speaker_11", "en_speaker_12": "en_speaker_12", "en_speaker_14": "en_speaker_14",
        "en_speaker_1": "en_speaker_1", "en_speaker_2": "en_speaker_2", "en_speaker_3": "en_speaker_3", "en_speaker_4": "en_speaker_4", "en_speaker_5": "en_speaker_5", "en_speaker_6": "en_speaker_6", "en_speaker_7": "en_speaker_7", "en_speaker_8": "en_speaker_8", "en_speaker_9": "en_speaker_9",
        "en_british": "en_british","en_deadpan": "en_deadpan","en_female_intense": "en_female_intense","en_female_performing_play_awesome_but_noisy": "en_female_performing_play_awesome_but_noisy","en_female_professional_reader": "en_female_professional_reader","en_female_slow_talker": "en_female_slow_talker","en_female_storyteller": "en_female_storyteller","en_fiery": "en_fiery","en_german_professor": "en_german_professor","en_guitar": "en_guitar","en_interesting_tone": "en_interesting_tone","en_male_nervous_subdued": "en_male_nervous_subdued","en_male_professional_reader": "en_male_professional_reader","en_man_giving_ted_talk": "en_man_giving_ted_talk","en_narrator_deep": "en_narrator_deep","en_narrator_light_bg": "en_narrator_light_bg","en_old_movie_actor": "en_old_movie_actor","en_public_speaker_2": "en_public_speaker_2","en_public_speaker": "en_public_speaker","en_quiet_intense": "en_quiet_intense","en_sharp_tone_but_noisy": "en_sharp_tone_but_noisy","en_smooth_gruff": "en_smooth_gruff","en_solo_singer": "en_solo_singer",  "en_tv_commercial": "en_tv_commercial",
        "es_speaker_0": "es_speaker_0", "es_speaker_1": "es_speaker_1", "es_speaker_2": "es_speaker_2", "es_speaker_3": "es_speaker_3", "es_speaker_4": "es_speaker_4", "es_speaker_5": "es_speaker_5", "es_speaker_6": "es_speaker_6", "es_speaker_7": "es_speaker_7", "es_speaker_8": "es_speaker_8", "es_speaker_9": "es_speaker_9",
        "fr_speaker_0": "fr_speaker_0", "fr_speaker_1": "fr_speaker_1", "fr_speaker_2": "fr_speaker_2", "fr_speaker_3": "fr_speaker_3", "fr_speaker_4": "fr_speaker_4", "fr_speaker_5": "fr_speaker_5", "fr_speaker_6": "fr_speaker_6", "fr_speaker_7": "fr_speaker_7", "fr_speaker_8": "fr_speaker_8", "fr_speaker_9": "fr_speaker_9",
        "hi_speaker_0": "hi_speaker_0", "hi_speaker_1": "hi_speaker_1", "hi_speaker_2": "hi_speaker_2", "hi_speaker_3": "hi_speaker_3", "hi_speaker_4": "hi_speaker_4", "hi_speaker_5": "hi_speaker_5", "hi_speaker_6": "hi_speaker_6", "hi_speaker_7": "hi_speaker_7", "hi_speaker_8": "hi_speaker_8", "hi_speaker_9": "hi_speaker_9",
        "it_speaker_0": "it_speaker_0","it_speaker_1": "it_speaker_1","it_speaker_2": "it_speaker_2","it_speaker_3": "it_speaker_3","it_speaker_4": "it_speaker_4","it_speaker_5": "it_speaker_5","it_speaker_6": "it_speaker_6","it_speaker_7": "it_speaker_7","it_speaker_8": "it_speaker_8","it_speaker_9": "it_speaker_9",
        "ja_speaker_0": "ja_speaker_0", "ja_speaker_1": "ja_speaker_1", "ja_speaker_2": "ja_speaker_2", "ja_speaker_3": "ja_speaker_3", "ja_speaker_4": "ja_speaker_4", "ja_speaker_5": "ja_speaker_5", "ja_speaker_6": "ja_speaker_6", "ja_speaker_7": "ja_speaker_7", "ja_speaker_8": "ja_speaker_8", "ja_speaker_9": "ja_speaker_9",
        "ko_speaker_0": "ko_speaker_0", "ko_speaker_1": "ko_speaker_1", "ko_speaker_2": "ko_speaker_2", "ko_speaker_3": "ko_speaker_3", "ko_speaker_4": "ko_speaker_4", "ko_speaker_5": "ko_speaker_5", "ko_speaker_6": "ko_speaker_6", "ko_speaker_7": "ko_speaker_7", "ko_speaker_8": "ko_speaker_8", "ko_speaker_9": "ko_speaker_9",
        "kpop_acoustic": "kpop_acoustic",
        "music_off_the_rails": "music_off_the_rails",
        "pl_speaker_0": "pl_speaker_0", "pl_speaker_1": "pl_speaker_1", "pl_speaker_2": "pl_speaker_2", "pl_speaker_3": "pl_speaker_3", "pl_speaker_4": "pl_speaker_4", "pl_speaker_5": "pl_speaker_5", "pl_speaker_6": "pl_speaker_6", "pl_speaker_7": "pl_speaker_7", "pl_speaker_8": "pl_speaker_8", "pl_speaker_9": "pl_speaker_9",
        "pt_speaker_0": "pt_speaker_0", "pt_speaker_1": "pt_speaker_1", "pt_speaker_2": "pt_speaker_2", "pt_speaker_3": "pt_speaker_3", "pt_speaker_4": "pt_speaker_4", "pt_speaker_5": "pt_speaker_5", "pt_speaker_6": "pt_speaker_6", "pt_speaker_7": "pt_speaker_7", "pt_speaker_8": "pt_speaker_8", "pt_speaker_9": "pt_speaker_9",
        "rock_maybe": "rock_maybe",
        "ru_speaker_0": "ru_speaker_0", "ru_speaker_1": "ru_speaker_1", "ru_speaker_2": "ru_speaker_2", "ru_speaker_3": "ru_speaker_3", "ru_speaker_4": "ru_speaker_4", "ru_speaker_5": "ru_speaker_5", "ru_speaker_6": "ru_speaker_6", "ru_speaker_7": "ru_speaker_7", "ru_speaker_8": "ru_speaker_8", "ru_speaker_9": "ru_speaker_9",
        "sing1": "sing1", "sing2": "sing2", "sing_3": "sing_3",
        "snarky_but_noisy": "snarky_but_noisy", "snarky_narrator_but_noisy": "snarky_narrator_but_noisy",
        "speaker_0": "speaker_0", "speaker_1": "speaker_1", "speaker_2": "speaker_2", "speaker_3": "speaker_3", "speaker_4": "speaker_4", "speaker_5": "speaker_5", "speaker_6": "speaker_6", "speaker_7": "speaker_7", "speaker_8": "speaker_8", "speaker_9": "speaker_9",
        "talkradio": "talkradio",
        "timid_jane": "timid_jane",
        "tr_speaker_0": "tr_speaker_0", "tr_speaker_1": "tr_speaker_1", "tr_speaker_2": "tr_speaker_2", "tr_speaker_3": "tr_speaker_3", "tr_speaker_4": "tr_speaker_4", "tr_speaker_5": "tr_speaker_5", "tr_speaker_6": "tr_speaker_6", "tr_speaker_7": "tr_speaker_7", "tr_speaker_8": "tr_speaker_8", "tr_speaker_9": "tr_speaker_9",
        "weirdvibes2": "weirdvibes2",
        "weirdvibes": "weirdvibes",
        "zh_speaker_0": "zh_speaker_0","zh_speaker_1": "zh_speaker_1","zh_speaker_2": "zh_speaker_2","zh_speaker_3": "zh_speaker_3","zh_speaker_4": "zh_speaker_4","zh_speaker_5": "zh_speaker_5","zh_speaker_6": "zh_speaker_6","zh_speaker_7": "zh_speaker_7","zh_speaker_8": "zh_speaker_8","zh_speaker_9": "zh_speaker_9"
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

            job = BarkTtsJob((self.bot, self.config, ctx, prompt, discord_first_message))
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
    @commands.command(name="tts-voices", help="List the available TTS voices.")
    async def tts_voice_list(self, ctx):
        try:
            available_languages = await self.list_available_languages()
            await discord.send_large_message(ctx=ctx, text="Available languages:\n" + available_languages)
        except Exception as e:
            await ctx.send(
                f"Error listing voices: {e}\n\nStack trace:\n{await clean_traceback(traceback.format_exc())}"
            )
    async def list_available_languages(self, user_id=None, languages=None):
        if languages is None:
            languages = Audio_generation.VOICES
        indicator = "**"  # Indicator variable
        indicator_length = len(indicator)
        max_columns = 5  # Maximum number of columns
        num_blocks = (len(languages) + max_columns - 1) // max_columns
        output = ""
        for block in range(num_blocks):
            start_index = block * max_columns
            end_index = min((block + 1) * max_columns, len(languages))
            block_languages = languages[start_index:end_index]
            # Calculate the maximum number of rows for the table
            max_rows = len(block_languages)
            # Calculate the maximum field text width for each column, including the indicator
            max_field_widths = [max(len(lang) + 2 * indicator_length for lang in block_languages)]
            # Generate language list in Markdown columns with padding
            header_row = "| " + " | ".join(lang.ljust(max_field_widths[0]) for lang in block_languages) + " |\n"
            separator_row = "+-" + "-+-".join("-" * (max_field_widths[0]) for _ in block_languages) + "-+\n"
            language_list = header_row + separator_row
            for i in range(max_rows):
                row_text = "| "
                for lang in block_languages:
                    current_language_indicator = ""
                    if user_id is not None:
                        user_language = config.get_user_setting(user_id, "language")
                        if user_language is not None and user_language == lang:
                            current_language_indicator = indicator
                    lang_str = current_language_indicator + lang + current_language_indicator
                    row_text += lang_str.ljust(max_field_widths[0]) + " | "
                language_list += row_text + "\n"
            # Wrap the output in triple backticks for fixed-width formatting in Discord
            output += f"```\n{language_list}\n```\n"
        return output